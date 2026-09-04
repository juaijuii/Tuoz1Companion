#!/usr/bin/env python3
"""
tuoz1_companion.py — Tuoz1 Bot 客户端插件（Tuoz1 Companion）
==============================================
运行在玩家自己的电脑上，直接读取英雄联盟客户端的本地 API（LCU，与 LeagueAkari 相同的原理），
在每局游戏结束后把完整的比赛详情发给 Discord 机器人，让机器人可以播报 Riot 公开 API
拿不到的模式（例如海克斯大乱斗）。

只依赖 Python 标准库，可直接 `python tuoz1_companion.py` 运行，也可以用 PyInstaller 打包成 exe。

首次运行会提示输入：
  1. 机器人地址（例如 http://1.2.3.4:25570）
  2. 在 Discord 里用 /companion_token 获取的令牌
配置保存在同目录的 companion_config.json。
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_NAME = "Tuoz1 Companion"
VERSION = "1.1.0"
PROTOCOL_VERSION = 1

IS_WINDOWS = sys.platform.startswith("win")
IS_FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "companion_config.json"
LOG_PATH = APP_DIR / "companion.log"

IN_GAME_PHASES = {"InProgress", "Reconnect"}
EOG_PHASES = {"WaitingForStats", "PreEndOfGame", "EndOfGame"}
EOG_WAIT_SECONDS = 150   # 游戏结束后最多等这么久拿赛后统计（治疗/护盾队友），超时就不带它上报
POST_GAME_FAST_POLL_SECONDS = 5      # 游戏刚结束后每 5 秒查一次战绩
POST_GAME_WINDOW_SECONDS = 15 * 60   # 结束后最多快查 15 分钟
RETRY_WINDOW_SECONDS = 30 * 60       # 不在语音频道时，最多等 30 分钟补报
PHASE_POLL_SECONDS = 3

logger = logging.getLogger("tuoz1-companion")


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"读取配置失败，将重新创建: {e}")
    return {}


def save_config(config: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:
        return ""


def pause_before_exit() -> None:
    if IS_FROZEN or IS_WINDOWS:
        try:
            input("\n按回车键退出...")
        except EOFError:
            pass


# ----------------------------------------------------------------------------
# 找到英雄联盟客户端（LCU）的端口和密码
# ----------------------------------------------------------------------------
_PORT_RE = re.compile(r'--app-port[=\s]+"?(\d+)"?')
_TOKEN_RE = re.compile(r'--remoting-auth-token[=\s]+"?([\w\-]+)"?')
_INSTALL_DIR_RE = re.compile(r'--install-directory[=\s]+"?([^"\r\n]+?)"?(?:\s+--|\s*$)')


def parse_client_command_line(text: str) -> tuple[int, str] | None:
    port = _PORT_RE.search(text or "")
    token = _TOKEN_RE.search(text or "")
    if port and token:
        return int(port.group(1)), token.group(1)
    return None


def parse_lockfile(path: Path) -> tuple[int, str] | None:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return None
    parts = content.split(":")
    # 格式: LeagueClient:PID:PORT:PASSWORD:https
    if len(parts) >= 5 and parts[2].isdigit():
        return int(parts[2]), parts[3]
    return None


def _run_hidden(cmd: list[str], timeout: float = 20) -> str:
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="ignore", **kwargs)
        return proc.stdout or ""
    except Exception as e:
        logger.debug(f"执行 {cmd[0]} 失败: {e}")
        return ""


def find_lcu() -> tuple[int, str] | None:
    """返回 (port, auth_token)，找不到返回 None"""
    override = os.environ.get("COMPANION_LCU_TOKEN")
    if override and os.environ.get("COMPANION_LCU_BASE"):
        return 0, override  # 测试模式：由 COMPANION_LCU_BASE 决定地址

    if IS_WINDOWS:
        # 1) PowerShell / CIM（最可靠）
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='LeagueClientUx.exe'\" "
              "| Select-Object -ExpandProperty CommandLine")
        out = _run_hidden(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
        found = parse_client_command_line(out)
        if found:
            return found
        # 2) wmic（老系统）
        out = _run_hidden(["wmic", "PROCESS", "WHERE", "name='LeagueClientUx.exe'", "GET", "commandline"])
        found = parse_client_command_line(out)
        if found:
            return found
        # 3) 从命令行里的安装目录读 lockfile
        m = _INSTALL_DIR_RE.search(out or "")
        candidates = []
        if m:
            candidates.append(Path(m.group(1).strip()) / "lockfile")
        candidates += [
            Path(r"C:\Riot Games\League of Legends\lockfile"),
            Path(r"D:\Riot Games\League of Legends\lockfile"),
            Path(r"C:\WeGameApps\英雄联盟\LeagueClient\lockfile"),
            Path(r"D:\WeGameApps\英雄联盟\LeagueClient\lockfile"),
            Path(r"E:\WeGameApps\英雄联盟\LeagueClient\lockfile"),
        ]
    else:
        out = _run_hidden(["sh", "-c", "ps -A -o command | grep -i 'LeagueClientUx' | grep -v grep"])
        found = parse_client_command_line(out)
        if found:
            return found
        candidates = [Path("/Applications/League of Legends.app/Contents/LoL/lockfile")]

    for path in candidates:
        if path.exists():
            found = parse_lockfile(path)
            if found:
                return found
    return None


# ----------------------------------------------------------------------------
# HTTP 客户端
# ----------------------------------------------------------------------------
class HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def _http_json(method: str, url: str, headers: dict, body: dict | None = None,
               timeout: float = 15, ssl_context: ssl.SSLContext | None = None):
    data = None
    hdrs = dict(headers)
    hdrs.setdefault("Accept", "application/json")
    hdrs.setdefault("User-Agent", f"tuoz1-companion/{VERSION}")
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        raise HttpError(e.code, raw.decode("utf-8", "ignore")) from None
    if not raw:
        return None
    return json.loads(raw.decode("utf-8", "ignore"))


class LCU:
    def __init__(self, port: int, token: str):
        base_override = os.environ.get("COMPANION_LCU_BASE")
        self.base = base_override.rstrip("/") if base_override else f"https://127.0.0.1:{port}"
        self.headers = {"Authorization": "Basic " + base64.b64encode(f"riot:{token}".encode()).decode()}
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def get(self, path: str, timeout: float = 15):
        return _http_json("GET", self.base + path, self.headers, timeout=timeout, ssl_context=self.ctx)

    # --- 常用接口 ---
    def gameflow_phase(self) -> str:
        return str(self.get("/lol-gameflow/v1/gameflow-phase") or "None")

    def current_game_id(self) -> int | None:
        """正在进行的对局 gameId（来自 /lol-gameflow/v1/session）"""
        session = self.get("/lol-gameflow/v1/session") or {}
        game_id = (session.get("gameData") or {}).get("gameId")
        return game_id if isinstance(game_id, int) and game_id > 0 else None

    def current_summoner(self) -> dict:
        return self.get("/lol-summoner/v1/current-summoner") or {}

    def champion_map(self) -> dict[str, str]:
        data = self.get("/lol-game-data/assets/v1/champion-summary.json") or []
        return {str(c["id"]): c["alias"] for c in data if isinstance(c, dict) and c.get("id", -1) > 0 and c.get("alias")}

    def latest_match_summary(self) -> tuple[dict | None, str]:
        hist = self.get("/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex=0") or {}
        games = ((hist.get("games") or {}).get("games")) or []
        platform = hist.get("platformId") or ""
        return (games[0] if games else None), platform

    def game_details(self, game_id: int) -> dict:
        return self.get(f"/lol-match-history/v1/games/{game_id}", timeout=30) or {}

    def eog_stats(self) -> dict | None:
        """赛后统计（结算界面的数据）。没到结算阶段时返回 None。"""
        try:
            return self.get("/lol-end-of-game/v1/eog-stats-block")
        except HttpError as e:
            if e.status == 404:
                return None
            raise


EOG_FIELDS = ("totalHealsOnTeammates", "totalDamageShieldedOnTeammates", "totalHeal", "totalUnitsHealed")
EOG_FIELDS_UPPER = {"totalHealsOnTeammates": "TOTAL_HEAL_ON_TEAMMATES",
                    "totalDamageShieldedOnTeammates": "TOTAL_DAMAGE_SHIELDED_ON_TEAMMATES",
                    "totalHeal": "TOTAL_HEAL", "totalUnitsHealed": "TOTAL_UNITS_HEALED"}

def extract_eog_stats(eog: dict) -> tuple[int | None, dict[str, dict]]:
    """从赛后统计里抽出每个玩家的补充字段：返回 (gameId, {puuid: {...}})"""
    result = {}
    for team in eog.get("teams") or []:
        for player in team.get("players") or []:
            puuid = player.get("puuid")
            stats = player.get("stats") or {}
            if not puuid:
                continue
            entry = {}
            for field in EOG_FIELDS:
                value = stats.get(field)
                if value is None:
                    value = stats.get(EOG_FIELDS_UPPER[field])
                if isinstance(value, (int, float)):
                    entry[field] = value
            entry["wasAfk"] = bool(player.get("wasAfk"))
            entry["leaver"] = bool(player.get("leaver"))
            result[str(puuid)] = entry
    game_id = eog.get("gameId")
    return (game_id if isinstance(game_id, int) else None), result


class BotAPI:
    def __init__(self, server: str, token: str):
        self.server = server.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def ping(self) -> dict:
        # 机器人第一次响应时要去数据库拉每个服务器的绑定，可能要几十秒
        return _http_json("GET", self.server + "/api/companion/ping", self.headers, timeout=90) or {}

    def post_match(self, payload: dict) -> dict:
        return _http_json("POST", self.server + "/api/companion/match", self.headers, body=payload, timeout=60) or {}


# ----------------------------------------------------------------------------
# 主逻辑
# ----------------------------------------------------------------------------
class Companion:
    def __init__(self, config: dict, bot: BotAPI, args):
        self.config = config
        self.bot = bot
        self.args = args
        self.lcu: LCU | None = None
        self.summoner: dict = {}
        self.champions: dict[str, str] = {}
        self.bindings: list[dict] = []
        self.last_phase: str | None = None
        self.post_game_deadline = 0.0
        self.next_history_check = 0.0
        self.last_lcu_warn = 0.0
        self.send_latest_once = bool(args.send_latest)
        self.current_game_id: int | None = None   # 正在打的对局
        self.pending_game_ids: list[int] = []      # 已结束、等待战绩生成的对局
        self.retry_uploads: dict[int, tuple[dict, str, float]] = {}  # 因不在语音频道被跳过的比赛: gameId -> (payload, match_id, 截止时间)
        self.eog_cache: dict[int, dict[str, dict]] = {}   # gameId -> {puuid: 赛后统计补充字段}
        self.eog_wait_until = 0.0                          # 游戏结束后等待赛后统计的截止时间
        self.config.setdefault("last_game_ids", {})
        self.config.setdefault("sent_game_ids", [])

    # --- 与机器人 ---
    def check_bot(self) -> bool:
        info = None
        for attempt in range(1, 4):
            try:
                logger.info(f"正在连接机器人 {self.bot.server} ...")
                info = self.bot.ping()
                break
            except HttpError as e:
                if e.status == 401:
                    logger.error("机器人拒绝了这个令牌（401）。请在 Discord 用 /companion_token 重新获取令牌。")
                    return False
                logger.warning(f"连接机器人失败（第 {attempt} 次）: {e}")
            except Exception as e:
                logger.warning(f"连接机器人失败（第 {attempt} 次）: {e}")
            time.sleep(5)
        if info is None:
            logger.error("多次连接机器人失败。请检查机器人地址是否正确、端口是否开放，或机器人是否在线。")
            return False
        self.bindings = info.get("bindings") or []
        if not self.bindings:
            logger.warning("机器人上没有找到你绑定的账号，请先在 Discord 里 /bind。")
        else:
            for b in self.bindings:
                logger.info(f"已绑定: {b.get('game_name')} [{b.get('region')}] @ {b.get('guild_name')}")
        if info.get("require_voice"):
            logger.info("提示：机器人只会为待在语音频道里的玩家播报。")
        return True

    # --- 与客户端 ---
    def connect_lcu(self) -> bool:
        found = find_lcu()
        if not found:
            now = time.time()
            if now - self.last_lcu_warn > 60:
                logger.info("未检测到英雄联盟客户端，等待中...（请先登录客户端）")
                self.last_lcu_warn = now
            return False
        port, token = found
        lcu = LCU(port, token)
        try:
            summoner = lcu.current_summoner()
            if not summoner.get("puuid"):
                return False  # 客户端已启动但还没登录
            self.champions = lcu.champion_map()
        except Exception as e:
            logger.debug(f"客户端尚未就绪: {e}")
            return False
        self.lcu = lcu
        self.summoner = summoner
        name = summoner.get("gameName") or summoner.get("displayName") or "?"
        tag = summoner.get("tagLine") or ""
        logger.info(f"已连接英雄联盟客户端: {name}#{tag} (英雄数据 {len(self.champions)} 个)")
        # 客户端的 puuid 和机器人（Riot 公开 API）的 puuid 不是同一个值，按游戏名比对
        bound_names = {str(b.get("game_name") or "").strip().lower() for b in self.bindings}
        if self.bindings and str(name).strip().lower() not in bound_names:
            logger.warning(f"当前登录的账号 {name}#{tag} 没有在机器人上绑定，打完的比赛会被机器人忽略。")
        self.next_history_check = 0.0
        return True

    def disconnect_lcu(self, reason: str) -> None:
        if self.lcu is not None:
            logger.info(f"与客户端断开连接: {reason}")
        self.lcu = None
        self.summoner = {}
        self.last_phase = None

    # --- 战绩 ---
    def already_sent(self, game_id: int) -> bool:
        return game_id in self.config["sent_game_ids"]

    def mark_sent(self, game_id: int) -> None:
        puuid = self.summoner.get("puuid", "")
        self.config["last_game_ids"][puuid] = game_id
        sent = self.config["sent_game_ids"]
        if game_id not in sent:
            sent.append(game_id)
        del sent[:-100]
        save_config(self.config)

    def collect_eog(self) -> None:
        """结算阶段抓一次赛后统计（含治疗/护盾队友），按 gameId 缓存"""
        assert self.lcu is not None
        try:
            eog = self.lcu.eog_stats()
        except Exception as e:
            logger.debug(f"读取赛后统计失败: {e}")
            return
        if not eog:
            return
        game_id, stats = extract_eog_stats(eog)
        if game_id and stats and game_id not in self.eog_cache:
            self.eog_cache[game_id] = stats
            logger.info(f"已获取比赛 {game_id} 的赛后统计（{len(stats)} 人）")
            # 只保留最近几场
            for old in list(self.eog_cache)[:-5]:
                self.eog_cache.pop(old, None)

    def check_new_game(self) -> None:
        """两条路径找新比赛：
        1) 打完的对局 gameId（来自 gameflow session）直接查 /games/{gameId}，最快最可靠
        2) 战绩列表最新一场（覆盖插件启动前就已经结束的比赛）"""
        assert self.lcu is not None
        # 路径 1
        for game_id in list(self.pending_game_ids):
            if self.already_sent(game_id):
                self.pending_game_ids.remove(game_id)
                continue
            if game_id not in self.eog_cache:
                self.collect_eog()
                if game_id not in self.eog_cache and time.time() < self.eog_wait_until:
                    logger.debug(f"等待比赛 {game_id} 的赛后统计...")
                    continue
            try:
                game = self.lcu.game_details(game_id)
            except HttpError as e:
                logger.debug(f"比赛 {game_id} 详情尚未生成 ({e.status})")
                game = {}
            if game.get("participantIdentities"):
                self.pending_game_ids.remove(game_id)
                self.send_game(game, game.get("platformId") or "")
            elif time.time() > self.post_game_deadline:
                logger.warning(f"等待比赛 {game_id} 的战绩超时，放弃（下次启动时如果仍是最新一场会补发）。")
                self.pending_game_ids.remove(game_id)

        # 路径 2
        try:
            summary, platform = self.lcu.latest_match_summary()
        except HttpError as e:
            logger.debug(f"战绩列表暂不可用 ({e.status})")
            return
        if not summary:
            return
        game_id = summary.get("gameId")
        if not isinstance(game_id, int):
            return
        platform = (summary.get("platformId") or platform or "").upper()
        puuid = self.summoner.get("puuid", "")
        last_id = self.config["last_game_ids"].get(puuid)

        if not self.send_latest_once:
            if (game_id == last_id or self.already_sent(game_id) or game_id == self.current_game_id
                    or game_id in self.retry_uploads):
                return
            if last_id is None:
                # 第一次运行：以当前最新一场为基线，不重复上报旧比赛
                self.config["last_game_ids"][puuid] = game_id
                save_config(self.config)
                logger.info(f"以比赛 {platform}_{game_id} 为基线，之后的新比赛会自动上报。")
                return
        self.send_latest_once = False

        logger.info(f"发现新比赛 {platform}_{game_id}（{summary.get('gameMode')} / queue {summary.get('queueId')}），获取详情...")
        if game_id not in self.eog_cache:
            self.collect_eog()   # 插件启动前刚打完的那局，结算数据可能还在
        game = self.lcu.game_details(game_id)
        if not game.get("participantIdentities"):
            logger.warning("比赛详情尚不完整，稍后重试。")
            return
        self.send_game(game, platform)

    def send_game(self, game: dict, platform: str) -> None:
        game_id = game["gameId"]
        platform = (platform or game.get("platformId") or "").upper()
        match_id = f"{platform}_{game_id}"
        eog_stats = self.eog_cache.get(game_id) or {}
        logger.info(f"上报比赛 {match_id}（{game.get('gameMode')} / queue {game.get('queueId')}，"
                    f"赛后统计{'已附带' if eog_stats else '缺失'}）...")
        payload = {
            "protocol": PROTOCOL_VERSION,
            "companion_version": VERSION,
            "puuid": self.summoner.get("puuid", ""),
            "platformId": platform,
            "game": game,
            "champions": self.champions,
            "eog_stats": eog_stats,
        }
        if self.args.dump_dir:
            dump = Path(self.args.dump_dir) / f"{match_id}.json"
            dump.parent.mkdir(parents=True, exist_ok=True)
            dump.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"已保存比赛数据到 {dump}")
        outcome = self.upload(payload, match_id)
        if outcome == "retry":
            # 还没进语音频道: 30 分钟内每分钟重试一次，进了语音就会补播
            self.retry_uploads[game_id] = (payload, match_id, time.time() + RETRY_WINDOW_SECONDS)
            logger.info(f"比赛 {match_id} 暂未播报（不在语音频道），{RETRY_WINDOW_SECONDS // 60} 分钟内进语音频道会自动补报。")
        elif outcome:
            self.mark_sent(game_id)

    def process_retries(self) -> None:
        for game_id, (payload, match_id, deadline) in list(self.retry_uploads.items()):
            if time.time() > deadline:
                logger.info(f"比赛 {match_id} 超过补报时限，放弃。")
                self.retry_uploads.pop(game_id, None)
                self.mark_sent(game_id)
                continue
            outcome = self.upload(payload, match_id, quiet=True)
            if outcome == "retry":
                continue
            self.retry_uploads.pop(game_id, None)
            if outcome:
                self.mark_sent(game_id)

    def upload(self, payload: dict, match_id: str, quiet: bool = False):
        """返回 True=已完成, "retry"=机器人因不在语音频道跳过, False=失败"""
        for attempt in range(1, 4):
            try:
                resp = self.bot.post_match(payload)
            except HttpError as e:
                if e.status == 401:
                    logger.error("令牌已失效（401），请重新用 /companion_token 获取令牌后修改 companion_config.json。")
                    return False
                if e.status == 422:
                    logger.warning(f"机器人：这个账号没有绑定，忽略比赛 {match_id}。")
                    return True
                if e.status == 400:
                    logger.error(f"机器人拒绝了数据: {e.body[:300]}")
                    return True
                logger.warning(f"上报失败（第 {attempt} 次）: {e}")
            except Exception as e:
                logger.warning(f"上报失败（第 {attempt} 次）: {e}")
            else:
                return self.report(resp, match_id, quiet)
            time.sleep(5 * attempt)
        logger.error(f"比赛 {match_id} 上报失败，放弃（机器人会在你下次开机时重试最新一场）。")
        return False

    @staticmethod
    def report(resp: dict, match_id: str, quiet: bool = False):
        status = resp.get("status")
        mode = resp.get("game_mode")
        queue = resp.get("queue_id")
        if status == "ignored_mode":
            logger.info(f"机器人忽略了 {match_id}（模式 {mode}/{queue} 不在播报范围内）")
            return True
        if status == "puuid_not_in_match":
            logger.warning(f"机器人：当前账号不在比赛 {match_id} 里？已跳过。")
            return True
        guilds = resp.get("guilds") or []
        if not guilds:
            logger.info(f"机器人已接收 {match_id}，但没有需要播报的服务器。")
            return True
        statuses = []
        for g in guilds:
            st = g.get("status")
            statuses.append(st)
            text = {
                "accepted": "✅ 机器人已接收，正在播报",
                "broadcast": "✅ 已播报",
                "already_processed": "已处理过（可能 API 已播报）",
                "not_in_voice": "你不在语音频道，未播报",
                "analyze_failed": "分析失败",
                "error": f"出错: {g.get('error')}",
            }.get(st, str(st))
            if not quiet or st != "not_in_voice":
                logger.info(f"[{g.get('guild_name')}] {match_id} ({mode}/{queue}): {text}")
        if statuses and all(s == "not_in_voice" for s in statuses):
            return "retry"
        return True

    # --- 主循环 ---
    def run(self) -> None:
        interval = max(15, int(self.args.interval))
        while True:
            now = time.time()
            if self.lcu is None:
                if not self.connect_lcu():
                    time.sleep(10)
                    continue
            try:
                phase = self.lcu.gameflow_phase()
            except Exception as e:
                self.disconnect_lcu(f"{e}")
                time.sleep(5)
                continue

            if phase in IN_GAME_PHASES and self.current_game_id is None:
                try:
                    self.current_game_id = self.lcu.current_game_id()
                    if self.current_game_id:
                        logger.info(f"对局进行中: gameId {self.current_game_id}")
                except Exception as e:
                    logger.debug(f"读取对局信息失败: {e}")

            if phase != self.last_phase:
                if self.last_phase is not None:
                    logger.info(f"客户端状态: {self.last_phase} -> {phase}")
                if self.last_phase in IN_GAME_PHASES and phase not in IN_GAME_PHASES:
                    logger.info("检测到游戏结束，开始等待战绩生成...")
                    if self.current_game_id and self.current_game_id not in self.pending_game_ids:
                        self.pending_game_ids.append(self.current_game_id)
                    self.current_game_id = None
                    self.post_game_deadline = now + POST_GAME_WINDOW_SECONDS
                    self.eog_wait_until = now + EOG_WAIT_SECONDS
                    self.next_history_check = now + POST_GAME_FAST_POLL_SECONDS
                if phase in EOG_PHASES and self.pending_game_ids:
                    self.collect_eog()
                self.last_phase = phase
            elif phase in EOG_PHASES and self.pending_game_ids and \
                    any(g not in self.eog_cache for g in self.pending_game_ids):
                self.collect_eog()

            if now >= self.next_history_check:
                try:
                    # 账号切换检测
                    current = self.lcu.current_summoner()
                    if current.get("puuid") and current.get("puuid") != self.summoner.get("puuid"):
                        self.summoner = current
                        logger.info(f"账号已切换为 {current.get('gameName')}#{current.get('tagLine')}")
                    self.check_new_game()
                    self.process_retries()
                except HttpError as e:
                    logger.debug(f"查询战绩失败: {e}")
                except Exception as e:
                    self.disconnect_lcu(f"{e}")
                    continue
                fast = now < self.post_game_deadline
                self.next_history_check = time.time() + (POST_GAME_FAST_POLL_SECONDS if fast else interval)

            time.sleep(PHASE_POLL_SECONDS)


def setup_console_utf8() -> None:
    """Windows 控制台默认代码页可能不是 UTF-8，切到 65001 以正确显示中文。"""
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
            ctypes.windll.kernel32.SetConsoleTitleW(f"{APP_NAME} v{VERSION}")
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def setup_logging(verbose: bool) -> None:
    setup_console_utf8()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(fh)
    except Exception:
        pass


def main() -> int:
    setup_console_utf8()
    parser = argparse.ArgumentParser(description=f"{APP_NAME} — Tuoz1 Bot 客户端插件")
    parser.add_argument("--server", help="机器人地址，例如 http://1.2.3.4:25570")
    parser.add_argument("--token", help="/companion_token 获取的令牌")
    parser.add_argument("--interval", type=int, default=60, help="平时检查新战绩的间隔（秒），默认 60")
    parser.add_argument("--send-latest", action="store_true", help="启动后立刻把最近一场比赛上报一次（用于测试）")
    parser.add_argument("--dump-dir", help="把上报的数据另存到此目录（调试用）")
    parser.add_argument("--reset", action="store_true", help="清除已保存的配置并重新设置")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    logger.info(f"{APP_NAME} v{VERSION} — Tuoz1 Bot 客户端插件  (配置: {CONFIG_PATH})")

    config = {} if args.reset else load_config()
    if args.server:
        config["server"] = args.server
    if args.token:
        config["token"] = args.token

    if not config.get("server"):
        config["server"] = prompt("请输入机器人地址（例如 http://1.2.3.4:25570）: ")
    if not config.get("token"):
        config["token"] = prompt("请输入 /companion_token 获取的令牌: ")
    if not config.get("server") or not config.get("token"):
        logger.error("缺少机器人地址或令牌。")
        return 2
    if not config["server"].startswith(("http://", "https://")):
        config["server"] = "http://" + config["server"]
    save_config(config)

    bot = BotAPI(config["server"], config["token"])
    companion = Companion(config, bot, args)
    if not companion.check_bot():
        return 1
    try:
        companion.run()
    except KeyboardInterrupt:
        logger.info("已退出。")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:  # 让 exe 用户能看到错误
        logging.getLogger("tuoz1-companion").error(f"发生错误: {e}", exc_info=True)
        code = 1
    if code != 0:
        pause_before_exit()
    sys.exit(code)
