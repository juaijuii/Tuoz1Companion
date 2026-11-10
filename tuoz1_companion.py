#!/usr/bin/env python3
"""
tuoz1_companion.py — Tuoz1 Bot 客户端插件（Tuoz1 Companion）
==============================================
运行在玩家自己的电脑上，直接读取英雄联盟客户端的本地 API（LCU，与 LeagueAkari 相同的原理），
在每局游戏结束后把完整的比赛详情发给 Discord 机器人，让机器人可以播报 Riot 公开 API
拿不到的模式（例如海克斯大乱斗）。

只依赖 Python 标准库，可直接 `python tuoz1_companion.py` 运行，也可以用 PyInstaller 打包成 exe。

机器人地址固定为 SERVER_URL（v1.4.0 起写死在代码里，玩家不需要也不能自己填；
旧配置文件里保存的地址会在启动时被覆盖）。首次运行只提示输入在 Discord 里用
/插件令牌 获取的令牌，配置保存在同目录的 companion_config.json。

批量导入黑名单（v1.5.0）：把 League Akari 导出的「标记玩家」JSON 拖到 exe 图标上，
或者 `tuoz1_companion.py --import-blacklist 文件`。插件通过客户端把 puuid 换成名字，
再发给机器人存进你选的服务器的黑名单（要求你是那个服务器的管理员）。

窗口（v1.7.0）：连上机器人后弹出一个小窗口（tkinter，标准库），黑色命令行窗口自动藏起来，
点「完整日志」再显示。窗口里有「英雄联盟，启动！」按钮、「简中汉化」和「自动接受对局」开关：
汉化 = 把 Riot 的 league_of_legends.live.product_settings.yaml 里的 locale 改成 zh_CN
（简体中文的文字和语音），插件开着的时候 Riot 客户端改回去就再改过来。--no-gui 只用命令行。

海克斯助手（v1.8.0）：海克斯大乱斗里三选一出来时，插件自动截下三张卡的标题发给机器人认，
机器人回每张卡对你这个英雄的梯队和排名，插件把它标在卡片上方，推荐的那张描边变亮，选完自动消失。
自动没认出来可以按 F6 手动认一次（只在海克斯大乱斗对局中占用 F6）；截图用系统自带的接口，游戏要用无边框模式。
"""
from __future__ import annotations

import argparse
import base64
import difflib
import http.client
import json
import logging
import math
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_NAME = "Tuoz1 Companion"
VERSION = "1.9.4"
PROTOCOL_VERSION = 1
SERVER_URL = "http://api.tuoz1bot.com:5016"   # 机器人地址写死；--server 只能临时覆盖，不会写进配置

IS_WINDOWS = sys.platform.startswith("win")
IS_FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "companion_config.json"
LOG_PATH = APP_DIR / "companion.log"

UPDATE_REPO = "juaijuii/Tuoz1Companion"
UPDATE_API = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
UPDATE_ASSET = "Tuoz1Companion.exe"
UPDATE_CHECK_SECONDS = 30 * 60      # 闲着的时候多久看一次有没有新版本（另外每打完一局、战绩报完以后马上看一次）
# 这些阶段不更新（更新要重启插件，几秒钟里什么都干不了）：排到了等确认、选人、读条、对局中、等结算
UPDATE_BUSY_PHASES = {"ReadyCheck", "ChampSelect", "GameStart", "InProgress", "Reconnect", "WaitingForStats", "PreEndOfGame"}
IN_GAME_PHASES = {"InProgress", "Reconnect"}
EOG_PHASES = {"WaitingForStats", "PreEndOfGame", "EndOfGame"}
EOG_WAIT_SECONDS = 150   # 游戏结束后最多等这么久拿赛后统计（治疗/护盾队友），超时就不带它上报
POST_GAME_FAST_POLL_SECONDS = 5      # 游戏刚结束后每 5 秒查一次战绩
POST_GAME_WINDOW_SECONDS = 15 * 60   # 结束后最多快查 15 分钟
RETRY_WINDOW_SECONDS = 30 * 60       # 不在语音频道时，最多等 30 分钟补报
FAIL_RETRY_WINDOW_SECONDS = 2 * 3600 # 上传失败（机器人重启 / 网络）时最多等 2 小时补报
RETRY_INTERVAL_SECONDS = 60          # 补报重试间隔（不跟随游戏结束后的快速轮询）
STALE_HISTORY_SECONDS = 12 * 3600    # 战绩列表里的「最新一场」超过这么久就不补报了（只记成基线）
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
    show_console(True)
    try:
        return input(text).strip()
    except EOFError:
        return ""


def prompt_with_timeout(text: str, seconds: float) -> str:
    """最多等 seconds 秒的输入：后台没人看控制台时不能让主循环（看对局、补报）卡在 input() 上"""
    import threading
    box = {}
    show_console(True)        # 窗口模式下黑框是藏起来的，要输入就得先让人看见

    def _read():
        try:
            box["v"] = input(text).strip()
        except EOFError:
            box["v"] = ""
    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(seconds)
    return box.get("v", "")


# 窗口模式下黑色命令行窗口默认藏起来；要输入令牌、或者点了「完整日志」时再显示
UI_STATE = {"console_hidden": False, "uploads": (None, 0)}


def _console_hwnd() -> int:
    if not IS_WINDOWS:
        return 0
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
    except Exception:
        return 0


def console_is_ours() -> bool:
    """黑框是不是插件自己的（双击 exe 打开的）。从 cmd / 终端里运行时黑框是别人的，不能藏。
    PyInstaller 单文件版有两个进程（启动器 + 本体）挂在同一个黑框上，所以不超过 2 个都算自己的"""
    if not (IS_WINDOWS and IS_FROZEN and _console_hwnd()):
        return False
    try:
        import ctypes
        arr = (ctypes.c_uint32 * 8)()
        return ctypes.windll.kernel32.GetConsoleProcessList(arr, 8) <= 2
    except Exception:
        return False


def show_console(show: bool) -> None:
    hwnd = _console_hwnd()
    if not hwnd or (not show and not console_is_ours()):
        return
    try:
        import ctypes
        ctypes.windll.user32.ShowWindow(hwnd, 5 if show else 0)      # SW_SHOW / SW_HIDE
        if show:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        UI_STATE["console_hidden"] = not show
    except Exception:
        pass


def count_broadcast() -> None:
    """窗口上的「今天播报 N 局」"""
    today = time.strftime("%Y-%m-%d")
    day, n = UI_STATE["uploads"]
    UI_STATE["uploads"] = (today, (n if day == today else 0) + 1)


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
        # 系统工具一律用绝对路径：按裸名字调用时 CreateProcess 会先在 exe 自己的目录（通常是「下载」）找同名程序，
        # 谁往那里放一个 powershell.exe 就能借插件的权限跑代码
        sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
        powershell = os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        wmic = os.path.join(sysroot, "System32", "wbem", "wmic.exe")
        # 1) PowerShell / CIM（最可靠）
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='LeagueClientUx.exe'\" "
              "| Select-Object -ExpandProperty CommandLine")
        out = _run_hidden([powershell, "-NoProfile", "-NonInteractive", "-Command", ps]) if os.path.exists(powershell) else ""
        found = parse_client_command_line(out)
        if found:
            return found
        # 2) wmic（老系统；新版 Win11 已经没有了）
        out = _run_hidden([wmic, "PROCESS", "WHERE", "name='LeagueClientUx.exe'", "GET", "commandline"]) if os.path.exists(wmic) else ""
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


CONNECT_SECONDS = 4             # 连一个地址最多等这么久，连不上就换下一个（正常连上用不了 1 秒）
_GOOD_ADDR: dict = {}           # (域名, 端口) -> 上次连通的那个地址：下次先连它
_OPENER = None


def _quick_connect(address, timeout=None, source_address=None, **_ignored):
    """代替 socket.create_connection。一个域名常常同时有 IPv4 和 IPv6 两个地址，系统把 IPv6 排在前面；
    玩家家里的宽带有 IPv6、而那个 IPv6 地址其实连不通时，自带的连法会在它上面等满整个超时（8 秒、15 秒、
    最长约 21 秒）才换 IPv4 —— 每一次请求都要白等这么久（实测：三选一 8 秒才出、选人侧栏晚十几秒、
    AI 建议晚二十多秒）。这里改成：上次连通的地址最先试，其次 IPv4，每个地址最多等 CONNECT_SECONDS 秒"""
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    good = _GOOD_ADDR.get((host, port))
    infos.sort(key=lambda i: (i[4] != good, i[0] != socket.AF_INET))       # 排序是稳定的：其余的保持系统给的先后
    total = timeout if isinstance(timeout, (int, float)) else socket.getdefaulttimeout()
    last = None
    for family, kind, proto, _, addr in infos:
        sock = None
        try:
            sock = socket.socket(family, kind, proto)
            sock.settimeout(min(total, CONNECT_SECONDS) if total else CONNECT_SECONDS)
            if source_address:
                sock.bind(source_address)
            sock.connect(addr)
            sock.settimeout(total)                    # 连上以后，等回应的时间还是调用的人说了算
            _GOOD_ADDR[(host, port)] = addr
            return sock
        except OSError as e:
            last = e
            if sock is not None:
                sock.close()
    _GOOD_ADDR.pop((host, port), None)
    raise last if last is not None else OSError(f"{host} 没有解析出地址")


class _QuickHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _quick_connect


class _QuickHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _quick_connect


class _QuickHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_QuickHTTPConnection, req)


class _QuickHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_QuickHTTPSConnection, req, context=self._quick_context)


def _open(req, timeout: float):
    """连外面的服务器（机器人、GitHub）都走这里；连本机的客户端 / 游戏用不着（只有一个地址）"""
    global _OPENER
    if _OPENER is None:
        https = _QuickHTTPSHandler()
        https._quick_context = ssl.create_default_context()
        _OPENER = urllib.request.build_opener(_QuickHTTPHandler(), https)
    return _OPENER.open(req, timeout=timeout)


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
        with (_open(req, timeout) if ssl_context is None
              else urllib.request.urlopen(req, timeout=timeout, context=ssl_context)) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        raise HttpError(e.code, raw.decode("utf-8", "ignore")) from None
    if not raw:
        return None
    return json.loads(raw.decode("utf-8", "ignore"))


_HISTORY_GUARD = threading.Lock()
_HISTORY_LOCKS: dict = {}                       # puuid -> 锁：同一个人的战绩同时只查一次
_HISTORY_SLOTS = threading.Semaphore(4)         # 查别人战绩的请求同时最多 4 个


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

    def post(self, path: str, timeout: float = 15):
        return _http_json("POST", self.base + path, self.headers, timeout=timeout, ssl_context=self.ctx)

    def patch(self, path: str, body: dict, timeout: float = 15):
        return _http_json("PATCH", self.base + path, self.headers, body=body, timeout=timeout, ssl_context=self.ctx)

    def get_bytes(self, path: str, timeout: float = 5) -> bytes:
        """读一个文件的原始内容（比如英雄头像图），不当 JSON 解析"""
        req = urllib.request.Request(self.base + path, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, "") from None

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

    def summoner_by_puuid(self, puuid: str) -> dict:
        return self.get(f"/lol-summoner/v2/summoners/puuid/{puuid}", timeout=10) or {}

    def champion_map(self) -> dict[str, str]:
        data = self.get("/lol-game-data/assets/v1/champion-summary.json") or []
        return {str(c["id"]): c["alias"] for c in data if isinstance(c, dict) and c.get("id", -1) > 0 and c.get("alias")}

    def latest_match_summary(self) -> tuple[dict | None, str]:
        hist = self.get("/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex=0") or {}
        games = ((hist.get("games") or {}).get("games")) or []
        platform = hist.get("platformId") or ""
        return (games[0] if games else None), platform

    def match_history(self, puuid: str, count: int = 20) -> list[dict]:
        """任意玩家（puuid）最近 count 局。客户端里点开别人的战绩用的就是这个接口"""
        # 开局情报和十人战绩都要查对面的人：两分钟内查过的直接用，不让客户端重复去问
        # 而且同一个人同时只问一次（后到的等先到的结果），全插件同时最多 4 个请求：
        # 读条时客户端本来就忙，一口气问十几个人的战绩会把它卡到连「现在什么阶段」都答不上来
        cache = self.__dict__.setdefault("_history_cache", {})
        hit = cache.get((puuid, count))
        if hit and time.time() - hit[0] < 120:
            return hit[1]
        with _HISTORY_GUARD:
            lock = _HISTORY_LOCKS.setdefault(puuid, threading.Lock())
        with lock:
            hit = cache.get((puuid, count))
            if hit and time.time() - hit[0] < 120:
                return hit[1]
            with _HISTORY_SLOTS:
                data = self.get(f"/lol-match-history/v1/products/lol/{puuid}/matches?begIndex=0&endIndex={count - 1}", timeout=15) or {}
            games = ((data.get("games") or {}).get("games")) or []
            cache[(puuid, count)] = (time.time(), games)
        for key in [k for k, v in list(cache.items()) if time.time() - v[0] > 300]:
            cache.pop(key, None)
        if len(_HISTORY_LOCKS) > 200:
            with _HISTORY_GUARD:
                _HISTORY_LOCKS.clear()
        return games

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
    """从赛后统计里抽出每个玩家的补充字段：返回 (gameId, {puuid: {...}})。客户端返回的结构不对就当没有"""
    result = {}
    if not isinstance(eog, dict):
        return None, result
    teams = eog.get("teams") if isinstance(eog.get("teams"), list) else []
    for team in teams:
        players = team.get("players") if isinstance(team, dict) and isinstance(team.get("players"), list) else []
        for player in players:
            if not isinstance(player, dict):
                continue
            puuid = player.get("puuid")
            stats = player.get("stats") if isinstance(player.get("stats"), dict) else {}
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


SCOUT_DETAIL_GAMES = 10       # 开局情报：每人最多补查几局详情（算伤害占比用）
SCOUT_DETAIL_SECONDS = 8      # 补查详情最多等这么久，读条时间有限
SCOUT_MAX_GAME_TIME = 180     # 中途打开插件时，游戏内时间超过这么多秒就不查开局情报了
ARENA_QUEUE_IDS = {1700, 1710, 1740, 1750}   # 斗魂竞技场的队列：开局情报不查（模式名字有时拿不到，靠队列和人数兜底）


def _team_damage(participants: list, team_id) -> int | None:
    """一局里某一队对英雄的总伤害；数据不全返回 None"""
    total = sum(int((x.get("stats") or {}).get("totalDamageDealtToChampions") or 0)
                for x in participants if x.get("teamId") == team_id)
    return total or None


# 连机器人时顺带报给它的开关（只报开 / 关，机器人主人用来看有多少人开着、多少人关了）：(配置里的名字, 没写过时算开还是关)
FEATURE_FLAGS = (("aram_helper", True), ("aram_auto", True), ("aram_panel", True), ("aram_ai_guide", True),
                 ("aram_borderless", True), ("player_stats", True))


def feature_flags(config: dict, gui: bool) -> str:
    """几个开关现在的状态，写成 "gui=1;aram_helper=1;aram_auto=0;…"。
    没开窗口（--no-gui）时海克斯助手这些都用不了，只报 gui=0"""
    if not gui:
        return "gui=0"
    return ";".join(["gui=1"] + [f"{key}={int(config.get(key, default) is not False and bool(config.get(key, default)))}"
                                 for key, default in FEATURE_FLAGS])


class BotAPI:
    def __init__(self, server: str, token: str):
        self.server = server.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.config: dict | None = None    # main() 填上以后，ping 时顺带报开关状态；没填就不报
        self.gui = False

    def ping(self, game_name: str = "", tag_line: str = "") -> dict:
        # 机器人第一次响应时要去数据库拉每个服务器的绑定，可能要几十秒
        url = self.server + "/api/companion/ping"
        if game_name and tag_line:
            url += "?" + urllib.parse.urlencode({"game_name": game_name, "tag_line": tag_line})
        headers = dict(self.headers)
        if self.config is not None:
            headers["X-Companion-Features"] = feature_flags(self.config, self.gui)
        return _http_json("GET", url, headers, timeout=90) or {}

    def post_match(self, payload: dict) -> dict:
        return _http_json("POST", self.server + "/api/companion/match", self.headers, body=payload, timeout=60) or {}

    def post_game_start(self, payload: dict) -> dict:
        return _http_json("POST", self.server + "/api/companion/game_start", self.headers, body=payload, timeout=30) or {}

    def post_scout(self, payload: dict) -> dict:
        return _http_json("POST", self.server + "/api/companion/scout", self.headers, body=payload, timeout=30) or {}

    def post_aram_augments(self, payload: dict) -> dict:
        # 海克斯助手：三选一只有几十秒，等不到就算了，让玩家再按一次
        return _http_json("POST", self.server + "/api/companion/aram/augments", self.headers, body=payload, timeout=8) or {}

    def get_aram_tiers(self) -> dict:
        return _http_json("GET", self.server + "/api/companion/aram/tiers", self.headers, timeout=15) or {}

    def get_aram_table(self, champion_id: int, game_id=None) -> dict:
        """这个英雄的全部海克斯数据（本机认字时用：三选一时查这张表，不用再问机器人）"""
        url = self.server + f"/api/companion/aram/table?champion={int(champion_id)}"
        if isinstance(game_id, int) and game_id > 0:
            url += f"&game_id={game_id}"
        return _http_json("GET", url, self.headers, timeout=15) or {}

    def get_aram_champion(self, champion_id: int) -> dict:
        return _http_json("GET", self.server + f"/api/companion/aram/champion?id={int(champion_id)}", self.headers, timeout=15) or {}

    def post_aram_guide(self, payload: dict) -> dict:
        # AI 写攻略要几秒到十几秒；读条有三四十秒，等得起
        return _http_json("POST", self.server + "/api/companion/aram/guide", self.headers, body=payload, timeout=25) or {}

    def post_blacklist_import(self, payload: dict) -> dict:
        # 一批最多 25 条，机器人那边每条要查一次 Riot（1.3 秒），所以超时给足
        return _http_json("POST", self.server + "/api/companion/blacklist_import", self.headers, body=payload, timeout=180) or {}


# ----------------------------------------------------------------------------
# 自动更新（只对打包成 exe 的形态生效）
# ----------------------------------------------------------------------------
def parse_version(text: str) -> tuple:
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in nums[:3]) or (0,)


def cleanup_old_binary() -> None:
    """新版本启动后，把上一版留下的 .old 文件删掉"""
    if not IS_FROZEN:
        return
    old = Path(sys.executable).with_suffix(".old.exe")
    for _ in range(5):
        try:
            if old.exists():
                old.unlink()
            return
        except OSError:
            time.sleep(1)


def fetch_latest_release() -> tuple[str, str, int] | None:
    """返回 (版本号, exe 下载地址, 大小)；没有可用版本返回 None"""
    data = _http_json("GET", UPDATE_API, {"Accept": "application/vnd.github+json"}, timeout=20)
    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name") or ""
    for asset in data.get("assets") or []:
        if asset.get("name") == UPDATE_ASSET and asset.get("browser_download_url"):
            return tag, asset["browser_download_url"], int(asset.get("size") or 0)
    return None


def download_file(url: str, dest: Path, expected_size: int = 0) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": f"tuoz1-companion/{VERSION}"})
    with _open(req, 120) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
    size = dest.stat().st_size
    if expected_size and size != expected_size:
        raise RuntimeError(f"下载不完整：{size} / {expected_size} 字节")
    with open(dest, "rb") as f:
        if f.read(2) != b"MZ":
            raise RuntimeError("下载的文件不是 Windows 可执行文件")


def check_for_update(enabled: bool = True) -> bool:
    """有新版本时下载并切换到新版本（返回 True 表示当前进程应立即退出）"""
    if not enabled:
        return False
    try:
        latest = fetch_latest_release()
    except Exception as e:
        logger.debug(f"检查更新失败: {e}")
        return False
    if not latest:
        return False
    tag, url, size = latest
    if parse_version(tag) <= parse_version(VERSION):
        logger.debug(f"已是最新版本（{VERSION}，最新 {tag}）")
        return False
    if not IS_FROZEN:
        logger.info(f"有新版本 {tag}（当前 {VERSION}），源码运行不会自动替换，请到 GitHub 下载：{url}")
        return False

    logger.info(f"发现新版本 {tag}（当前 {VERSION}），正在下载更新...")
    exe = Path(sys.executable)
    new_file = exe.with_suffix(".new.exe")
    old_file = exe.with_suffix(".old.exe")
    try:
        download_file(url, new_file, size)
        if old_file.exists():
            old_file.unlink()
        os.rename(exe, old_file)      # Windows 允许给正在运行的 exe 改名
        os.rename(new_file, exe)
    except Exception as e:
        logger.error(f"更新失败，继续使用当前版本: {e}")
        # 回滚的两步分开 try：以前删 .new 失败（杀软锁文件）就跳过了「把旧 exe 改回来」，插件本体直接没了
        try:
            if new_file.exists():
                new_file.unlink()
        except OSError as e2:
            logger.warning(f"清理半截的新文件失败: {e2}")
        try:
            if not exe.exists() and old_file.exists():
                os.rename(old_file, exe)
        except OSError as e2:
            logger.error(f"把旧版本改回原名失败，请手动把 {old_file.name} 改成 {exe.name}: {e2}")
        return False

    logger.info(f"已更新到 {tag}，正在重新启动...")
    try:
        args = [str(exe)] + [a for a in sys.argv[1:] if a != "--send-latest"]
        # PyInstaller 会把 _PYI_ARCHIVE_FILE / _MEIPASS2 等变量留在环境里，新版本继承后会去找已被改名的旧文件而直接退出，
        # 这里从当前进程环境里彻底删掉（本进程已经解包完成，不再需要它们）
        for key in list(os.environ):
            if key.startswith("_PYI_") or key == "_MEIPASS2":
                os.environ.pop(key, None)
        env = dict(os.environ)
        kwargs = {"cwd": str(exe.parent), "env": env, "close_fds": True}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        child = subprocess.Popen(args, **kwargs)
        time.sleep(3)
        if child.poll() is not None:
            logger.warning(f"新版本进程退出了（返回码 {child.returncode}），改用系统方式启动")
            if IS_WINDOWS:
                os.startfile(str(exe))
            else:
                subprocess.Popen(args, cwd=str(exe.parent), env=env, start_new_session=True)
            time.sleep(2)
    except Exception as e:
        logger.error(f"启动新版本失败，请手动双击 {exe.name}: {e}")
        return False
    return True


# ----------------------------------------------------------------------------
# 管理员权限：游戏通常是以管理员身份在跑，插件权限比它低的话系统不让插件看到按键（按住 Tab 看出装就没反应）
# ----------------------------------------------------------------------------
ELEVATE_LINGER_SECONDS = 6      # 申请到权限、开出管理员身份的自己以后，原来这个进程再留这么久才退出（比自动更新盯的 3 秒长）


def relaunch_elevated(argv: list) -> str:
    """让 Windows 以管理员身份再开一个自己（会弹一次系统的确认框）。
    返回 started（开出来了，这个进程该退出）/ declined（确认框点了「否」）/ failed"""
    import ctypes
    from ctypes import wintypes
    # 和自动更新重启时一样：PyInstaller 留在环境里的变量万一被新进程继承，它会去用我们这个进程解包出来的临时目录，
    # 而那个目录在我们退出时就删了
    for key in list(os.environ):
        if key.startswith("_PYI_") or key == "_MEIPASS2":
            os.environ.pop(key, None)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, ctypes.c_int]
    rc = shell32.ShellExecuteW(None, "runas", sys.executable, subprocess.list2cmdline(argv), str(APP_DIR), 1) or 0
    if rc > 32:
        return "started"
    return "declined" if rc == 5 or ctypes.get_last_error() == 1223 else "failed"


def ensure_elevated(args) -> bool:
    """插件不是管理员身份时，申请一次。返回 True = 已经另开了一个管理员身份的自己，这个进程该退出了。

    为什么不直接在 exe 上写死「必须管理员」：旧版本自动更新完是用普通方式启动新版本的，exe 写死的话那一步会直接失败；
    而且确认框点了「否」、或者这个账号根本不是管理员时，插件就完全打不开了。这样做的话拿不到权限也照常运行，只是 Tab 可能没反应。
    不申请的情况：源码运行、已经是管理员、这就是申请之后开出来的那个（--elevated，防止来回重开）、
    拖文件导入黑名单、命令行加了 --no-elevate、配置文件里 "run_as_admin": false"""
    if not (IS_WINDOWS and IS_FROZEN) or args.no_elevate or args.elevated or args.import_blacklist or args.file:
        return False
    try:
        if load_config().get("run_as_admin", True) is False or process_elevated(os.getpid()) is not False:
            return False
        logger.info("正在申请管理员权限，Windows 会弹一次确认框，点「是」就行。原因：游戏通常以管理员身份运行，"
                    "插件权限比它低的话系统不让插件看到按键，对局里按住 Tab 看出装会没有反应")
        result = relaunch_elevated(sys.argv[1:] + ["--elevated"])
    except Exception as e:
        logger.debug(f"申请管理员权限出错: {e}")
        result = "failed"
    if result == "started":
        # 别马上退出：旧版本自动更新完会盯新版本 3 秒，看到它退出了就以为没启动成功、会再开一个出来。
        # 这几秒把自己的黑窗口藏起来（管理员身份的那个有它自己的窗口）
        show_console(False)
        time.sleep(ELEVATE_LINGER_SECONDS)
        return True
    logger.warning("没有拿到管理员权限（确认框点了「否」，或者这个账号不是管理员）。插件照常运行，只是对局里按住 Tab 看出装可能没有反应。"
                   "不想每次被问：在 companion_config.json 里加一行 \"run_as_admin\": false")
    return False


def spawn_unelevated(cmd: list) -> bool:
    """插件自己是管理员身份时，用普通权限去开别的程序（借桌面那个资源管理器进程的身份）。成功返回 True。
    用在「英雄联盟，启动！」上：不这样的话游戏会跟着插件变成管理员身份，别的普通权限的程序（比如 Discord 的按键说话）
    在游戏里就收不到按键了"""
    import ctypes
    from ctypes import wintypes

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                    ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    u.GetShellWindow.restype = wintypes.HWND
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    k.OpenProcess.restype, k.OpenProcess.argtypes = wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    a.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                   ctypes.POINTER(wintypes.HANDLE)]
    a.CreateProcessWithTokenW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
                                          wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                          ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
    handles = []
    try:
        pid = wintypes.DWORD(0)
        shell = u.GetShellWindow()
        if not shell or not u.GetWindowThreadProcessId(shell, ctypes.byref(pid)) or not pid.value:
            return False
        proc = k.OpenProcess(0x0400, False, pid.value)                 # PROCESS_QUERY_INFORMATION
        if not proc:
            return False
        handles.append(proc)
        token = wintypes.HANDLE()
        if not a.OpenProcessToken(proc, 0x0002, ctypes.byref(token)):  # TOKEN_DUPLICATE
            return False
        handles.append(token)
        primary = wintypes.HANDLE()
        # TOKEN_QUERY | ASSIGN_PRIMARY | DUPLICATE | ADJUST_DEFAULT | ADJUST_SESSIONID；SecurityImpersonation；TokenPrimary
        if not a.DuplicateTokenEx(token, 0x018B, None, 2, 1, ctypes.byref(primary)):
            return False
        handles.append(primary)
        si, pi = STARTUPINFOW(), PROCESS_INFORMATION()
        si.cb = ctypes.sizeof(si)
        line = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(c) for c in cmd]))
        if not a.CreateProcessWithTokenW(primary, 0, None, line, 0x00000200, None, str(Path(str(cmd[0])).parent),
                                         ctypes.byref(si), ctypes.byref(pi)):      # CREATE_NEW_PROCESS_GROUP
            return False
        handles.extend((pi.hProcess, pi.hThread))
        return True
    except Exception as e:
        logger.debug(f"用普通权限启动失败: {e}")
        return False
    finally:
        for h in handles:
            k.CloseHandle(h)


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
        self.phase_errors = 0                      # 连着几次问客户端阶段都超时
        self.send_latest_once = bool(args.send_latest)
        self.last_reauth_prompt = 0.0
        self.account_issue = ""                    # 当前登录的号有问题（没绑定 / 令牌是别人的）：窗口上红字提示
        self.current_game_id: int | None = None   # 正在打的对局
        self.pending_game_ids: list[int] = []      # 已结束、等待战绩生成的对局
        self.retry_uploads: dict[int, tuple[dict, str, float]] = {}  # 因不在语音频道被跳过的比赛: gameId -> (payload, match_id, 截止时间)
        self.eog_cache: dict[int, dict[str, dict]] = {}   # gameId -> {puuid: 赛后统计补充字段}
        self.eog_wait_until = 0.0                          # 游戏结束后等待赛后统计的截止时间
        self.next_retry_check = 0.0                        # 补报（不在语音频道）的下一次尝试时间，固定 60 秒间隔
        self.next_update_check = time.time() + UPDATE_CHECK_SECONDS
        self.update_after_game = False             # 刚打完一局：战绩报完、回到大厅以后马上看一次有没有新版本
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
                    logger.error("机器人拒绝了这个令牌（401）。请在 Discord 用 /插件令牌 查看令牌。")
                    return "unauthorized"
                logger.warning(f"连接机器人失败（第 {attempt} 次）: {e}")
            except Exception as e:
                logger.warning(f"连接机器人失败（第 {attempt} 次）: {e}")
            time.sleep(5)
        if info is None:
            logger.error("多次连接机器人失败。请检查本机网络是否正常，或机器人是否在线（地址已内置在插件里，不需要自己填）。")
            return False
        self.bindings = info.get("bindings") or []
        if not self.bindings:
            logger.warning("机器人上没有找到你绑定的账号，请先在 Discord 里 /绑定。")
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
        # 让机器人用 Riot 账号接口判断当前账号是否绑定（改过名也能识别）；机器人不支持时退回按游戏名比对
        bound = None
        elsewhere = False
        try:
            info = self.bot.ping(name, tag)
            elsewhere = bool(info.get("current_bound_elsewhere"))     # 机器人 2026-11-09 起才有这个字段
            if "current_bound" in info:
                bound = bool(info["current_bound"])
                if bound and info.get("current_bound_as") and str(info["current_bound_as"]).lower() != str(name).lower():
                    logger.info(f"当前账号在机器人上绑定的名字是 {info['current_bound_as']}（你改过名，不影响使用）")
        except Exception as e:
            logger.debug(f"查询绑定状态失败: {e}")
        if bound is None:
            bound_names = {str(b.get("game_name") or "").strip().lower() for b in self.bindings}
            bound = (not self.bindings) or str(name).strip().lower() in bound_names
        # 令牌是谁的：用它绑定的游戏号来指代（只是插件自己的绑定列表，不涉及别人的信息）
        owner = "、".join(sorted({str(b.get("game_name") or "").strip() for b in self.bindings if b.get("game_name")})[:2])
        self.account_issue = ""
        if elsewhere and not bound:
            # 共用令牌：朋友直接拷了别人的插件文件夹（里面的 companion_config.json 带着别人的令牌）
            self.account_issue = "这个号是别人绑的，令牌不是它的"
            logger.warning(f"当前登录的 {name}#{tag} 是另一个 Discord 账号绑定的，但插件里的令牌属于"
                           f"{f'绑定了「{owner}」的' if owner else '另一个'} Discord 账号（令牌不能共用）。"
                           f"这个号打的比赛和开局情报都没法通过这个插件上报。"
                           f"如果 {name} 是你的号：用你自己的 Discord 账号输入 /插件令牌 拿你自己的令牌，"
                           f"然后删掉插件文件夹里的 companion_config.json，重开插件粘贴新令牌。")
        elif self.bindings and not bound:
            self.account_issue = "这个号没绑定"
            if owner:
                logger.warning(f"当前登录的账号 {name}#{tag} 没有在机器人上绑定，打完的比赛会被机器人忽略。"
                               f"插件里的令牌属于绑定了「{owner}」的 Discord 账号："
                               f"如果你就是这个 Discord 账号，用 /绑定 {name}#{tag} 把这个号也绑上就行；"
                               f"如果不是，用你自己的 Discord 账号 /绑定 {name}#{tag}，再输入 /插件令牌 拿你自己的令牌，"
                               f"删掉插件文件夹里的 companion_config.json，重开插件粘贴新令牌（令牌不能共用）。")
            else:
                logger.warning(f"当前登录的账号 {name}#{tag} 没有在机器人上绑定，打完的比赛会被机器人忽略。"
                               f"请在 Discord 用 /绑定 {name}#{tag} 绑定。")
        self.next_history_check = 0.0
        return True

    def disconnect_lcu(self, reason: str) -> None:
        if self.lcu is not None:
            logger.info(f"与客户端断开连接: {reason}")
        self.lcu = None
        self.summoner = {}
        self.account_issue = ""
        # 断开时如果正在对局中，这局先记进待上报队列（重连后战绩生成了照样能报）；
        # 然后必须清掉 current_game_id：以前不清，重连后下一局因为它非空而不发开局通知、不开盘
        if self.current_game_id and self.last_phase in IN_GAME_PHASES:
            if self.current_game_id not in self.pending_game_ids:
                self.pending_game_ids.append(self.current_game_id)
            self.post_game_deadline = time.time() + POST_GAME_WINDOW_SECONDS
            self.eog_wait_until = time.time() + EOG_WAIT_SECONDS
            logger.info(f"断开时对局 {self.current_game_id} 还在进行，重连后会补查它的战绩")
        self.current_game_id = None
        self.last_phase = None

    # --- 开局通知（机器人据此开赌局）---
    def live_game_time(self) -> float | None:
        """游戏内已进行的秒数（来自游戏进程的 Live Client Data 接口，端口 2999）；游戏还没起来 / 查不到返回 None"""
        try:
            data = _http_json("GET", "https://127.0.0.1:2999/liveclientdata/gamestats", {}, timeout=3,
                              ssl_context=self.lcu.ctx if self.lcu else None)
            t = (data or {}).get("gameTime")
            return float(t) if isinstance(t, (int, float)) else None
        except Exception:
            return None

    def notify_game_start(self, game_id: int, seen_start: bool = True) -> None:
        """进入对局时把 gameId、模式和同队队友告诉机器人。失败只记日志，不影响其它功能。
        seen_start：是不是亲眼看到从选人进入对局（插件中途启动接上一局正在打的对局时为 False）"""
        try:
            session = self.lcu.get("/lol-gameflow/v1/session") or {}
            game_data = session.get("gameData") or {}
            queue = game_data.get("queue") or {}
            my_puuid = self.summoner.get("puuid") or ""
            my_name = str(self.summoner.get("gameName") or "").strip().lower()

            def entry_name(t: dict) -> str:
                return str(t.get("gameName") or t.get("summonerName") or t.get("summonerInternalName") or "").strip()

            teammates, players = [], []
            for team_no, team in enumerate((game_data.get("teamOne") or [], game_data.get("teamTwo") or []), 1):
                entries = [t for t in team if isinstance(t, dict)]
                mine = any(t.get("puuid") == my_puuid or entry_name(t).lower() == my_name for t in entries)
                for t in entries:
                    name = entry_name(t)
                    tag = str(t.get("tagLine") or "").strip()
                    puuid = str(t.get("puuid") or "").strip()
                    if (not name or not tag) and puuid:
                        # 对局数据里往往没有名字（新版客户端、或者对方开了隐藏名字），按 puuid 再查一次：
                        # 客户端这个接口不受隐私设置影响，所以黑名单预警能抓到隐身的人
                        try:
                            info = self.lcu.summoner_by_puuid(puuid)
                            name = str(info.get("gameName") or name or "").strip()
                            tag = str(info.get("tagLine") or tag or "").strip()
                        except Exception as e:
                            logger.debug(f"查询玩家 {puuid} 失败: {e}")
                    if puuid or name:
                        players.append({"lcu_puuid": puuid, "game_name": name, "tag_line": tag, "team": team_no,
                                        "is_me": t.get("puuid") == my_puuid, "champion_id": t.get("championId")})
                    if mine and t.get("puuid") != my_puuid and name and name.lower() != my_name:
                        teammates.append({"game_name": name, "tag_line": tag})
            logger.info("同队队友: " + (", ".join(f"{t['game_name']}#{t['tag_line']}" for t in teammates) or "（对局数据里没有队友信息）"))
            hidden = sum(1 for p in players if not p["game_name"])
            logger.info(f"对局共 {len(players)} 人" + (f"，{hidden} 人查不到名字" if hidden else "") + "，已交给机器人对黑名单")
            try:
                _, platform = self.lcu.latest_match_summary()
            except Exception:
                platform = ""
            payload = {
                "companion_version": VERSION,
                "game_id": game_id,
                "platform": str(platform or "").upper(),
                "game_name": self.summoner.get("gameName") or "",
                "tag_line": self.summoner.get("tagLine") or "",
                "game_mode": queue.get("gameMode") or "",
                "queue_id": queue.get("id") if isinstance(queue.get("id"), int) else None,
                "teammates": teammates,
                "players": players,
                "game_time": self.live_game_time(),       # 已进行秒数；机器人据此决定还开不开盘
                "phase_seen_start": bool(seen_start),
                "is_custom": bool(game_data.get("isCustomGame")) or str(queue.get("type") or "").upper() == "CUSTOM",
            }
            # 开局情报（v1.6.0）：查对面每个人最近的对局交给机器人。放到后台线程，不耽误开盘和主循环。
            # 读条时就开着插件的照常查；中途才打开的，游戏内时间还没到 SCOUT_MAX_GAME_TIME 也查（v1.6.1）。
            # 中途打开又读不到游戏时间的不查：没法确认是不是已经打了很久
            gt = payload["game_time"]
            early = seen_start or (isinstance(gt, (int, float)) and gt <= SCOUT_MAX_GAME_TIME)
            # 斗魂竞技场不查：读条时模式名字偶尔是空的，所以再看队列编号，以及是不是两边各 5 人
            team_sizes = [sum(1 for p in players if p.get("team") == n) for n in (1, 2)]
            arena = (payload["game_mode"].upper() == "CHERRY" or payload["queue_id"] in ARENA_QUEUE_IDS
                     or team_sizes != [5, 5])
            payload["team_sizes"] = team_sizes
            if early and not payload["is_custom"] and not arena:
                threading.Thread(target=self.scout_enemies, args=(game_id, payload, players), daemon=True).start()
            resp = self.bot.post_game_start(payload)
            for g in resp.get("blacklist_alerted") or []:
                logger.warning(f"⚠️ 这局里有黑名单玩家，已在 {g} 提醒")
            opened = resp.get("opened") or []
            if opened:
                logger.info(f"已通知机器人开局，赌局在 {', '.join(opened)} 开盘（队友 {len(teammates)} 人）")
            else:
                logger.info(f"已通知机器人开局（{resp.get('ignored') or '没有需要开盘的服务器'}）")
        except HttpError as e:
            if e.status == 404:
                logger.debug("机器人版本还不支持开局通知")
            else:
                logger.warning(f"通知机器人开局失败: {e}")
        except Exception as e:
            logger.warning(f"通知机器人开局失败: {e}")

    def scout_enemies(self, game_id: int, start_payload: dict, players: list[dict]) -> None:
        """开局情报：对面每个人最近 20 局的原始数据交给机器人，谁是大哥、谁开黑由机器人算（改算法不用发新版插件）。
        只读客户端接口，不碰 Riot API；任何失败只记日志"""
        try:
            my_team = next((p["team"] for p in players if p.get("is_me")), None)
            enemies = [p for p in players if my_team and p.get("team") != my_team and p.get("lcu_puuid")][:5]
            if not enemies:
                logger.debug("开局情报：找不到对面的玩家，跳过")
                return
            lcu = self.lcu

            def fetch(p: dict) -> dict:
                rows = []
                try:
                    for g in lcu.match_history(p["lcu_puuid"]):
                        if not isinstance(g, dict) or g.get("gameId") == game_id:
                            continue
                        parts = g.get("participants") or []
                        idents = g.get("participantIdentities") or []
                        # 别人的战绩列表里通常只有他自己一个人；保险起见按 puuid 找
                        pid = next((i.get("participantId") for i in idents
                                    if ((i.get("player") or {}).get("puuid")) == p["lcu_puuid"]), None)
                        part = next((x for x in parts if x.get("participantId") == pid), parts[0] if parts else None)
                        if not part:
                            continue
                        st = part.get("stats") or {}
                        rows.append({
                            "game_id": g.get("gameId"), "queue_id": g.get("queueId"),
                            "team_id": part.get("teamId"),
                            # 列表里如果带了全场 10 个人，直接算出他那一队的总伤害；只有他自己时先空着，后面查详情补
                            "team_damage": _team_damage(parts, part.get("teamId")) if len(parts) >= 6 else None,
                            "champion": self.champions.get(str(part.get("championId")), ""),
                            "win": bool(st.get("win")), "kills": int(st.get("kills") or 0),
                            "deaths": int(st.get("deaths") or 0), "assists": int(st.get("assists") or 0),
                            "damage": int(st.get("totalDamageDealtToChampions") or 0),
                            "duration": int(g.get("gameDuration") or 0),
                        })
                except Exception as e:
                    logger.debug(f"开局情报：查 {p.get('game_name') or p['lcu_puuid'][:8]} 的战绩失败: {e}")
                return {"lcu_puuid": p["lcu_puuid"], "game_name": p.get("game_name") or "", "tag_line": p.get("tag_line") or "",
                        "champion": self.champions.get(str(p.get("champion_id")), ""), "games": rows}

            from concurrent.futures import ThreadPoolExecutor, wait
            started = time.time()
            with ThreadPoolExecutor(max_workers=5) as pool:
                scouted = list(pool.map(fetch, enemies))
            # 伤害占比要知道那一局全队打了多少：列表里没有的，按局查详情补上。每人只补最近 SCOUT_DETAIL_GAMES 局，
            # 开黑的人共同的对局只查一次；总共最多等 SCOUT_DETAIL_SECONDS 秒，查不完的局机器人退回用每分钟伤害
            need = {}
            for e in scouted:
                for row in [r for r in e["games"] if r["team_damage"] is None][:SCOUT_DETAIL_GAMES]:
                    need.setdefault(row["game_id"], []).append(row)
            if need:
                def detail(gid):
                    try:
                        parts = (lcu.game_details(gid) or {}).get("participants") or []
                        for row in need[gid]:
                            row["team_damage"] = _team_damage(parts, row["team_id"])
                    except Exception as e:
                        logger.debug(f"开局情报：查对局 {gid} 详情失败: {e}")
                pool = ThreadPoolExecutor(max_workers=8)
                futures = [pool.submit(detail, gid) for gid in need]
                done, _ = wait(futures, timeout=SCOUT_DETAIL_SECONDS)
                pool.shutdown(wait=False, cancel_futures=True)
                filled = sum(1 for rows in need.values() for r in rows if r["team_damage"])
                logger.info(f"开局情报：补了 {len(done)}/{len(need)} 局详情，{filled} 条有全队伤害")
            got = sum(1 for e in scouted if e["games"])
            logger.info(f"开局情报：查到对面 {got}/{len(scouted)} 人的近期战绩（{time.time() - started:.1f} 秒）")
            if not got:
                return
            payload = {k: start_payload.get(k) for k in ("companion_version", "game_id", "platform", "game_name",
                                                         "tag_line", "game_mode", "queue_id", "game_time", "team_sizes")}
            payload["enemies"] = scouted
            resp = self.bot.post_scout(payload)
            if resp.get("ignored"):
                logger.info(f"开局情报：机器人没有播（{resp['ignored']}）")
            else:
                logger.info("开局情报已交给机器人")
        except HttpError as e:
            if e.status == 404:
                logger.debug("机器人版本还不支持开局情报")
            else:
                logger.warning(f"开局情报发送失败: {e}")
        except Exception as e:
            logger.warning(f"开局情报出错: {e}")

    def reauthorize(self) -> bool:
        """运行中令牌失效（比如在 Discord 重新生成了令牌）：当场让用户粘贴新令牌，不用重启"""
        now = time.time()
        if now - self.last_reauth_prompt < 300:
            return False          # 5 分钟内问过了，别反复弹
        self.last_reauth_prompt = now
        logger.error("机器人拒绝了当前令牌（401）。可能是在 Discord 重新生成过令牌。")
        new_token = prompt_with_timeout("请粘贴新的令牌（在 Discord 输入 /插件令牌 查看；直接回车或 60 秒不理会就先跳过）: ", 60)
        if not new_token:
            logger.warning("没有输入新令牌，接下来的上报会先排队，5 分钟后再问一次。")
            return False
        self.config["token"] = new_token
        save_config(self.config)
        self.bot.headers = {"Authorization": f"Bearer {new_token}"}
        logger.info("令牌已更新。")
        return True

    # --- 战绩 ---
    def already_sent(self, game_id: int) -> bool:
        return game_id in self.config["sent_game_ids"]

    def mark_sent(self, game_id: int, puuid: str | None = None) -> None:
        puuid = puuid or self.summoner.get("puuid", "")     # 补报时可能已经换了账号登录，要记到打这局的那个账号名下
        self.config["last_game_ids"][puuid] = game_id
        sent = self.config["sent_game_ids"]
        if game_id not in sent:
            sent.append(game_id)
        del sent[:-100]
        save_config(self.config)

    def collect_eog(self) -> None:
        """结算阶段抓一次赛后统计（含治疗/护盾队友），按 gameId 缓存。出错只记日志，不让主循环崩"""
        try:
            self._collect_eog()
        except Exception as e:
            logger.debug(f"读取赛后统计失败: {e}")

    def _collect_eog(self) -> None:
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
                    or game_id in self.retry_uploads or game_id in self.pending_game_ids):   # 路径 1 还在等它的赛后统计
                return
            if last_id is None:
                # 第一次运行：以当前最新一场为基线，不重复上报旧比赛
                self.config["last_game_ids"][puuid] = game_id
                save_config(self.config)
                logger.info(f"以比赛 {platform}_{game_id} 为基线，之后的新比赛会自动上报。")
                return
            # 「和上次不一样」不等于「比上次新」。斗魂竞技场这类模式不进战绩列表（或列表还没刷新），
            # 打完一局竞技场后，列表里的最新一场还是很久以前的那场，以前会被当成新比赛报上去。
            # 同一个大区里 gameId 是递增的：比上次的小，就一定不是新比赛
            if isinstance(last_id, int) and game_id < last_id:
                logger.debug(f"战绩列表最新一场 {game_id} 比已上报的 {last_id} 还旧，跳过")
                return
            # 隔了很久才开插件：那场比赛早就过去了，别再进语音念一遍，只记成基线
            created = summary.get("gameCreation")
            if isinstance(created, (int, float)) and created > 0 and \
                    time.time() - created / 1000 > STALE_HISTORY_SECONDS:
                self.config["last_game_ids"][puuid] = game_id
                save_config(self.config)
                logger.info(f"比赛 {platform}_{game_id} 是 {int((time.time() - created / 1000) // 3600)} 小时前的，"
                            f"不再补报，以它为新基线。")
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
        if outcome == "unauthorized":
            outcome = self.upload(payload, match_id) if self.reauthorize() else "retry"
        if outcome == "retry":
            # 还没进语音频道: 30 分钟内每分钟重试一次，进了语音就会补播
            self.retry_uploads[game_id] = (payload, match_id, time.time() + RETRY_WINDOW_SECONDS)
            logger.info(f"比赛 {match_id} 暂未播报（不在语音频道），{RETRY_WINDOW_SECONDS // 60} 分钟内进语音频道会自动补报。")
        elif outcome is False:
            # 机器人没响应（重启 / 网络）：不在这里睡着等，进补报队列，2 小时内每分钟试一次
            self.retry_uploads[game_id] = (payload, match_id, time.time() + FAIL_RETRY_WINDOW_SECONDS)
            logger.warning(f"比赛 {match_id} 暂时发不出去，{FAIL_RETRY_WINDOW_SECONDS // 3600} 小时内会自动重试。")
        else:
            self.mark_sent(game_id, payload.get("puuid"))

    def process_retries(self) -> None:
        if not self.retry_uploads or time.time() < self.next_retry_check:
            return
        self.next_retry_check = time.time() + RETRY_INTERVAL_SECONDS
        for game_id, (payload, match_id, deadline) in list(self.retry_uploads.items()):
            if time.time() > deadline:
                logger.info(f"比赛 {match_id} 超过补报时限，放弃。")
                self.retry_uploads.pop(game_id, None)
                self.mark_sent(game_id)
                continue
            outcome = self.upload(payload, match_id, quiet=True)
            if outcome == "unauthorized":
                if not self.reauthorize():
                    continue          # 没给新令牌：留在补报队列里下次再试
                outcome = self.upload(payload, match_id, quiet=True)
            if outcome == "retry":
                continue
            if outcome is False:
                continue          # 这次没发出去（机器人重启 / 网络）：留在队列里下次再试，到期再放弃
            self.retry_uploads.pop(game_id, None)
            self.mark_sent(game_id, payload.get("puuid"))

    def upload(self, payload: dict, match_id: str, quiet: bool = False):
        """返回 True=已完成, "retry"=机器人因不在语音频道跳过, False=失败"""
        for attempt in range(1, 3):
            try:
                resp = self.bot.post_match(payload)
            except HttpError as e:
                if e.status == 401:
                    return "unauthorized"
                if e.status == 422:
                    logger.warning(f"机器人：这个账号没有绑定，忽略比赛 {match_id}。")
                    return True
                if e.status == 400:
                    logger.error(f"机器人拒绝了数据: {e.body[:300]}")
                    return True
                if e.status == 503:
                    # 机器人刚重启、数据库或 Riot 接口暂时不通：不是终态，进补报队列过会儿再发
                    logger.info(f"机器人暂时判断不了这局（{e.body[:80]}），稍后自动重试。")
                    return "retry"
                logger.warning(f"上报失败（第 {attempt} 次）: {e}")
            except Exception as e:
                logger.warning(f"上报失败（第 {attempt} 次）: {e}")
            else:
                return self.report(resp, match_id, quiet)
            if attempt < 2:
                time.sleep(2)     # 只快速重试一次；再不行就交给补报队列，别在这里睡几十秒不看对局
            else:
                break
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
        if any(s in ("accepted", "broadcast") for s in statuses):
            count_broadcast()
        if statuses and any(s == "not_in_voice" for s in statuses):
            return "retry"        # 有一个服务器还没进语音就继续补报；已经播过的服务器会回 already_processed，无害
        return True

    def update_due(self, now: float, phase: str) -> bool:
        """现在该不该去看有没有新版本。两个时机：刚打完一局（战绩报完以后），或者闲着满了 UPDATE_CHECK_SECONDS。
        忙的时候不看——更新要重启插件：选人时侧栏会没掉，对局中标注会没掉；战绩还没报完的话重启会把它丢了。
        排队时可以更新，但开着「自动接受对局」的人不行（重启那几秒正好排到就接不上了）"""
        if phase in UPDATE_BUSY_PHASES or self.pending_game_ids or self.retry_uploads:
            return False
        if phase == "Matchmaking" and getattr(getattr(self, "accepter", None), "enabled", False):
            return False
        return self.update_after_game or now >= self.next_update_check

    # --- 主循环 ---
    def run(self) -> None:
        interval = max(15, int(self.args.interval))
        while True:
            now = time.time()
            self.process_retries()          # 补报不依赖客户端：打完就关客户端的人，进语音后照样能补播
            if self.lcu is None:
                if not self.connect_lcu():
                    time.sleep(10)
                    continue
            try:
                phase = self.lcu.gameflow_phase()
                self.phase_errors = 0
            except Exception as e:
                # 客户端只是答得慢（读条、刚进游戏时常见）不算断开：连着三次都超时才算。
                # 以前一次超时就断开重连，重连后要重新走一遍开局通知，海克斯助手这期间也跟着停摆
                self.phase_errors += 1
                if "timed out" in str(e).lower() and self.phase_errors < 3:
                    logger.debug(f"客户端答得慢（第 {self.phase_errors} 次超时），先不当成断开")
                    time.sleep(2)
                    continue
                self.phase_errors = 0
                self.disconnect_lcu(f"{e}")
                time.sleep(5)
                continue

            if phase in IN_GAME_PHASES and self.current_game_id is None:
                try:
                    self.current_game_id = self.lcu.current_game_id()
                    if self.current_game_id:
                        logger.info(f"对局进行中: gameId {self.current_game_id}")
                        # last_phase 是 None（插件刚起来）或本来就在对局里 = 中途接上的，不是亲眼看到开局
                        seen_start = self.last_phase is not None and self.last_phase not in IN_GAME_PHASES
                        self.notify_game_start(self.current_game_id, seen_start)
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
                    self.update_after_game = True
                    self.post_game_deadline = now + POST_GAME_WINDOW_SECONDS
                    self.eog_wait_until = now + EOG_WAIT_SECONDS
                    self.next_history_check = now + POST_GAME_FAST_POLL_SECONDS
                if phase in EOG_PHASES and self.pending_game_ids:
                    self.collect_eog()
                self.last_phase = phase
            elif phase in EOG_PHASES and self.pending_game_ids and \
                    any(g not in self.eog_cache for g in self.pending_game_ids):
                self.collect_eog()
            # 已经离开结算界面（点了再来一局 / 回大厅）还没拿到赛后统计，它不会再出现了：
            # 以前要干等满 EOG_WAIT_SECONDS 才上报，播报晚 2 分多钟
            if self.pending_game_ids and self.eog_wait_until > now and \
                    phase not in EOG_PHASES and phase not in IN_GAME_PHASES:
                self.eog_wait_until = now
                self.next_history_check = min(self.next_history_check, now)

            if self.update_due(now, phase):
                self.next_update_check, self.update_after_game = now + UPDATE_CHECK_SECONDS, False
                if check_for_update(not self.args.no_update):
                    return  # 已切换到新版本，本进程退出

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


# ----------------------------------------------------------------------------
# 批量导入黑名单（把文件拖到 exe 上）
# ----------------------------------------------------------------------------
IMPORT_BATCH = 25


def load_blacklist_file(path: Path) -> list[dict]:
    """认两种文件：League Akari 的标记玩家导出（只有客户端 puuid + 备注），
    或者已经带 riot_id 的列表 [{"riot_id": "名字#tag", "reason": "…"}]"""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = data.get("data") or data.get("entries") or []
    if not isinstance(data, list):
        raise ValueError("文件里没有记录列表")
    out = []
    for e in data:
        if not isinstance(e, dict):
            continue
        rid = str(e.get("riot_id") or e.get("riotId") or "").strip()
        reason = str(e.get("reason") or e.get("tag") or e.get("note") or "").strip()
        puuid = str(e.get("lcu_puuid") or e.get("puuid") or "").strip()
        if rid or puuid:
            out.append({"riot_id": rid, "reason": reason, "lcu_puuid": puuid})
    return out


def run_blacklist_import(path: Path, companion: "Companion", bot: "BotAPI") -> int:
    print()
    print("=" * 60)
    print(f"批量导入黑名单：{path}")
    print("=" * 60)
    try:
        entries = load_blacklist_file(path)
    except Exception as e:
        logger.error(f"读不了这个文件: {e}")
        return 2
    if not entries:
        logger.error("文件里没有任何记录。")
        return 2
    # 同一个人可能被多个账号标记过：按 puuid / 名字去重，备注合并
    merged: dict[str, dict] = {}
    for e in entries:
        key = (e["lcu_puuid"] or e["riot_id"]).lower()
        m = merged.setdefault(key, {"riot_id": e["riot_id"], "reason": "", "lcu_puuid": e["lcu_puuid"]})
        if e["reason"] and e["reason"] not in m["reason"].split("；"):
            m["reason"] = (m["reason"] + "；" + e["reason"]).strip("；")
        if e["riot_id"] and not m["riot_id"]:
            m["riot_id"] = e["riot_id"]
    entries = list(merged.values())
    need_names = [e for e in entries if not e["riot_id"]]
    logger.info(f"读到 {len(entries)} 个玩家，其中 {len(need_names)} 个只有客户端 puuid，要通过客户端查名字")

    # 名字要靠客户端查：等客户端登录
    if need_names:
        waited = 0
        while companion.lcu is None:
            if companion.connect_lcu():
                break
            if waited == 0:
                logger.info("请打开并登录英雄联盟客户端，最多等 3 分钟…")
            time.sleep(5)
            waited += 5
            if waited >= 180:
                logger.error("一直没等到客户端，放弃。")
                return 2
        failed_names = []
        for i, e in enumerate(need_names, 1):
            try:
                summ = companion.lcu.summoner_by_puuid(e["lcu_puuid"])
                name, tag = summ.get("gameName"), summ.get("tagLine")
            except HttpError as err:
                name, tag = None, f"HTTP {err.status}"
            except Exception as err:
                name, tag = None, str(err)[:60]
            if name and tag:
                e["riot_id"] = f"{name}#{tag}"
                print(f"  [{i}/{len(need_names)}] {name}#{tag}  ← {e['reason'] or '无备注'}")
            else:
                failed_names.append((e["lcu_puuid"], tag))
            time.sleep(0.15)
        if failed_names:
            logger.warning(f"{len(failed_names)} 个玩家在客户端查不到（多半是账号已注销或改区），跳过：")
            for p, why in failed_names:
                print(f"    {p}  {why}")
        entries = [e for e in entries if e["riot_id"]]
    if not entries:
        logger.error("没有一条能用的记录。")
        return 2

    # 选服务器：从机器人拿这个令牌绑定过的服务器
    try:
        info = bot.ping()
    except Exception as e:
        logger.error(f"连不上机器人: {e}")
        return 1
    guilds: dict[str, str] = {}
    for b in info.get("bindings") or []:
        if b.get("guild_id") and b.get("guild_name"):
            guilds.setdefault(str(b["guild_id"]), b["guild_name"])
    if not guilds:
        logger.error("你在机器人上还没绑定任何服务器，先在 Discord 里 /bind。")
        return 2
    ids = list(guilds)
    print()
    print("导入到哪个服务器？（只有你是管理员的服务器才会成功）")
    for i, gid in enumerate(ids, 1):
        print(f"  [{i}] {guilds[gid]}")
    print("  [a] 全部")
    choice = prompt("输入编号（多个用空格分开）或 a，直接回车取消: ").lower()
    if not choice:
        logger.info("已取消。")
        return 0
    if choice == "a":
        chosen = ids
    else:
        chosen = []
        for tok in choice.replace(",", " ").split():
            if tok.isdigit() and 1 <= int(tok) <= len(ids):
                chosen.append(ids[int(tok) - 1])
        if not chosen:
            logger.error("没选中任何服务器。")
            return 2
    logger.info(f"导入到：{'、'.join(guilds[g] for g in chosen)}；共 {len(entries)} 条，分 "
                f"{(len(entries) + IMPORT_BATCH - 1) // IMPORT_BATCH} 批发送（新名字每条要 1.3 秒，已有的很快）")

    totals: dict[str, dict] = {}
    failed: list = []
    denied: list = []
    for start in range(0, len(entries), IMPORT_BATCH):
        batch = entries[start:start + IMPORT_BATCH]
        try:
            resp = bot.post_blacklist_import({"guild_ids": chosen, "entries": batch})
        except HttpError as e:
            try:
                detail = json.loads(e.body)
            except Exception:
                detail = {}
            if detail.get("error") == "no_permission":
                logger.error("机器人拒绝了：你不是所选服务器的管理员。")
                for d in detail.get("denied") or []:
                    print(f"    {d.get('guild_name') or d.get('guild_id')}：{d.get('why')}")
                return 3
            logger.error(f"第 {start // IMPORT_BATCH + 1} 批失败: {e}")
            return 1
        except Exception as e:
            logger.error(f"第 {start // IMPORT_BATCH + 1} 批失败: {e}")
            return 1
        for g in resp.get("guilds") or []:
            t = totals.setdefault(g["guild_id"], {"name": g["guild_name"], "added": 0, "updated": 0, "total": 0})
            t["added"] += int(g.get("added") or 0)
            t["updated"] += int(g.get("updated") or 0)
            t["total"] = int(g.get("total") or t["total"])
        failed.extend(resp.get("failed") or [])
        denied = resp.get("denied") or denied
        print(f"  已发送 {min(start + IMPORT_BATCH, len(entries))}/{len(entries)}")
    print()
    for t in totals.values():
        logger.info(f"{t['name']}：新增 {t['added']}，更新 {t['updated']}，黑名单现在共 {t['total']} 人")
    for d in denied:
        logger.warning(f"{d.get('guild_name') or d.get('guild_id')}：{d.get('why')}")
    if failed:
        logger.warning(f"{len(failed)} 条没导进去：")
        for f in failed:
            print(f"    {f.get('riot_id')}：{f.get('why')}")
    return 0


# ----------------------------------------------------------------------------
# 简中汉化：改 Riot 的语言设置文件，文字和语音都变成简体中文
# ----------------------------------------------------------------------------
ZH_LOCALE = "zh_CN"
LOCALE_CHECK_SECONDS = 3
_LOCALE_RE = re.compile(r'^(settings:[ \t]*\r?\n(?:[ \t]+.*\r?\n)*?[ \t]+locale:[ \t]*)"?([A-Za-z_]*)"?', re.M)
_DEFAULT_LOCALE_RE = re.compile(r'^[ \t]+default_locale:[ \t]*"?([A-Za-z_]+)"?', re.M)


def lol_settings_path() -> Path:
    base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return (Path(base) / "Riot Games" / "Metadata" / "league_of_legends.live"
            / "league_of_legends.live.product_settings.yaml")


def read_game_locale(path: Path | None = None) -> str | None:
    """现在游戏用的语言（zh_CN / en_AU …）；没装游戏或读不出来是 None"""
    try:
        m = _LOCALE_RE.search((path or lol_settings_path()).read_bytes().decode("utf-8"))
        return m.group(2) if m else None
    except Exception:
        return None


def default_game_locale(path: Path | None = None) -> str:
    """这个区服本来的语言（关掉汉化时退回它）"""
    try:
        m = _DEFAULT_LOCALE_RE.search((path or lol_settings_path()).read_bytes().decode("utf-8"))
        return m.group(1) if m else "en_US"
    except Exception:
        return "en_US"


def write_game_locale(locale: str, path: Path | None = None) -> bool:
    """把 settings 下面的 locale 改成 locale。只改这一行，其余原样保留；第一次改之前备份一份"""
    path = path or lol_settings_path()
    raw = path.read_bytes().decode("utf-8")
    new, n = _LOCALE_RE.subn(lambda m: f'{m.group(1)}"{locale}"', raw, count=1)
    if not n:
        raise RuntimeError("语言设置文件里没找到 settings.locale")
    if new == raw:
        return False
    backup = path.with_name(path.name + ".tuoz1.bak")
    if not backup.exists():
        backup.write_bytes(raw.encode("utf-8"))
    try:
        os.chmod(path, 0o666)        # 有的工具会把它设成只读
    except Exception:
        pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(new.encode("utf-8"))
    os.replace(tmp, path)
    return True


def find_riot_client() -> Path | None:
    """RiotClientServices.exe 在哪：先看 Riot 自己记的安装信息，再看游戏装在哪"""
    base = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData") / "Riot Games"
    try:
        info = json.loads((base / "RiotClientInstalls.json").read_text(encoding="utf-8"))
        for key in ("rc_default", "rc_live"):
            if info.get(key) and Path(info[key]).is_file():
                return Path(info[key])
        for p in (info.get("associated_client") or {}).values():
            if p and Path(p).is_file():
                return Path(p)
    except Exception:
        pass
    try:
        m = re.search(r'^product_install_root:[ \t]*"?([^"\r\n]+)"?', lol_settings_path().read_text(encoding="utf-8"), re.M)
        if m:
            p = Path(m.group(1)) / "Riot Client" / "RiotClientServices.exe"
            if p.is_file():
                return p
    except Exception:
        pass
    return None


def launch_lol() -> str:
    """拉起英雄联盟（和桌面快捷方式一样的参数）。返回给窗口看的一句话"""
    exe = find_riot_client()
    if exe is None:
        return "没找到 Riot 客户端，请用平时的方式打开游戏"
    cmd = [str(exe), "--launch-product=league_of_legends", "--launch-patchline=live"]
    # 插件现在通常是管理员身份：游戏不要跟着变成管理员身份，用普通权限去开；这条路走不通再照老办法开
    plain = bool(IS_WINDOWS and process_elevated(os.getpid()) is True and spawn_unelevated(cmd))
    if not plain:
        flags = 0x00000008 | 0x00000200 if IS_WINDOWS else 0   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(cmd, close_fds=True, creationflags=flags)
    logger.info(f"英雄联盟，启动！（{exe}）")
    return "正在打开英雄联盟…"


def league_client_running() -> bool:
    """英雄联盟客户端（大厅）现在开着没有。看进程，不看插件连没连上：
    插件刚启动、还没连上客户端时改了语言，也得知道「这次要重开客户端才生效」"""
    if not IS_WINDOWS:
        return False
    tasklist = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "tasklist.exe")
    if not os.path.exists(tasklist):
        return False
    out = _run_hidden([tasklist, "/FI", "IMAGENAME eq LeagueClientUx.exe", "/NH", "/FO", "CSV"], timeout=10)
    return "leagueclientux.exe" in out.lower()


class LocaleKeeper:
    """汉化开着的时候每 3 秒看一眼语言设置，Riot 客户端（更新 / 修复 / 改设置）改回去了就再改成 zh_CN。
    改的那一刻客户端开着的话，这次不生效、要重开客户端：窗口上一直提示，直到客户端关掉"""

    def __init__(self, config: dict):
        self.config = config
        self.changed_while_client_open = False
        self.last_error = ""
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True, name="locale").start()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("zh_locale"))

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            cur = read_game_locale()
            self.config["zh_locale"] = bool(on)
            if on:
                if cur and cur != ZH_LOCALE and not self.config.get("locale_before"):
                    self.config["locale_before"] = cur      # 关掉汉化时还原成它
                self._apply()
            else:
                back = self.config.pop("locale_before", None) or default_game_locale()
                try:
                    if cur and write_game_locale(back):
                        logger.info(f"已关闭简中汉化，游戏语言恢复成 {back}")
                        self.changed_while_client_open = league_client_running()
                    self.last_error = ""
                except Exception as e:
                    self.last_error = f"恢复语言失败：{e}"
                    logger.warning(self.last_error)
            save_config(self.config)

    def ensure(self) -> None:
        if self.enabled:
            with self._lock:
                self._apply()

    def _apply(self) -> None:
        try:
            cur = read_game_locale()
            if cur is None:
                self.last_error = "没找到英雄联盟的语言设置（游戏装了吗？）"
                return
            if write_game_locale(ZH_LOCALE):
                self.changed_while_client_open = league_client_running()
                logger.info(f"游戏语言 {cur} → {ZH_LOCALE}（简体中文文字和语音）"
                            + ("，客户端开着，重开一次客户端后生效" if self.changed_while_client_open else ""))
            self.last_error = ""
        except Exception as e:
            msg = f"改语言失败：{e}"
            if msg != self.last_error:
                logger.warning(msg)
            self.last_error = msg

    def _loop(self) -> None:
        while True:
            try:
                self.ensure()
                # 客户端关掉了：下次打开读的就是新语言，「要重开」的提示可以撤了
                if self.changed_while_client_open and not league_client_running():
                    self.changed_while_client_open = False
            except Exception:
                pass
            time.sleep(LOCALE_CHECK_SECONDS)

    def status(self) -> str:
        if self.last_error:
            return self.last_error
        cur = read_game_locale()
        if cur is None:
            return "没找到英雄联盟的语言设置（游戏装了吗？）"
        if self.enabled:
            if self.changed_while_client_open:
                return "已改成简体中文，客户端重开一次后生效"
            return "游戏语言：简体中文（第一次会提示下载语音包）" if cur == ZH_LOCALE else "正在改成简体中文…"
        if self.changed_while_client_open:
            return f"已恢复成 {cur}，客户端重开一次后生效"
        return f"游戏语言：{cur}（未汉化）"


# ----------------------------------------------------------------------------
ACCEPT_POLL_SECONDS = 1       # 找到对局的确认框只有 10 秒左右，主循环 3 秒一次太慢，单独一个线程每秒看一眼


class AutoAccepter:
    """自动接受对局：找到对局弹出确认框时替玩家点「接受」。用的是客户端自己的接口（和手点一样），
    已经点过接受 / 拒绝的不再动；关掉开关立刻停"""

    def __init__(self, config: dict, companion):
        self.config, self.companion = config, companion
        self.last_try = 0.0
        threading.Thread(target=self._loop, daemon=True, name="auto-accept").start()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("auto_accept"))

    def set_enabled(self, on: bool) -> None:
        self.config["auto_accept"] = bool(on)
        save_config(self.config)
        logger.info("自动接受对局：已开启" if on else "自动接受对局：已关闭")

    def _loop(self):
        while True:
            time.sleep(ACCEPT_POLL_SECONDS)
            lcu = self.companion.lcu
            if not self.enabled or lcu is None:
                continue
            try:
                self.check(lcu)
            except Exception as e:
                logger.debug(f"自动接受对局：{e}")

    def check(self, lcu) -> bool:
        """确认框开着、自己还没点（接受 / 拒绝都没点）→ 马上替他点接受。点完 3 秒内不重复点"""
        if time.time() - self.last_try < 3:
            return False
        rc = lcu.get("/lol-matchmaking/v1/ready-check", timeout=3) or {}
        if rc.get("state") != "InProgress" or rc.get("playerResponse") != "None":
            return False
        self.last_try = time.time()
        lcu.post("/lol-matchmaking/v1/ready-check/accept", timeout=5)
        logger.info("找到对局，已自动接受")
        return True


# ----------------------------------------------------------------------------
# 海克斯助手（v1.8.0）：海克斯大乱斗三选一出来时（自动发现，或者按热键），截下三张卡的标题发给机器人认，
# 机器人回每张卡的梯队和排名，窗口那边把它标在卡片上方（AramOverlay）。
# 找游戏窗口、标题位置的比例参考了 Nyx0ra/lol-aram-mayhem-hextech-helper（MIT）的 window_layout.py
# ----------------------------------------------------------------------------
ARAM_MODES = {"KIWI", "KIWI_JADE"}          # 海克斯大乱斗的模式名
ARAM_QUEUES = {2400}
ARAM_HOTKEY_DEFAULT = "F6"
ARAM_HOTKEYS = {f"F{n}": 0x6F + n for n in range(1, 13)}        # 配置文件里 aram_hotkey 可以改成 F1–F12
# 三张卡的中线 ÷ 画面宽（按 16:9 的画面算）。标题在卡片里的高低，不同版本 / 分辨率下差着 1% 屏高左右
# （hextech-helper 的截图里标题中心在 40.0% 屏高，Riot 客户端 16.22 实测在 41.1%），
# 所以不截固定的一窄条，而是截高一点的一条（37.2%–45.8%），在里面自己找标题那一行
ARAM_CARD_CENTERS = (0.3115, 0.5, 0.6885)
ARAM_BAND_TOP, ARAM_BAND_W, ARAM_BAND_H = 0.372, 0.135, 0.086
ARAM_COOLDOWN = 0.7                         # 两次手动识别最少隔这么久（按住不放、连按）
ARAM_SCAN_SECONDS = 0.35                    # 自动识别：对局中每隔这么久看一眼三张卡标题的位置
ARAM_RECHECK_SECONDS = 1.5                  # 两次上传最少隔这么久（重掷、鼠标悬停都会让画面变）
_ARAM_WIN = None


def _aram_win():
    """这一节用到的 Windows 接口。单独开一份 user32 / gdi32 并声明参数类型：
    64 位系统上窗口句柄不声明会被截成 32 位；也不影响文件里其它地方对 ctypes.windll 的调用"""
    global _ARAM_WIN
    if _ARAM_WIN is None:
        import ctypes
        from ctypes import wintypes
        import types
        u, g = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("gdi32", use_last_error=True)

        def bind(dll, name, res, *args):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = res, list(args)
            return fn

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]
        ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        H, B, I, U = wintypes.HWND, wintypes.BOOL, ctypes.c_int, wintypes.UINT
        HDC = wintypes.HDC
        _ARAM_WIN = types.SimpleNamespace(
            ctypes=ctypes, wintypes=wintypes, BITMAPINFOHEADER=BITMAPINFOHEADER,
            FindWindowW=bind(u, "FindWindowW", H, wintypes.LPCWSTR, wintypes.LPCWSTR),
            GetForegroundWindow=bind(u, "GetForegroundWindow", H),
            GetAsyncKeyState=bind(u, "GetAsyncKeyState", ctypes.c_short, I),
            IsIconic=bind(u, "IsIconic", B, H), IsWindowVisible=bind(u, "IsWindowVisible", B, H),
            GetClientRect=bind(u, "GetClientRect", B, H, ctypes.POINTER(wintypes.RECT)),
            ClientToScreen=bind(u, "ClientToScreen", B, H, ctypes.POINTER(wintypes.POINT)),
            GetDC=bind(u, "GetDC", HDC, H), ReleaseDC=bind(u, "ReleaseDC", I, H, HDC),
            RegisterHotKey=bind(u, "RegisterHotKey", B, H, I, U, U),
            UnregisterHotKey=bind(u, "UnregisterHotKey", B, H, I),
            PeekMessageW=bind(u, "PeekMessageW", B, ctypes.POINTER(wintypes.MSG), H, U, U, U),
            GetAncestor=bind(u, "GetAncestor", H, H, U),
            GetWindowRect=bind(u, "GetWindowRect", B, H, ctypes.POINTER(wintypes.RECT)),
            GetClassNameW=bind(u, "GetClassNameW", I, H, wintypes.LPWSTR, I),
            ENUMPROC=ENUMPROC, EnumWindows=bind(u, "EnumWindows", B, ENUMPROC, wintypes.LPARAM),
            MonitorFromRect=bind(u, "MonitorFromRect", wintypes.HANDLE, ctypes.POINTER(wintypes.RECT), wintypes.DWORD),
            GetMonitorInfoW=bind(u, "GetMonitorInfoW", B, wintypes.HANDLE, ctypes.c_void_p), MONITORINFO=MONITORINFO,
            GetWindowLongW=bind(u, "GetWindowLongW", wintypes.LONG, H, I),
            SetWindowLongW=bind(u, "SetWindowLongW", wintypes.LONG, H, I, wintypes.LONG),
            SetWindowPos=bind(u, "SetWindowPos", B, H, H, I, I, I, I, U),
            CreateCompatibleDC=bind(g, "CreateCompatibleDC", HDC, HDC),
            CreateCompatibleBitmap=bind(g, "CreateCompatibleBitmap", wintypes.HBITMAP, HDC, I, I),
            SelectObject=bind(g, "SelectObject", wintypes.HGDIOBJ, HDC, wintypes.HGDIOBJ),
            BitBlt=bind(g, "BitBlt", B, HDC, I, I, I, I, HDC, I, I, wintypes.DWORD),
            GetDIBits=bind(g, "GetDIBits", I, HDC, wintypes.HBITMAP, U, U, ctypes.c_void_p, ctypes.c_void_p, U),
            DeleteObject=bind(g, "DeleteObject", B, wintypes.HGDIOBJ), DeleteDC=bind(g, "DeleteDC", B, HDC))
    return _ARAM_WIN


def process_elevated(pid: int) -> bool | None:
    """这个进程是不是以管理员身份在跑。True / False；查不了（多半就是因为它权限比我们高）返回 None"""
    import ctypes
    from ctypes import wintypes
    k, a = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("advapi32", use_last_error=True)
    k.OpenProcess.restype, k.OpenProcess.argtypes = wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    handle = k.OpenProcess(0x1000, False, int(pid))           # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        token = wintypes.HANDLE()
        if not a.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):      # TOKEN_QUERY
            return None
        try:
            value, size = wintypes.DWORD(0), wintypes.DWORD(0)
            if not a.GetTokenInformation(token, 20, ctypes.byref(value), 4, ctypes.byref(size)):   # TokenElevation
                return None
            return bool(value.value)
        finally:
            k.CloseHandle(token)
    finally:
        k.CloseHandle(handle)


def game_blocks_keys() -> bool:
    """游戏是不是以比插件高的权限在跑。是的话，游戏窗口在最前面时系统不让插件看到按键（按住 Tab 就没反应）"""
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = find_game_window()
        if not hwnd or process_elevated(os.getpid()) is not False:
            return False                                  # 没在对局里，或者插件自己就是管理员权限：没这个问题
        pid = wintypes.DWORD(0)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # 只在确定是管理员身份时才算：反作弊护着游戏进程，常常查不了（None），查不了不等于它权限比我们高。
        # 查不了的情况由侧栏看实际情况来判断（AramPanel._watch_input）
        return bool(pid.value) and process_elevated(pid.value) is True
    except Exception:
        return False


def find_game_window() -> int:
    """对局的游戏窗口（不是大厅）。没在对局里就是 0"""
    return int(_aram_win().FindWindowW("RiotWindowClass", "League of Legends (TM) Client") or 0)


def game_layout() -> dict | None:
    """游戏窗口在最前面时，返回它画面在屏幕上的位置：
    {"hwnd", "client": (左, 上, 宽, 高), "content": 同样四个数，超宽屏上是中间那块 16:9}。
    不在最前面、最小化了、没开游戏都返回 None——这时既截不到卡片，标注也没地方放"""
    w = _aram_win()
    hwnd = find_game_window()
    if not hwnd or hwnd != int(w.GetForegroundWindow() or 0) or w.IsIconic(hwnd) or not w.IsWindowVisible(hwnd):
        return None
    rect = w.wintypes.RECT()
    if not w.GetClientRect(hwnd, w.ctypes.byref(rect)):
        return None
    a, b = w.wintypes.POINT(rect.left, rect.top), w.wintypes.POINT(rect.right, rect.bottom)
    if not (w.ClientToScreen(hwnd, w.ctypes.byref(a)) and w.ClientToScreen(hwnd, w.ctypes.byref(b))):
        return None
    width, height = b.x - a.x, b.y - a.y
    if width < 320 or height < 200:
        return None
    cw = min(width, round(height * 16 / 9))
    return {"hwnd": hwnd, "client": (a.x, a.y, width, height), "content": (a.x + (width - cw) // 2, a.y, cw, height)}


def aram_band_rect(layout: dict, slot: int) -> tuple[int, int, int, int]:
    """第 slot 张卡（0–2）标题所在的那一条在屏幕上的位置 (左, 上, 宽, 高)。比标题本身高，里面还会有标题下面的小标签"""
    left, top, cw, ch = layout["content"]
    width = max(8, int(cw * ARAM_BAND_W))
    return (left + int(cw * ARAM_CARD_CENTERS[slot]) - width // 2, top + int(ch * ARAM_BAND_TOP),
            width, max(8, int(ch * ARAM_BAND_H)))


def aram_find_title(bgra: bytes, width: int, height: int) -> tuple[int, int] | None:
    """在一条截图里找标题那一行，返回 (上, 下) 两个行号；找不到返回 None。
    卡片里从上到下是：标题（大字）、一排小标签（「伤害」「功能」）、说明文字。标题是这一条里最靠上的、够高的一行亮字"""
    gray = bgra[1::4]
    step = max(1, width // 160)
    rows = [gray[y * width:(y + 1) * width:step] for y in range(height)]
    sample = b"".join(rows[::max(1, height // 40)])
    order = sorted(sample)
    bg, hi = order[len(order) // 2], order[len(order) - 1 - len(order) // 200]
    if hi - bg < 60 or bg > 80:
        return None
    table = bytes(1 if v > bg + (hi - bg) * 0.55 else 0 for v in range(256))
    masks = [r.translate(table) for r in rows]
    sw = len(masks[0])
    # 这一条的两边可能带进卡片的亮边框：从上到下一直亮着的那些列不是字，先去掉（不然每一行都「有亮点」）
    column = [sum(m[x] for m in masks) for x in range(sw)]
    keep = bytes(0 if column[x] > height * 0.6 else 1 for x in range(sw))
    need = max(2, sw // 60)
    on = [sum(a & b for a, b in zip(m, keep)) >= need for m in masks]
    runs, start = [], None
    for y in range(height + 1):
        lit = y < height and on[y]
        if lit and start is None:
            start = y
        elif not lit and start is not None:
            if runs and start - runs[-1][1] <= max(1, height // 45):      # 一个字上下两半之间断了一两行：算同一行
                runs[-1][1] = y
            else:
                runs.append([start, y])
            start = None
    for top, bottom in runs:
        if height * 0.13 <= bottom - top <= height * 0.45:               # 标题字高约占这一条的四分之一；小标签更矮
            return top, bottom
    return None


def aram_title_shot(layout: dict, slot: int) -> tuple[tuple, bool, tuple]:
    """截第 slot 张卡的标题：返回 ((像素, 宽, 高), 像不像卡片标题, 指纹)。
    找到标题行就只留那一行（上下各留一点边），发给机器人的也只是这一行；找不到就整条算作「不像」"""
    left, top, width, height = aram_band_rect(layout, slot)
    raw = grab_screen(left, top, width, height)
    found = aram_find_title(raw, width, height)
    if found is None:
        looks, sig = aram_fingerprint(raw, width, height)
        return (raw, width, height), False, sig
    pad = max(2, int((found[1] - found[0]) * 0.4))
    y0, y1 = max(0, found[0] - pad), min(height, found[1] + pad)
    crop = raw[y0 * width * 4:y1 * width * 4]
    looks, sig = aram_fingerprint(crop, width, y1 - y0)
    return (crop, width, y1 - y0), looks, sig


def grab_screen(left: int, top: int, width: int, height: int) -> bytes:
    """截屏幕上一块，返回 BGRA 像素（从上到下）。只用系统自带的 GDI，游戏要是无边框 / 窗口模式才截得到"""
    w = _aram_win()
    screen = w.GetDC(None)
    mem = w.CreateCompatibleDC(screen)
    bmp = w.CreateCompatibleBitmap(screen, width, height)
    old = w.SelectObject(mem, bmp)
    try:
        if not w.BitBlt(mem, 0, 0, width, height, screen, left, top, 0x00CC0020 | 0x40000000):   # SRCCOPY | CAPTUREBLT
            raise OSError("截屏失败")
        w.SelectObject(mem, old)                 # GetDIBits 要求位图没被选进任何 DC
        hdr = w.BITMAPINFOHEADER(w.ctypes.sizeof(w.BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = w.ctypes.create_string_buffer(width * height * 4)
        if w.GetDIBits(mem, bmp, 0, height, buf, w.ctypes.byref(hdr), 0) != height:
            raise OSError("读取截屏失败")
        return buf.raw
    finally:
        w.SelectObject(mem, old)
        w.DeleteObject(bmp)
        w.DeleteDC(mem)
        w.ReleaseDC(None, screen)


def png_gray(bgra: bytes, width: int, height: int) -> bytes:
    """BGRA 像素 -> 灰度 PNG（只取绿色通道，认字够用，一条标题才几 KB）"""
    import struct
    import zlib
    gray = bgra[1::4]
    rows = b"".join(b"\x00" + gray[y * width:(y + 1) * width] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def aram_fingerprint(bgra: bytes, width: int, height: int) -> tuple[bool, tuple]:
    """一条标题截图的「指纹」：(看着像不像卡片标题, (亮点图, 亮点个数))。
    卡片标题的样子很固定：一大片很暗的卡片底色，正中间一行亮的字。只用来在本机便宜地判断
    「三选一是不是出来了 / 字换没换」，真正认字还是交给机器人。纯 Python，一条不到 1 毫秒"""
    gray = bgra[1::4]
    step = max(1, width // 120)                      # 缩到宽 120 左右再算
    rows = [gray[y * width:(y + 1) * width:step] for y in range(0, height, step)]
    small = b"".join(rows)
    n, sw, sh = len(small), len(rows[0]), len(rows)
    if n < 64:
        return False, (b"", 0)
    order = sorted(small)
    bg, hi = order[n // 2], order[n - 1 - n // 100]  # 底色（中位数）、字的亮度（最亮的 1%）
    thr = bg + (hi - bg) * 0.5
    mask = small.translate(bytes(1 if v > thr else 0 for v in range(256)))      # 每个点：比阈值亮是 1
    total = sum(mask)
    near_bg = sum(small.translate(bytes(1 if abs(v - bg) <= 22 else 0 for v in range(256)))) / n
    looks = hi - bg >= 60 and bg <= 80 and 0.012 <= total / n <= 0.42 and near_bg >= 0.5
    if looks:
        # 标题是居中的一行字：亮点的重心在正中间，最上最下各 1/8 的行基本没有亮点。
        # 地形、技能特效这些亮点位置是随机的。用真实截图校准：标题 12/12 通过，游戏画面别处的误判从 29% 降到 1% 左右
        col_sums = [sum(mask[x::sw]) for x in range(sw)]
        row_sums = [sum(mask[y * sw:(y + 1) * sw]) for y in range(sh)]
        cx = sum(i * c for i, c in enumerate(col_sums)) / total / max(1, sw - 1)
        cy = sum(i * c for i, c in enumerate(row_sums)) / total / max(1, sh - 1)
        k = max(1, sh // 8)
        margin = (sum(row_sums[:k]) + sum(row_sums[-k:])) / total
        looks = 0.38 <= cx <= 0.62 and 0.3 <= cy <= 0.7 and margin <= 0.12
    return looks, (mask, total)


# ----------------------------------------------------------------------------
# 本机认字：三选一的卡片标题在自己电脑上认，不用把截图传给机器人，也不用等网络
# ----------------------------------------------------------------------------
ARAM_OCR_MODEL = "assets/ocr/ch_PP-OCRv4_rec_infer.onnx"
ARAM_LOCAL_RECHECK = 0.3        # 本机认一次只要几十毫秒：两次之间隔这么久就够了
ARAM_TIER_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}
_ARAM_NORM_RE = re.compile(r"[\s·•:：!！?？\-—_'’`\".,，。、()（）\[\]【】<>《》/\\|]+")


def resource_path(rel: str) -> Path:
    """随插件带着的文件在哪：打包成 exe 时在解包目录里，用源码跑时在脚本旁边"""
    return Path(getattr(sys, "_MEIPASS", "") or Path(__file__).resolve().parent) / rel


class LocalOcr:
    """PP-OCRv4 的文字识别模型（中英文），用 onnxruntime 在 CPU 上跑。输入一行字的图，输出是哪些字。
    需要 numpy 和 onnxruntime 两个库：打包好的 exe 里自带；用源码跑要自己 pip install。
    库或模型不在时 ready 一直是 False，插件照旧把截图交给机器人认"""

    def __init__(self):
        self.session = None
        self.error = ""
        self.lock = threading.Lock()
        self.np = None
        self.input = ""
        self.chars: list = []

    @property
    def ready(self) -> bool:
        return self.session is not None

    def load(self) -> bool:
        """加载模型（要零点几秒，别在界面线程里调）。成功返回 True；失败记下原因，以后不再试"""
        with self.lock:
            if self.session is not None:
                return True
            if self.error:
                return False
            try:
                import numpy as np
                import onnxruntime as ort
                path = resource_path(ARAM_OCR_MODEL)
                if not path.exists():
                    raise FileNotFoundError(f"没有模型文件 {path.name}")
                ort.set_default_logger_severity(3)
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = 2        # 只认三小条，别和游戏抢 CPU
                opts.inter_op_num_threads = 1
                session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
                chars = session.get_modelmeta().custom_metadata_map.get("character", "").splitlines()
                if len(chars) < 100:
                    raise RuntimeError("识别模型里没有字表")
                self.np, self.input, self.chars = np, session.get_inputs()[0].name, ["", *chars, " "]   # 0 号是空白
                self.session = session
                return True
            except Exception as e:
                self.error = f"{type(e).__name__} {str(e)[:160]}"
                return False

    def _resize(self, gray, out_w: int):
        """把一行字缩放到高 48、宽 out_w。缩小时按缩小的倍数把取样范围放宽（不然 4K 屏上的字缩下来会缺笔画）"""
        np = self.np

        def weights(src: int, dst: int):
            scale = src / dst
            support = max(1.0, scale)
            centers = (np.arange(dst, dtype=np.float32) + 0.5) * scale
            w = 1.0 - np.abs((np.arange(src, dtype=np.float32)[None, :] + 0.5 - centers[:, None]) / support)
            w = np.clip(w, 0.0, None)
            return w / w.sum(axis=1, keepdims=True)
        h, w = gray.shape
        return weights(h, 48) @ gray.astype(np.float32) @ weights(w, out_w).T

    def line(self, gray) -> tuple[str, float]:
        """gray：二维 uint8 数组，一行字。返回 (文字, 平均把握)"""
        np = self.np
        h, w = gray.shape
        if h < 4 or w < 4:
            return "", 0.0
        rw = max(8, min(1600, int(math.ceil(48 * w / h))))
        img = (self._resize(gray, rw) / 255.0 - 0.5) / 0.5
        canvas = np.zeros((1, 3, 48, max(320, rw)), dtype=np.float32)     # 右边补黑到至少 320 宽，和训练时一样
        canvas[0, :, :, :rw] = img
        probs = self.session.run(None, {self.input: canvas})[0][0]          # (时间步, 字表大小)
        idx, conf = probs.argmax(axis=1).tolist(), probs.max(axis=1).tolist()
        out, scores, prev = [], [], 0
        for i, p_ in zip(idx, conf):
            if i != prev and i != 0:                 # 相邻重复的合并，空白的去掉
                out.append(self.chars[i] if i < len(self.chars) else "")
                scores.append(p_)
            prev = i
        return "".join(out).strip(), (sum(scores) / len(scores) if scores else 0.0)

    def text_lines(self, gray) -> list:
        """把标题条里有字的那几行框出来：字是亮的、卡片底是暗的。返回 [(上, 下, 左, 右)]，从上到下"""
        np = self.np
        h, w = gray.shape
        bg, hi = float(np.median(gray)), float(np.percentile(gray, 99.5))
        if hi - bg < 45:                              # 整条差不多一个颜色：没有字
            return []
        mask = gray > bg + (hi - bg) * 0.5
        rows = mask.sum(axis=1) >= max(2, w // 200)
        runs, start = [], None
        for y in range(h + 1):
            on = y < h and bool(rows[y])
            if on and start is None:
                start = y
            elif not on and start is not None:
                runs.append((start, y))
                start = None
        if not runs:
            return []
        merged = [list(runs[0])]
        for a, b in runs[1:]:                         # 一个字的上下两半中间断一两行，并回去
            if a - merged[-1][1] <= max(1, h // 40):
                merged[-1][1] = b
            else:
                merged.append([a, b])
        tall = max(b - a for a, b in merged)
        boxes = []
        for a, b in merged:
            if b - a < max(6, tall * 0.55):           # 比最高那行矮很多的是杂点、边框
                continue
            cols = np.where(mask[a:b].sum(axis=0) > 0)[0]
            if len(cols) < 4:
                continue
            pad_y, pad_x = max(2, (b - a) // 4), max(4, (b - a) // 2)
            boxes.append((max(0, a - pad_y), min(h, b + pad_y), max(0, int(cols[0]) - pad_x), min(w, int(cols[-1]) + 1 + pad_x)))
        return boxes[:3]

    def read(self, bgra: bytes, width: int, height: int, enough=None) -> list:
        """一条标题截图（BGRA 像素）-> 几种读法 [(文字, 把握)]：按行框出来读的、整条直接读的。哪种对得上名字用哪种。
        enough(文字)：按行读出来的已经够用了（比如和某个名字一字不差）就返回 True，整条那一遍就不读了，省一半时间"""
        np = self.np
        if width < 8 or height < 8 or len(bgra) < width * height * 4:
            return []
        # 只取绿色通道，和传给机器人时一样
        gray = np.ascontiguousarray(np.frombuffer(bgra, dtype=np.uint8, count=width * height * 4).reshape(height, width, 4)[:, :, 1])
        reads = []
        boxes = self.text_lines(gray)
        if boxes:
            parts = [self.line(np.ascontiguousarray(gray[t:b, l:r])) for t, b, l, r in boxes]
            parts = [x for x in parts if x[0]]
            if parts:
                reads.append(("".join(x[0] for x in parts), sum(x[1] for x in parts) / len(parts)))
                if len(parts) > 1:                    # 英文名字折成两行时中间要有空格
                    reads.append((" ".join(x[0] for x in parts), sum(x[1] for x in parts) / len(parts)))
                if enough is not None and any(enough(text) for text, _ in reads):
                    return reads
        whole = self.line(gray)
        if whole[0]:
            reads.append(whole)
        return reads


LOCAL_OCR = LocalOcr()


def ocr_selftest() -> int:
    """--ocr-selftest：打包以后检查认字模型和运行库是不是都带上了、能不能跑。能跑返回 0"""
    started = time.time()
    if not LOCAL_OCR.load():
        print(f"OCR FAIL: {LOCAL_OCR.error}")
        return 1
    reads = LOCAL_OCR.read(bytes(260 * 92 * 4), 260, 92)       # 一条全黑的图：应该什么都读不到
    print(f"OCR OK: {len(LOCAL_OCR.chars)} chars, load {time.time() - started:.2f}s, blank -> {reads}")
    return 0 if not reads else 1


def aram_norm(text) -> str:
    return _ARAM_NORM_RE.sub("", str(text or "")).lower()


def aram_table_index(table: dict, local_names: dict | None = None) -> dict:
    """把机器人给的整张海克斯表整理成认名字用的样子。
    names：[(海克斯编号, "zh" / "en" / "local", 去掉标点的名字, 原样去空格的名字)]；exact：去掉标点的名字 -> names 里的下标。
    local_names：{海克斯编号: 玩家自己客户端里的名字}。机器人只有大陆简体和英文两套名字，别的语言是另一套译名
    （马来西亚简体的「钝击」就是大陆的「大力」），得照玩家客户端里的叫法才对得上；显示时也用这个叫法"""
    cards = {str(k): v for k, v in (table.get("cards") or {}).items() if isinstance(v, dict)}
    names, exact, local = [], {}, {}

    def add(aid, lang, name):
        norm = aram_norm(name)
        if norm:
            exact.setdefault(norm, []).append(len(names))
            names.append((aid, lang, norm, "".join(str(name).split()).lower()))
    for aid, card in cards.items():
        add(aid, "zh", card.get("name_zh"))
        add(aid, "en", card.get("name_en"))
        mine = str((local_names or {}).get(aid) or "").strip()
        if mine and aram_norm(mine) not in (aram_norm(card.get("name_zh")), aram_norm(card.get("name_en"))):
            local[aid] = mine
            add(aid, "local", mine)
    floor = table.get("min_score")
    return {"cards": cards, "names": names, "exact": exact, "local": local,
            "prefer": {aid for aid, card in cards.items() if card.get("source") == "champion"},
            "min_score": float(floor) if isinstance(floor, (int, float)) else 0.6}


def aram_match(text: str, index: dict) -> tuple:
    """读到的字是哪个海克斯：(编号, 相似度, "zh" / "en")；对不上是 (None, 分数, "")。和机器人上是同一套规则"""
    t = aram_norm(text)
    raw = "".join(str(text or "").split()).lower()
    if not t:
        return None, 0.0, ""
    prefer, best = index["prefer"], (None, 0.0, "")
    hits = index["exact"].get(t)
    if hits:                                          # 一字不差（绝大多数时候走这里）
        for i in hits:
            aid, lang, _, name_raw = index["names"][i]
            # 「罪恶快感」和「罪恶快感！」是两个海克斯，去掉标点后一样：连标点都一样的那个优先
            score = 1.0 + (0.01 if name_raw == raw else 0.0) + (0.015 if aid in prefer else 0.0)
            if score > best[1]:
                best = (aid, score, lang)
        return best[0], min(best[1], 1.0), best[2]
    if len(t) < 2:
        return None, 0.0, ""                          # 只读到一个字：不猜
    matcher = difflib.SequenceMatcher()
    matcher.set_seq2(t)
    for aid, lang, norm, _ in index["names"]:
        matcher.set_seq1(norm)
        score = 0.0
        if matcher.real_quick_ratio() >= 0.5 and matcher.quick_ratio() >= 0.5:
            score = matcher.ratio()
        if len(norm) >= 3 and norm in t and len(norm) / len(t) >= 0.5:
            score = max(score, 0.92)                  # 多读了旁边的字（标签、边框当成了字）：名字整个包含在里面就算对上
        if score <= 0:
            continue
        if aid in prefer:
            score += 0.015
        if score > best[1]:
            best = (aid, score, lang)
    if best[1] < index["min_score"]:
        return None, best[1], ""
    return best[0], min(best[1], 1.0), best[2]


def aram_local_cards(reads_per_card: list, index: dict) -> list:
    """三张卡各自的几种读法 -> 和机器人回的一样格式的三张卡的结果（并标出推荐的那张）"""
    cards = []
    for slot, reads in enumerate(reads_per_card):
        card = {"slot": slot, "matched": False, "text": ""}
        if not reads:
            card["error"] = "no_text"
            cards.append(card)
            continue
        best = (None, 0.0, "", "")
        for text, _conf in reads:
            aid, score, lang = aram_match(text, index)
            if aid and score > best[1]:
                best = (aid, score, lang, text)
        card["text"] = (best[3] or reads[0][0])[:40]
        if not best[0]:
            card["error"] = "unknown_name"
            cards.append(card)
            continue
        data = dict(index["cards"][best[0]])
        if best[2] == "en":                           # 卡片是英文的：名字和天胡搭档都用英文
            data["name"] = data.get("name_en") or data.get("name")
            if data.get("combos"):
                data["combos"] = [{**c, "with": c.get("with_en") or c.get("with")} for c in data["combos"] if isinstance(c, dict)]
        elif best[2] == "local":                      # 对上的是玩家客户端里的叫法：名字和天胡搭档都用这套叫法
            local = index.get("local") or {}
            data["name"] = local.get(best[0]) or data.get("name")
            if data.get("combos"):
                data["combos"] = [{**c, "with": local.get(str(c.get("with_id"))) or c.get("with")} for c in data["combos"] if isinstance(c, dict)]
        card.update({"matched": True, "score": round(best[1], 3), **data})
        cards.append(card)

    def order(c):
        # 先比梯队；梯队一样时这个英雄自己的数据优先，再比排名
        tier = ARAM_TIER_ORDER.get(str(c.get("tier") or "").upper(), len(ARAM_TIER_ORDER))
        src = {"champion": 0, "global": 1}.get(c.get("source"), 2)
        return tier, src, c["rank"] if isinstance(c.get("rank"), (int, float)) else 10 ** 6
    ranked = sorted((c for c in cards if c["matched"] and c.get("source") != "none"), key=order)
    for c in cards:
        c["best"] = bool(ranked) and c is ranked[0]
        if c["best"]:
            c["trap"] = False                          # 三张里最好的那张不再标陷阱
    return cards


def aram_same(a, b) -> bool:
    """两次截的三条标题是不是同样的字：每一条亮点位置不一样的地方都不到亮点数的四分之一。
    故意比得细：重掷只换一张卡、新旧名字长短差不多时也要看得出来；宁可多重认一次，也不能留着旧的标注"""
    if a is None or b is None or len(a) != len(b):
        return False
    for (mask_a, n_a), (mask_b, n_b) in zip(a, b):
        if len(mask_a) != len(mask_b):
            return False
        diff = bin(int.from_bytes(mask_a, "big") ^ int.from_bytes(mask_b, "big")).count("1")
        if diff > 0.25 * max(1.0, (n_a + n_b) / 2):
            return False
    return True


ARAM_PANEL_HIDE_AT = 40         # 游戏内时间到了这么多秒，攻略侧栏收起（读条可能只有十来秒，AI 还没写完，进了游戏接着看）
ARAM_PEEK_VK = 0x09             # 收起以后按住这个键（Tab，也就是游戏里看计分板的键）再看一眼攻略侧栏
# 海克斯什么时候能选（用来判断「现在有没有待选的」。等级和死没死都从游戏自己的本地只读接口读）：
# - 按等级：第一次开局就能选，之后升到 7、11、15 级各解锁一次；「世界赛」嘉宾把等级上限提到 21 级，21 级再多一次
# - 不按等级：「KDA」嘉宾集满四张卡以后，下一次死亡后可以额外再选一个
# - 一局只会有一个嘉宾，所以一局最多选五次
# - 卡片什么时候出来：解锁以后要等到死亡才出来；或者复活后人还在泉水时正好升到那一级，也会出来。
#   所以会攒着，死一回连选两个（比如 7 级和 11 级的一起选）
# 所以卡片只可能在这几种时候出现在屏幕上，插件只在这些时候才截图去找（见 AramAssistant._may_pick）：
#   开局头几分钟、第一次还没选；人死着，以及复活后一小会儿（还在泉水）；刚升到解锁的那一级；
#   标注过的卡片刚消失不久（可能只是收起来了，或者马上出攒着的下一组）。
# 别的时候卡片不可能在屏幕上，完全不截图。问不到等级 / 死活时退回老办法（一直截），手动按热键随时可用
ARAM_PICK_LEVELS = (1, 7, 11, 15, 21)
ARAM_PICK_MAX = 5               # 一局最多能选几次：普通的四次 + 嘉宾带来的一次（21 级那次，或者 KDA 额外的那次）
ARAM_FOUNTAIN_GRACE = 30        # 复活以后这么多秒内当作人还在泉水（这时升到对应等级，卡片也会出来）
ARAM_GUIDE_MIN_ENEMIES = 3      # 对面至少这么多人才写 AI 建议（和机器人那边的规矩一样）：一个人的自定义对局没有阵容可分析
ARAM_INPUT_KEYS = (0x01, 0x02, 0x09, 0x20, 0x51, 0x57, 0x45, 0x52, 0x44, 0x46)   # 鼠标左右键、Tab、空格、Q W E R D F
ARAM_INPUT_QUIET_SECONDS = 120  # 对局里游戏在最前面累计这么久、上面这些键一次都没看到：多半是系统不让插件看按键
ARAM_CARD_RETRY_WAITS = (4, 10, 20, 40)    # 英雄攻略卡没取到（机器人刚重启时很慢、会超时）：隔这么多秒再试，一共试 5 次
ARAM_PICK_CONFIRM_SECONDS = 60  # 标注过的卡片消失了这么久才算「这一次选完了」（玩家可以把卡片收起来再打开，消失不等于选完）
ARAM_FIRST_PICK_SECONDS = 180   # 开局第一次：从这局第一次问到等级算起，这么久内只要还没选就一直找（之后还没认到过，就不再为它一直截了）
# 海克斯助手下面的细项开关：(配置文件里的键, 窗口上的字)。都默认开；总开关关掉时全部不工作
ARAM_OPTIONS = (("aram_auto", "海克斯选择推荐"),
                ("aram_panel", "选人和读条时的攻略侧栏"),
                ("aram_ai_guide", "AI 开局攻略"))
# 全屏时自动改成无边框不放进细项：默认就开着，只能在配置文件里用 "aram_borderless": false 关
ARAM_PLAYER_GAMES = 20          # 十人战绩：每人看最近这么多局
ARAM_PREMADE_SHARED = 3         # 两个人最近的对局里有这么多局是同一局，就算一起开黑的
ARAM_BOSS_MIN_GAMES, ARAM_BOSS_MIN_WIN = 8, 0.55


def live_clock(lcu) -> float | None:
    """游戏里已经过了多少秒（游戏进程自己的接口，端口 2999）。读条时游戏还没开，问不到，返回 None"""
    try:
        data = _http_json("GET", "https://127.0.0.1:2999/liveclientdata/gamestats", {}, timeout=1.5,
                          ssl_context=getattr(lcu, "ctx", None))
        seconds = (data or {}).get("gameTime")
        return float(seconds) if isinstance(seconds, (int, float)) else None
    except Exception:
        return None


def live_me(lcu) -> dict | None:
    """我现在的状态 {"level": 几级, "dead": 死没死（不知道是 None）}（游戏进程自己的接口，端口 2999）。问不到返回 None"""
    ctx = getattr(lcu, "ctx", None)
    try:
        me = _http_json("GET", "https://127.0.0.1:2999/liveclientdata/activeplayer", {}, timeout=1.5, ssl_context=ctx) or {}
    except Exception:
        return None
    level = me.get("level")
    if not isinstance(level, (int, float)) or level <= 0:
        return None
    dead = None
    try:
        players = _http_json("GET", "https://127.0.0.1:2999/liveclientdata/playerlist", {}, timeout=1.5, ssl_context=ctx)
        dead = live_is_dead(me, players)
    except Exception:
        pass
    return {"level": int(level), "dead": dead}


def live_is_dead(me: dict, players) -> bool | None:
    """在对局名单里找到我自己那一行，看死没死。名单里找不到我（名字对不上）返回 None"""
    mine = {str(me.get(k)).strip().lower() for k in ("riotId", "summonerName", "riotIdGameName") if me.get(k)}
    for row in players if isinstance(players, list) else []:
        if not isinstance(row, dict):
            continue
        theirs = {str(row.get(k)).strip().lower() for k in ("riotId", "summonerName", "riotIdGameName") if row.get(k)}
        if mine & theirs:
            return bool(row.get("isDead")) if isinstance(row.get("isDead"), bool) else None
    return None


def live_level(lcu) -> int | None:
    """我现在几级。问不到返回 None"""
    me = live_me(lcu)
    return me["level"] if me else None


def parse_champ_select(session: dict) -> dict:
    """选人界面的状态：我现在拿的英雄、候选席上有哪些英雄"""
    session = session if isinstance(session, dict) else {}
    local, mine = session.get("localPlayerCellId"), 0
    for m in session.get("myTeam") or []:
        if isinstance(m, dict) and m.get("cellId") == local:
            cid = m.get("championId") or m.get("championPickIntent") or 0
            mine = cid if isinstance(cid, int) else 0
    bench = [b.get("championId") for b in session.get("benchChampions") or []
             if isinstance(b, dict) and isinstance(b.get("championId"), int) and b.get("championId") > 0]
    return {"champion_id": mine, "bench": bench}


def summarize_player(games: list, puuid: str, skip_game_id=None) -> dict:
    """一个人最近的对局 -> 局数、胜率、KDA、每分钟伤害，外加对局编号（用来看谁和谁一起打过）"""
    n = wins = kills = deaths = assists = damage = seconds = 0
    ids = set()
    for g in games or []:
        if not isinstance(g, dict) or g.get("gameId") == skip_game_id:
            continue
        parts = g.get("participants") or []
        idents = g.get("participantIdentities") or []
        pid = next((i.get("participantId") for i in idents if ((i.get("player") or {}).get("puuid")) == puuid), None)
        part = next((x for x in parts if x.get("participantId") == pid), parts[0] if parts else None)
        if not part:
            continue
        st = part.get("stats") or {}
        n += 1
        wins += 1 if st.get("win") else 0
        kills += int(st.get("kills") or 0)
        deaths += int(st.get("deaths") or 0)
        assists += int(st.get("assists") or 0)
        damage += int(st.get("totalDamageDealtToChampions") or 0)
        seconds += int(g.get("gameDuration") or 0)
        if g.get("gameId"):
            ids.add(g["gameId"])
        if n >= ARAM_PLAYER_GAMES:
            break
    return {"games": n, "wins": wins, "win_rate": round(wins / n, 3) if n else None,
            "kda": round((kills + assists) / max(1, deaths), 1) if n else None,
            "dpm": round(damage / (seconds / 60)) if seconds > 0 else None, "game_ids": ids}


def collect_player_stats(lcu, teams: list, game_id=None) -> list:
    """双方十个人的近期战绩，顺序和客户端给的一样（读条界面的卡片也是这个顺序）。
    每人一行：{"team", "slot", "is_me", "champion_id", "games", "win_rate", "kda", "premade", "boss"}。
    premade = 和他一起开黑的一共几个人（0 = 单排）；boss = 这一队里近期打得最凶的那个（「大哥」）。
    只读客户端的战绩接口，和点开别人的战绩页是同一个接口"""
    from concurrent.futures import ThreadPoolExecutor

    def fetch(p: dict) -> dict:
        stats = {"games": 0, "wins": 0, "win_rate": None, "kda": None, "dpm": None, "game_ids": set()}
        if p.get("puuid"):
            try:
                stats = summarize_player(lcu.match_history(p["puuid"], ARAM_PLAYER_GAMES), p["puuid"], game_id)
            except Exception as e:
                logger.debug(f"十人战绩：查 {p['puuid'][:8]} 失败: {e}")
        return {"team": p["team"], "is_me": bool(p.get("is_me")), "champion_id": p.get("champion_id") or 0, **stats}
    with ThreadPoolExecutor(max_workers=5) as pool:
        rows = list(pool.map(fetch, teams))
    for team in (1, 2):
        mates = [r for r in rows if r["team"] == team]
        for slot, r in enumerate(mates):
            r["slot"] = slot
        # 开黑：一起打过够多局的连成一组（甲和乙、乙和丙一起打过，三个人就是一组）
        group = list(range(len(mates)))
        for i in range(len(mates)):
            for j in range(i + 1, len(mates)):
                if len(mates[i]["game_ids"] & mates[j]["game_ids"]) >= ARAM_PREMADE_SHARED:
                    old, new = group[j], group[i]
                    group = [new if x == old else x for x in group]
        for i, r in enumerate(mates):
            size = group.count(group[i])
            r["premade"] = size if size >= 2 else 0
        strong = [r for r in mates if r["games"] >= ARAM_BOSS_MIN_GAMES and (r["win_rate"] or 0) >= ARAM_BOSS_MIN_WIN and r["dpm"]]
        boss = max(strong, key=lambda r: r["dpm"], default=None)
        for r in mates:
            r["boss"] = r is boss
    for r in rows:
        r.pop("game_ids", None)
    return rows


class AramAssistant:
    """海克斯助手的后台部分。只在海克斯大乱斗对局里工作：
    - 自动：每 0.35 秒截一下三张卡标题的位置，在本机判断像不像卡片标题；连着两次都像、画面也稳住了，
      才把这三小条发给机器人认。认出来就显示，之后一直盯着：换了卡（重掷）就重认，卡片没了就收掉
    - 手动：热键（默认 F6）立刻认一次，自动没认出来时用
    结果放进 events 交给窗口线程去画。不读游戏内存，不向游戏发任何按键"""

    def __init__(self, config: dict, companion, bot):
        import queue
        self.config, self.companion, self.bot = config, companion, bot
        self.events = queue.Queue()        # ("busy", layout) / ("cards", layout, [卡片]) / ("msg", layout, 文字) / ("clear",)
        self.info: dict = {}               # 这一局：game_id、模式、队列、我的英雄
        self.info_retry_at = 0.0
        self.registered_vk = 0
        self.register_failed = ""          # 热键被别的程序占了：只提示一次
        self.last_press = 0.0
        self.busy = False
        self.next_scan = 0.0
        self.prev_bits = None              # 上一次扫到的指纹：和这次差不多才算「画面稳住了」
        self.shown_bits = None             # 正显示着的那组卡的指纹；None = 现在没在显示
        self.miss = 0                      # 显示着的时候连着几次不像卡片了
        self.neg_bits = None               # 发上去机器人说不是卡片的那个画面：一样的画面不再发
        self.neg_until = 0.0
        self.neg_streak = 0                # 连着几次白发：越多，下次发得越晚
        # 按等级判断有没有待选的海克斯（见 ARAM_PICK_LEVELS）
        self.slot_game = None              # 下面几个数是哪一局的
        self.level = None                  # 我现在几级；None = 还没问到（那就不按等级卡，照老样子识别）
        self.level_at = 0.0
        self.next_level = 0.0
        self.level_busy = False
        self.unlocked_seen = 0             # 已经解锁到第几次了（只用来写日志）
        self.picks_done = 0                # 已经选完几次
        self.cards_gone_at = None          # 标注过的卡片是什么时候消失的；满 ARAM_PICK_CONFIRM_SECONDS 没再出来才算选完一次
        self.pick_names: set = set()       # 这一次待选的三张叫什么。下次认出来的三张和它一张都不重样 = 上一次已经选掉了，这是新的一次
        self.dead = None                   # 我现在是不是死着（None = 不知道）
        self.fountain_until = 0.0          # 死着、以及复活后一小会儿，都当作在泉水：这段时间一律照常识别
        self.slot_since = 0.0              # 这一局第一次问到等级是什么时候（开局第一次三选一的窗口从这儿算）
        self.unlock_until = 0.0            # 刚升到解锁的那一级：到这个时刻之前都去找卡片（人可能正在泉水）
        self.next_upload = 0.0
        self.same_streak = 0               # 重认了几次结果都没变（鼠标悬停让画面一直在动）：越多，下次重认越晚
        self.auto_off_until = 0.0          # 机器人不支持 / 限流时，自动识别先歇一会儿
        self.last_names: tuple = ()
        self.next_borderless = 0.0         # 下次检查游戏是不是全屏
        self.borderless_warned = False
        self.select_state = None           # 选人界面：我的英雄 + 候选席（变了才通知窗口）
        self.select_mayhem = None          # 这次选人是不是海克斯大乱斗（None = 还没问）
        self.next_select = 0.0
        self.prepared_game = None          # 已经为哪一局准备过读条数据（十人战绩、攻略）
        self._icons: dict = {}             # 英雄编号 -> 头像图的原始内容（从本机客户端读的，一次开机每个英雄只读一回）
        self._icons_busy: set = set()      # 正在读的
        self._icons_retry: dict = {}       # 没读到的：英雄编号 -> 什么时候可以再试
        self._icon_tables: dict = {}       # "item" / "aug" -> {编号: 图标在客户端里的地址}
        self.tables: dict = {}             # 英雄编号 -> 这个英雄的整张海克斯表（本机认字时查它）
        self._client_names: dict | None = None     # {海克斯编号: 玩家自己客户端里的名字}；None = 还没读到
        self.table_off_until = 0.0         # 机器人不支持整表接口（旧版本）：这之前别再问
        self._icons_lock = threading.Lock()
        self._report_timer = None          # 开关变了以后，过几秒向机器人补报一次的定时器
        self._report_lock = threading.Lock()
        self.guide_off_until = 0.0
        self.started_game = None           # 哪一局已经确认开打了
        self.next_clock = 0.0
        self._tiers: dict = {}
        self._tiers_at = self._tiers_retry = 0.0
        self._cards: dict = {}             # 英雄 id -> (时刻, 攻略卡)
        threading.Thread(target=self._loop, daemon=True, name="aram-hotkey").start()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("aram_helper", True))

    def set_enabled(self, on: bool) -> None:
        self.config["aram_helper"] = bool(on)
        save_config(self.config)
        logger.info("海克斯助手：已开启（海克斯大乱斗里三选一时自动标出推荐）" if on else "海克斯助手：已关闭")
        self._report_soon()

    def _report_soon(self) -> None:
        """开关变了：5 秒后告诉机器人一声（连着点几下只报最后一次）。报不上也没关系，下次连机器人时还会报"""
        with self._report_lock:
            if self._report_timer is not None:
                self._report_timer.cancel()

            def work():
                try:
                    self.bot.ping()
                except Exception as e:
                    logger.debug(f"开关状态没报上: {e}")
            self._report_timer = threading.Timer(5.0, work)
            self._report_timer.daemon = True
            self._report_timer.start()

    def option(self, key: str) -> bool:
        """细项开关：没写过就是开"""
        return self.config.get(key, True) is not False

    def set_option(self, key: str, on: bool) -> None:
        self.config[key] = bool(on)
        save_config(self.config)
        label = dict(ARAM_OPTIONS).get(key, key)
        logger.info(f"海克斯助手：「{label}」已{'开启' if on else '关闭'}")
        self._report_soon()

    @property
    def hotkey_name(self) -> str:
        name = str(self.config.get("aram_hotkey") or ARAM_HOTKEY_DEFAULT).upper()
        return name if name in ARAM_HOTKEYS else ARAM_HOTKEY_DEFAULT

    # --- 这一局是不是海克斯大乱斗、我用的什么英雄 ---
    def game_info(self) -> dict | None:
        c = self.companion
        game_id, lcu = c.current_game_id, c.lcu
        if not game_id or lcu is None:        # current_game_id 一进对局就有、对局结束 / 断开就清掉，不用再看阶段：
            return None                       # 阶段要等主循环发完开局通知才更新，那可能要好几秒
        if self.info.get("game_id") == game_id and (self.info.get("champion_id") or time.time() < self.info_retry_at):
            return self.info
        self.info_retry_at = time.time() + 5          # 没读到（客户端忙）就 5 秒后再读，别每轮都问
        info = {"game_id": game_id, "game_mode": "", "queue_id": None, "champion_id": 0, "teams": []}
        try:
            data = (lcu.get("/lol-gameflow/v1/session", timeout=4) or {}).get("gameData") or {}
            queue = data.get("queue") or {}
            info["game_mode"] = str(queue.get("gameMode") or "").upper()
            info["queue_id"] = queue.get("id") if isinstance(queue.get("id"), int) else None
            puuid = c.summoner.get("puuid")
            members = []
            for team_no, team in enumerate((data.get("teamOne") or [], data.get("teamTwo") or []), 1):
                for t in team:
                    if isinstance(t, dict):
                        cid = t.get("championId") if isinstance(t.get("championId"), int) else 0
                        members.append({"puuid": str(t.get("puuid") or ""), "champion_id": cid, "team": team_no,
                                        "is_me": bool(puuid) and t.get("puuid") == puuid})
            # 读条界面上卡片的左右顺序不是 teamOne / teamTwo 的顺序，而是 playerChampionSelections 的顺序
            # （实测一局：这份名单和读条界面、和战绩里的参赛者编号完全一致）。上排是 teamOne，下排是 teamTwo
            by_puuid = {mb["puuid"]: mb for mb in members if mb["puuid"]}
            picks = [x.get("puuid") for x in data.get("playerChampionSelections") or [] if isinstance(x, dict)]
            if len(by_puuid) == len(members) and sorted(picks) == sorted(by_puuid):
                members = [by_puuid[x] for x in picks]
            info["teams"] = sorted(members, key=lambda mb: mb["team"])        # 稳定排序：队内顺序保持上面排好的
            info["champion_id"] = next((mb["champion_id"] for mb in members if mb["is_me"] and mb["champion_id"]), 0)
        except Exception as e:
            logger.debug(f"海克斯助手：读对局信息失败: {e}")
        if info["game_mode"] or info["champion_id"] or self.info.get("game_id") != game_id:
            fresh = self.info.get("game_id") != game_id
            self.info = info
            if fresh and self.enabled and self.is_mayhem(info):
                logger.info(f"海克斯助手：这局是海克斯大乱斗，三选一时会自动标出推荐（游戏要用无边框模式；"
                            f"没出来可以按 {self.hotkey_name}）")
            elif fresh and self.enabled and (info["game_mode"] or info["queue_id"] is not None):
                # 说清楚为什么这局没动静（自定义对局报上来的模式名可能和匹配的不一样，看这行就知道）
                logger.info(f"海克斯助手：这局的模式是 {info['game_mode'] or '（空）'}、队列 {info['queue_id']}，"
                            f"不是海克斯大乱斗，这局不启用")
        return self.info

    @staticmethod
    def is_mayhem(info: dict | None) -> bool:
        return bool(info) and (info.get("game_mode") in ARAM_MODES or info.get("queue_id") in ARAM_QUEUES)

    # --- 主循环：管热键（注册和等消息必须在同一个线程），顺带定时自动扫描 ---
    def _loop(self) -> None:
        w = _aram_win()
        msg = w.wintypes.MSG()
        while True:
            want = 0
            try:
                info = self.game_info()
                want = ARAM_HOTKEYS[self.hotkey_name] if self.enabled and self.is_mayhem(info) else 0
                if want:
                    self._tick_level(info)
                if want != self.registered_vk and not (want and self.register_failed == self.hotkey_name):
                    if self.registered_vk:
                        w.UnregisterHotKey(None, 1)
                        self.registered_vk = 0
                    if want:
                        if w.RegisterHotKey(None, 1, 0x4000, want):          # MOD_NOREPEAT：按住不放只算一次
                            self.registered_vk = want
                        else:
                            self.register_failed = self.hotkey_name          # 这个键这次别再试了；自动识别不受影响
                            logger.info(f"海克斯助手：{self.hotkey_name} 被别的程序占用了，手动识别用不了（自动的照常）。"
                                        f"可以在 companion_config.json 里把 aram_hotkey 改成别的键（F1–F12）")
                if not want:
                    self.register_failed = ""
                    if self.shown_bits is not None or self.prev_bits is not None:
                        self._gone()                                         # 对局结束 / 关掉了开关：收掉标注
                        self.prev_bits = None
                while w.PeekMessageW(w.ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == 0x0312 and msg.wParam == 1:            # WM_HOTKEY
                        self.press()
                if want and time.time() >= self.next_scan:
                    self.next_scan = time.time() + ARAM_SCAN_SECONDS
                    self._scan()
                self._tick_extras()
            except Exception as e:
                logger.debug(f"海克斯助手：{e}")
            time.sleep(0.05 if want else 0.5)

    # ------------------------------------------------------------------------
    # 选人梯队、英雄攻略、AI 开局攻略、十人战绩、无边框检查：都是「准备数据 → 放进 events 给窗口线程显示」
    # ------------------------------------------------------------------------
    def _tick_extras(self) -> None:
        """主循环每圈调用一次，里面各自按自己的节奏干活"""
        c = self.companion
        lcu, phase, now = c.lcu, c.last_phase, time.time()
        if lcu is None:
            if self.select_state is not None or self.prepared_game is not None:
                self.select_state, self.prepared_game, self.select_mayhem = None, None, None
                self.events.put(("select", None))
                self.events.put(("pregame", None))
            return
        # 十人战绩不分模式、有自己的开关；其余几样（无边框、选人梯队、攻略）都算海克斯助手的一部分
        if self.enabled and now >= self.next_borderless and phase not in IN_GAME_PHASES:
            self.next_borderless = now + 60
            threading.Thread(target=self.ensure_borderless, args=(lcu,), daemon=True, name="aram-borderless").start()
        if phase == "ChampSelect" and not c.current_game_id and self.enabled and self.option("aram_panel"):
            if now >= self.next_select:
                self.next_select = now + 1.0
                self._poll_select(lcu)
        elif self.select_state is not None or self.select_mayhem is not None:
            self.select_state, self.select_mayhem = None, None
            self.events.put(("select", None))
        info = self.game_info()
        if info and info.get("teams") and info.get("game_id") != self.prepared_game:
            self.prepared_game = info["game_id"]
            threading.Thread(target=self._prepare_game, args=(lcu, dict(info)), daemon=True, name="aram-pregame").start()
        elif not info and self.prepared_game is not None:
            self.prepared_game = None
            self.events.put(("pregame", None))

    # --- 无边框 ---
    def ensure_borderless(self, lcu) -> str:
        """海克斯标注盖不到独占全屏的游戏上，截图也截不到：发现游戏设成了全屏，就通过客户端把它改成无边框
        （下一局生效）。窗口模式不动。配置文件里 "aram_borderless": false 可以关掉这个检查。
        返回做了什么（给日志和测试看）：off / unknown / ok / changed / failed"""
        if not self.option("aram_borderless"):
            return "off"
        try:
            settings = lcu.get("/lol-game-settings/v1/game-settings", timeout=5) or {}
            general = settings.get("General") if isinstance(settings, dict) else None
            mode = general.get("WindowMode") if isinstance(general, dict) else None
            if not isinstance(mode, int) or isinstance(mode, bool):
                logger.debug("海克斯助手：没读到游戏的显示模式，不动它")
                return "unknown"                  # 客户端返回的结构和预期不一样：宁可不改
            if mode != 0:                         # 0 全屏 / 1 窗口 / 2 无边框
                return "ok"
            lcu.patch("/lol-game-settings/v1/game-settings", {"General": {"WindowMode": 2}}, timeout=5)
            try:
                lcu.post("/lol-game-settings/v1/save", timeout=5)
            except Exception as e:
                logger.debug(f"海克斯助手：保存游戏设置时出错: {e}")
            after = ((lcu.get("/lol-game-settings/v1/game-settings", timeout=5) or {}).get("General") or {}).get("WindowMode")
            if after == 2:
                logger.info("海克斯助手：游戏原来是全屏模式，已经改成无边框（下一局生效）。全屏下标注盖不上去。"
                            "不想让插件改：在 companion_config.json 里加一行 \"aram_borderless\": false")
                return "changed"
            if not self.borderless_warned:
                self.borderless_warned = True
                logger.warning("海克斯助手：游戏是全屏模式，自动改成无边框没成功。请在游戏里按 Esc → 视频 → 窗口模式选「无边框」，"
                               "不然海克斯标注显示不出来")
            return "failed"
        except Exception as e:
            logger.debug(f"海克斯助手：检查显示模式失败: {e}")
            return "failed"

    # --- 机器人那边的数据（梯队表、英雄攻略卡），取一次存着 ---
    def tiers(self) -> dict:
        """全英雄梯队 {英雄 id（字符串）: {"tier", "rank", "win_rate"}}；拿不到返回空表，10 分钟后再试"""
        now = time.time()
        if self._tiers and now - self._tiers_at < 6 * 3600:
            return self._tiers
        if now < self._tiers_retry:
            return self._tiers
        self._tiers_retry = now + 600
        try:
            resp = self.bot.get_aram_tiers()
            if isinstance(resp.get("champions"), dict) and resp["champions"]:
                self._tiers, self._tiers_at = resp["champions"], now
        except Exception as e:
            logger.debug(f"海克斯助手：梯队表没拿到: {e}")
        return self._tiers

    def champion_card(self, champion_id: int) -> dict | None:
        """一个英雄的攻略卡（技能加点、召唤师技能、出装、最强的海克斯）。每个英雄每小时最多问一次机器人"""
        hit = self._cards.get(champion_id)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        try:
            card = self.bot.get_aram_champion(champion_id)
        except Exception as e:
            logger.debug(f"海克斯助手：英雄 {champion_id} 的攻略没拿到: {e}")
            card = None
        if not (isinstance(card, dict) and card.get("ok")):
            card = None
        self._cards[champion_id] = (time.time() if card else time.time() - 3540, card)      # 没拿到的 1 分钟后可以再试
        if len(self._cards) > 60:
            self._cards.pop(next(iter(self._cards)))
        return card

    # --- 选人阶段：我现在的英雄和候选席上每个英雄的梯队 ---
    def _poll_select(self, lcu) -> None:
        try:
            if self.select_mayhem is None:            # 这次选人是不是海克斯大乱斗：问一次就够
                queue = ((lcu.get("/lol-gameflow/v1/session", timeout=3) or {}).get("gameData") or {}).get("queue") or {}
                self.select_mayhem = self.is_mayhem({"game_mode": str(queue.get("gameMode") or "").upper(),
                                                     "queue_id": queue.get("id")})
            if not self.select_mayhem:
                return
            session = lcu.get("/lol-champ-select/v1/session", timeout=3) or {}
        except Exception as e:
            logger.debug(f"海克斯助手：读选人信息失败: {e}")
            return
        state = parse_champ_select(session)
        if state == self.select_state:
            return
        changed_mine = (self.select_state or {}).get("champion_id") != state["champion_id"]
        self.select_state = state
        tiers = self.tiers()

        def tag(cid: int) -> dict:
            row = tiers.get(str(cid)) or {}
            return {"champion_id": cid, "name": self.champion_name(cid), "tier": row.get("tier"), "rank": row.get("rank"),
                    "win_rate": row.get("win_rate")}
        view = {"mine": tag(state["champion_id"]) if state["champion_id"] else None,
                "bench": sorted((tag(i) for i in state["bench"]), key=lambda t: t["rank"] if isinstance(t["rank"], (int, float)) else 999),
                "card": None}
        hit = self._cards.get(state["champion_id"])
        if hit and hit[1]:
            view["card"] = hit[1]
        self.events.put(("select", view))
        if state["champion_id"] and changed_mine and not view["card"]:
            def work(cid=state["champion_id"], base=view):
                card = self.champion_card(cid)
                if card and (self.select_state or {}).get("champion_id") == cid:     # 这会儿没又换英雄
                    self.events.put(("select", {**base, "card": card}))
            threading.Thread(target=work, daemon=True, name="aram-card").start()

    def icon_bytes(self, key) -> bytes | None:
        """一张图标（PNG 原始内容）。key：英雄编号，或者 ("item", 装备编号) / ("aug", 海克斯编号)。
        还没读到就是 None，先调 want_icons 去读"""
        return self._icons.get(key)

    @staticmethod
    def _icon_key_ok(key) -> bool:
        if isinstance(key, int):
            return key > 0
        return isinstance(key, tuple) and len(key) == 2 and key[0] in ("item", "aug") and isinstance(key[1], int) and key[1] > 0

    def _icon_path(self, lcu, key) -> str | None:
        """这张图标在客户端里的地址。装备和海克斯要先各读一次总表，查出每个编号对应哪张图"""
        if isinstance(key, int):
            return f"/lol-game-data/assets/v1/champion-icons/{key}.png"
        kind, ident = key
        table = self._icon_tables.get(kind)
        if table is None:
            path, field = (("/lol-game-data/assets/v1/items.json", "iconPath") if kind == "item"
                           else ("/lol-game-data/assets/v1/cherry-augments.json", "augmentSmallIconPath"))
            rows = lcu.get(path, timeout=8) or []
            table = {r.get("id"): r.get(field) for r in rows
                     if isinstance(r, dict) and str(r.get(field) or "").startswith("/lol-game-data/assets/")}
            self._icon_tables[kind] = table
        return table.get(ident)

    def want_icons(self, keys) -> None:
        """后台去客户端读这些图标（客户端自带的图，不联网）。读到了发一个 ("icons",) 事件，侧栏把占位换成图"""
        now = time.time()
        with self._icons_lock:
            todo = [k for k in dict.fromkeys(keys) if self._icon_key_ok(k) and k not in self._icons
                    and k not in self._icons_busy and now >= self._icons_retry.get(k, 0)]
            self._icons_busy.update(todo)
        if not todo:
            return

        def work():
            got = False
            for key in todo:
                try:
                    lcu = getattr(self.companion, "lcu", None)
                    path = self._icon_path(lcu, key) if lcu else None
                    raw = lcu.get_bytes(path) if path else None
                    if raw:
                        self._icons[key] = raw
                        got = True
                    else:
                        self._icons_retry[key] = time.time() + (20 if lcu else 5)
                except Exception as e:
                    self._icons_retry[key] = time.time() + 20
                    logger.debug(f"图标 {key}：{e}")
                finally:
                    with self._icons_lock:
                        self._icons_busy.discard(key)
            if got:
                self.events.put(("icons",))
        threading.Thread(target=work, daemon=True, name="aram-icons").start()

    def champion_name(self, champion_id) -> str:
        """英雄的名字：梯队表里带中文名；表还没拿到时用客户端给的英文代号"""
        name = (self._tiers.get(str(champion_id)) or {}).get("name")
        return name or (getattr(self.companion, "champions", None) or {}).get(str(champion_id), "")

    # --- 进入对局（读条）：十人战绩，海克斯大乱斗再加攻略卡和 AI 攻略 ---
    def _prepare_game(self, lcu, info: dict) -> None:
        """进对局（读条开始）时准备数据。十人战绩、攻略卡 + AI 攻略各用一条线同时去取，谁先好谁先显示；
        这条线自己留下来盯游戏里的时钟：一有时间了 = 读条结束（收掉读条界面上的战绩标签），
        到 ARAM_PANEL_HIDE_AT 秒再收攻略侧栏"""
        game_id, teams = info["game_id"], info.get("teams") or []
        mine = next((p["team"] for p in teams if p.get("is_me")), None)
        allies = [p["champion_id"] for p in teams if p["team"] == mine and p["champion_id"]]
        enemies = [p["champion_id"] for p in teams if p["team"] != mine and p["champion_id"]]
        want_panel = bool(self.is_mayhem(info) and info.get("champion_id") and self.enabled and self.option("aram_panel"))
        want_guide = want_panel and self.option("aram_ai_guide") and time.time() >= self.guide_off_until
        if want_guide and len(enemies) < ARAM_GUIDE_MIN_ENEMIES:
            want_guide = False
            logger.info(f"海克斯助手：对面不到 {ARAM_GUIDE_MIN_ENEMIES} 个人（自定义 / 练习），这局不写 AI 建议；出装、加点和三选一的标注照常")
        # guide_failed：这局不会有 AI 建议（不写，或者没写成）——侧栏就不显示「AI 分析中」的占位条
        data = {"game_id": game_id, "mayhem": self.is_mayhem(info), "champion_id": info.get("champion_id"), "game_mode": info.get("game_mode") or ("KIWI" if self.is_mayhem(info) else ""),
                "players": None, "card": None, "guide": None, "guide_failed": not want_guide}

        def publish():
            if self.prepared_game == game_id:         # 这会儿还是这一局
                self.events.put(("pregame", dict(data)))

        def players():
            try:
                sizes = [sum(1 for p in teams if p["team"] == n) for n in (1, 2)]
                if sizes == [5, 5] and self.config.get("player_stats", True) is not False:
                    started = time.time()
                    data["players"] = collect_player_stats(lcu, teams, game_id)
                    publish()
                    logger.debug(f"十人战绩：{time.time() - started:.1f} 秒")
            except Exception as e:
                logger.debug(f"十人战绩：{e}")

        def guide():
            try:
                # 一局只问这一次（prepared_game 记着这局问过了，切出去再切回来不会重问）；
                # 机器人那边同一局同一个英雄也只让 AI 写一次，再问给的是存着的那份，不再花钱
                started = time.time()
                resp = self.bot.post_aram_guide({
                    "companion_version": VERSION, "game_id": game_id, "champion_id": info["champion_id"],
                    "allies": allies, "enemies": enemies})
                if resp.get("ok") and resp.get("lines"):
                    data["guide"] = resp["lines"]
                    publish()
                    logger.info(f"海克斯攻略（{time.time() - started:.1f} 秒）：" + "；".join(f"{ln.get('label')}：{ln.get('text')}" for ln in resp["lines"]))
            except HttpError as e:
                if e.status in (404, 503):            # 机器人不支持 / AI 没开 / 超了上限：这次开机别每局都问了
                    self.guide_off_until = time.time() + (3600 if e.status == 404 else 600)
                logger.debug(f"海克斯攻略：机器人回 {e.status}")
            except Exception as e:
                logger.debug(f"海克斯攻略：{e}")
            if not data["guide"]:
                data["guide_failed"] = True           # 让窗口别再显示「AI 分析中」的占位条
                publish()

        def card():
            # 以前只取一次：机器人刚重启时慢、请求超时，这一局的侧栏就整局都没内容，按住 Tab 也没反应
            champion_id = info["champion_id"]
            for attempt, wait in enumerate((0, *ARAM_CARD_RETRY_WAITS)):
                if wait:
                    time.sleep(wait)
                    self._cards.pop(champion_id, None)      # 上一次没取到的记录清掉，不然一分钟内不会真的去问
                if self.prepared_game != game_id:
                    return                                  # 这局已经结束了
                try:
                    got = self.champion_card(champion_id)
                except Exception as e:
                    logger.debug(f"英雄攻略：{e}")
                    got = None
                if got:
                    data["card"] = got
                    publish()
                    if attempt:
                        logger.info(f"海克斯助手：英雄攻略取到了（第 {attempt + 1} 次才成功），侧栏和按住 Tab 可以用了")
                    return
            data["card_failed"] = True
            publish()
            logger.info("海克斯助手：这局的英雄攻略一直没取到（机器人没回应），侧栏和按住 Tab 这局没有内容可显示；三选一的标注不受影响")

        threading.Thread(target=players, daemon=True, name="aram-players").start()
        if data["mayhem"] and info.get("champion_id") and self.enabled and self.option("aram_local_ocr"):
            threading.Thread(target=self._prepare_local, args=(info["champion_id"], game_id), daemon=True,
                             name="aram-table").start()
        if want_guide:
            threading.Thread(target=guide, daemon=True, name="aram-guide").start()     # AI 最慢，最先出发
        if want_panel:
            threading.Thread(target=card, daemon=True, name="aram-card").start()
        loaded, deadline = False, time.time() + 900
        while self.prepared_game == game_id and time.time() < deadline:
            seconds = live_clock(lcu)
            if seconds is not None and not loaded:
                loaded = True
                self.events.put(("loaded", game_id))
            if seconds is not None and seconds >= ARAM_PANEL_HIDE_AT:
                self.started_game = game_id
                self.events.put(("started", game_id))
                return
            time.sleep(1.0 if loaded else 0.7)

    def client_augment_names(self) -> dict:
        """玩家自己客户端里每个海克斯叫什么（客户端当前语言）。读不到返回空表，下一局再试"""
        if self._client_names is not None:
            return self._client_names
        try:
            lcu = getattr(self.companion, "lcu", None)
            rows = lcu.get("/lol-game-data/assets/v1/cherry-augments.json", timeout=8) if lcu else None
            names = {str(r.get("id")): str(r.get("nameTRA")).strip() for r in rows or []
                     if isinstance(r, dict) and isinstance(r.get("id"), int) and r.get("nameTRA")}
            if names:
                self._client_names = names
                return names
        except Exception as e:
            logger.debug(f"海克斯助手：没读到客户端里的海克斯名字（{e}）")
        return {}

    def _prepare_local(self, champion_id: int, game_id) -> None:
        """为本机认字做准备（读条时做，三选一出来时就都齐了）：加载认字模型，取这个英雄的整张海克斯表。
        哪样没成都没关系：到时候照旧把截图交给机器人认"""
        try:
            if not LOCAL_OCR.load():
                logger.debug(f"海克斯助手：本机认字用不了（{LOCAL_OCR.error}），照旧交给机器人认")
                return
            if champion_id in self.tables or time.time() < self.table_off_until:
                return
            resp = self.bot.get_aram_table(champion_id, game_id)
            if resp.get("ok") and resp.get("cards"):
                if len(self.tables) > 8:
                    self.tables.pop(next(iter(self.tables)))
                index = aram_table_index(resp, self.client_augment_names())
                self.tables[champion_id] = index
                extra = f"（另有 {len(index['local'])} 个按你客户端里的叫法来认）" if index["local"] else ""
                logger.info("海克斯助手：这局在本机认卡片，不用上传截图" + extra)
        except HttpError as e:
            if e.status == 404:                       # 机器人还是旧版本，没有这个接口
                self.table_off_until = time.time() + 3600
            logger.debug(f"海克斯助手：没取到整张海克斯表（机器人回 {e.status}），照旧交给机器人认")
        except Exception as e:
            logger.debug(f"海克斯助手：没取到整张海克斯表（{e}），照旧交给机器人认")

    def local_index(self, champion_id=None) -> dict | None:
        """这局能不能在本机认：模型加载好了、这个英雄的表也取到了，就返回那张表；否则 None"""
        if not LOCAL_OCR.ready or not self.option("aram_local_ocr"):
            return None
        if champion_id is None:
            champion_id = (self.game_info() or {}).get("champion_id")
        return self.tables.get(champion_id)

    @staticmethod
    def _shoot(layout: dict) -> tuple[list, tuple, int]:
        """截三张卡的标题。返回 ([(像素, 宽, 高)], 三条的指纹, 有几条看着像卡片标题)"""
        shots, bits, looks = [], [], 0
        for slot in range(3):
            shot, ok, sig = aram_title_shot(layout, slot)
            shots.append(shot)
            bits.append(sig)
            looks += bool(ok)
        return shots, tuple(bits), looks

    def _gone(self) -> None:
        if self.shown_bits is not None:
            self.events.put(("clear",))
            if self.cards_gone_at is None:
                self.cards_gone_at = time.time()      # 标注过的卡片没了：可能是选完了，也可能只是收起来了，先记下时刻
        self.shown_bits, self.miss, self.last_names, self.same_streak = None, 0, (), 0

    def _tick_level(self, info: dict | None) -> None:
        """对局中每秒问一次等级和死没死（另起一条线去问，问不到也不卡热键和扫描）"""
        now, game_id = time.time(), (info or {}).get("game_id")
        if game_id != self.slot_game:                 # 换了一局：从头数
            self.slot_game, self.level, self.level_at = game_id, None, 0.0
            self.unlocked_seen, self.picks_done, self.cards_gone_at = 0, 0, None
            self.pick_names, self.dead, self.fountain_until = set(), None, 0.0
            self.slot_since, self.unlock_until = 0.0, 0.0
        if now < self.next_level or self.level_busy:
            return
        self.next_level, self.level_busy = now + 1.0, True

        def work():
            try:
                me = live_me(getattr(self.companion, "lcu", None))
                if me is not None and self.slot_game == game_id:
                    level = me["level"]
                    self.level, self.level_at = level, time.time()
                    if not self.slot_since:
                        self.slot_since = self.level_at
                    self.dead = me["dead"]
                    if self.dead:
                        self.fountain_until = self.level_at + ARAM_FOUNTAIN_GRACE
                    unlocked = sum(1 for lv in ARAM_PICK_LEVELS if level >= lv)
                    if unlocked > self.unlocked_seen:
                        if self.unlocked_seen:        # 开局那一次不用说
                            logger.info(f"海克斯助手：升到 {level} 级，第 {unlocked} 次海克斯可以选了")
                            self.unlock_until = self.level_at + ARAM_FOUNTAIN_GRACE
                        self.unlocked_seen = unlocked
            finally:
                self.level_busy = False
        threading.Thread(target=work, daemon=True, name="aram-level").start()

    def _pick_done(self, why: str) -> None:
        """记一次「选完了」。最多记到一局的上限，数错了也不会越记越多"""
        self.picks_done = min(self.picks_done + 1, ARAM_PICK_MAX)
        self.cards_gone_at, self.pick_names = None, set()
        logger.debug(f"海克斯助手：第 {self.picks_done} 次海克斯算选完了（{why}）")

    def _note_cards(self, cards: list) -> None:
        """认出一组卡片时调用：判断这是不是新的一次三选一。
        重掷只换一张，收起来再打开一张都不变；三张全换了，就是上一次已经选掉、接着选攒下的下一个"""
        names = {str(c.get("name")) for c in cards if c.get("matched") and c.get("name")}
        if not names:
            return
        if self.pick_names and not (names & self.pick_names):
            self._pick_done("换了一组新的卡")
        self.pick_names = names
        self.cards_gone_at = None                     # 卡片还在：这一次还没选完

    def _may_pick(self, now: float) -> bool:
        """这会儿三选一的卡片有没有可能在屏幕上（标注没显示着的时候才问）。False = 不可能，不用截图。
        规则见 ARAM_PICK_LEVELS 上面那段说明。拿不准的一律算「有可能」"""
        if self.cards_gone_at is not None:
            if now - self.cards_gone_at < ARAM_PICK_CONFIRM_SECONDS:
                return True                           # 标注过的卡片刚消失：可能只是收起来了，或者马上出攒着的下一组
            self._pick_done("卡片消失够久了")
        if self.level is None or now - self.level_at > 15:
            return True                               # 读条中，或者游戏的接口问不到：退回老办法
        if self.dead is None:                         # 等级问得到、死没死问不到：只能按等级数还欠不欠
            return sum(1 for lv in ARAM_PICK_LEVELS if self.level >= lv) > self.picks_done
        if self.dead or now < self.fountain_until or now < self.unlock_until:
            return True                               # 死着 / 刚复活还在泉水 / 刚升到解锁的那一级
        return self.picks_done == 0 and now - self.slot_since < ARAM_FIRST_PICK_SECONDS     # 开局第一次

    def _scan(self) -> None:
        """自动识别的一步"""
        layout = game_layout()
        if layout is None:                    # 切出去了：标注对不上位置，收掉；回来以后重新认
            self._gone()
            self.prev_bits = None
            return
        if _aram_win().GetAsyncKeyState(ARAM_PEEK_VK) & 0x8000:
            # 按着 Tab：计分板盖在画面上，上面的数字会被当成卡片标题。这会儿不截也不认，已经显示的标注原样留着
            self.prev_bits = None
            return
        now = time.time()
        if self.shown_bits is None and not self._may_pick(now):
            self.prev_bits = None                 # 这会儿卡片不可能在屏幕上（人活着在外面，也没有刚解锁的）：不截图
            return
        shots, bits, looks = self._shoot(layout)
        stable = aram_same(bits, self.prev_bits)
        self.prev_bits = bits
        if self.shown_bits is not None:
            # 正显示着：只要还有两张像卡片就留着（鼠标悬停的那张会发光、变样）；连着两次都不像了才是选完了
            if looks < 2:
                self.miss += 1
                if self.miss >= 2:
                    self._gone()
                return
            self.miss = 0
            if stable and not aram_same(bits, self.shown_bits) and now >= self.next_upload and not self.busy:
                self._start(layout, shots, bits, auto=True)       # 重掷换了卡：重认一次，认出来之前旧的标注先留着
            return
        if not self.option("aram_auto"):
            return                                # 自动标注关了：只有按热键才认（认出来以后上面那段照样盯着卡片）
        local = self.local_index() is not None
        # 交给机器人认的时候要等画面停稳了再发（一来一回不便宜）；本机认只要几十毫秒，卡片还在翻就可以先认着，
        # 不过没停稳时要三张都认出来才显示（见 _recognize 的 strict）
        if looks < 3 or (not stable and not local) or self.busy or now < self.next_upload or now < self.auto_off_until:
            return
        if self.neg_bits is not None and now < self.neg_until and aram_same(bits, self.neg_bits):
            return                                # 这个画面刚认过，不是卡片
        self._start(layout, shots, bits, auto=True, strict=not stable)

    def press(self) -> None:
        """热键：马上认一次"""
        now = time.time()
        if self.busy or now - self.last_press < ARAM_COOLDOWN:
            return
        self.last_press = now
        layout = game_layout()
        if layout is None:
            logger.info(f"海克斯助手：按了 {self.hotkey_name}，但游戏窗口不在最前面，没法识别")
            return                            # 游戏不在最前面：截不到，也没地方显示
        shots, bits, looks = self._shoot(layout)
        logger.info(f"海克斯助手：按了 {self.hotkey_name}，三张卡里有 {looks} 张看着像标题，正在识别")
        self._start(layout, shots, bits, auto=False)

    def _start(self, layout: dict, shots: list, bits: tuple, auto: bool, strict: bool = False) -> None:
        self.busy = True
        self.next_upload = time.time() + (ARAM_LOCAL_RECHECK if self.local_index() is not None else ARAM_RECHECK_SECONDS)
        threading.Thread(target=self._recognize, args=(layout, shots, bits, auto, strict), daemon=True,
                         name="aram-recognize").start()

    def _recognize(self, layout: dict, shots: list, bits: tuple, auto: bool, strict: bool = False) -> None:
        """认三条标题：能在本机认就在本机认，否则发给机器人。auto=True 是自动扫到的：认不出来就悄悄算了，
        不在游戏画面上弹提示。strict=True 是画面还没停稳时认的：三张都认出来才显示，免得翻到一半的卡标成「没认出来」"""
        try:
            info = self.game_info() or {}
            if not info.get("champion_id"):
                if not auto:
                    self.events.put(("msg", layout, "还没读到你这局的英雄，过几秒再按"))
                return
            if not auto:
                self.events.put(("busy", layout))
            started = time.time()
            index, local, cards = self.local_index(info["champion_id"]), False, []
            if index is not None:
                try:
                    exact = index["exact"]
                    cards = aram_local_cards([LOCAL_OCR.read(raw, width, height, lambda text: aram_norm(text) in exact)
                                              for raw, width, height in shots], index)
                    local = True
                except Exception as e:                # 本机认出了岔子：这一次交给机器人
                    logger.debug(f"海克斯助手：本机认字出错（{e}），这次交给机器人")
            if not local:
                images = [base64.b64encode(png_gray(raw, width, height)).decode() for raw, width, height in shots]
                resp = self.bot.post_aram_augments({
                    "companion_version": VERSION, "game_id": info.get("game_id"), "champion_id": info["champion_id"],
                    "game_mode": info.get("game_mode"), "queue_id": info.get("queue_id"),
                    "screen": list(layout["client"][2:]), "auto": auto, "images": images})
                cards = [c for c in resp.get("cards") or [] if isinstance(c, dict)]
            matched = sum(1 for c in cards if c.get("matched"))
            where = "本机" if local else "机器人"
            if matched >= (3 if auto and strict else 2 if auto else 1):
                self.events.put(("cards", layout, cards))
                self.shown_bits, self.miss = bits, 0
                self._note_cards(cards)               # 还是同一次（重掷、收起又打开），还是已经换成下一次了
                self.neg_bits, self.neg_streak = None, 0
                names = tuple(aram_card_line(c) for c in cards)
                if names != self.last_names:
                    self.last_names, self.same_streak = names, 0
                    logger.info("海克斯：" + " / ".join(names) + f"（{where} {int((time.time() - started) * 1000)} 毫秒）")
                else:                                     # 画面动了但卡没换（多半是鼠标悬停）：下次重认往后推，最多隔 6 秒（本机认的 1.5 秒）
                    self.same_streak += 1
                    self.next_upload = time.time() + (min(1.5, ARAM_LOCAL_RECHECK * 2 ** min(self.same_streak, 3)) if local
                                                      else min(6.0, ARAM_RECHECK_SECONDS * 2 ** min(self.same_streak, 3)))
            elif auto:
                logger.debug(f"海克斯助手：自动认了一次（{where}），没对上名字：" + " / ".join(str(c.get("text") or "") for c in cards))
                # 本机觉得像、机器人认不出名字：多半不是海克斯三选一（或者卡片还在翻）。记下这个画面，越是连着白发越晚再发。
                # 三张都读到了字、却一张都对不上的，是别的三选一（属性碎片）或商店：这个画面一分钟内不再问，别的画面也多等一会儿
                wrong = matched == 0 and sum(1 for c in cards if c.get("text")) >= 2
                if local:
                    # 本机认的不花网络：只记住「这个画面不是」，别的画面马上可以再认（卡片翻到一半时就是这样，下一眼就认出来了）
                    self.neg_bits, self.neg_until = bits, time.time() + (60 if wrong else 3)
                    self.next_upload = time.time() + ARAM_LOCAL_RECHECK
                else:
                    self.neg_bits, self.neg_until = bits, time.time() + (60 if wrong else 30)
                    self.neg_streak += 2 if wrong else 1
                    self.next_upload = time.time() + min(15.0, ARAM_RECHECK_SECONDS * 2 ** min(self.neg_streak, 4))
            elif matched == 0 and sum(1 for c in cards if c.get("text")) >= 2:
                logger.info("海克斯助手：这三张不是海克斯（读到的是 " + " / ".join(str(c.get("text") or "") for c in cards) + "）")
                self.events.put(("msg", layout, "这三张不是海克斯，没有推荐"))
            elif any(c.get("text") for c in cards):
                logger.info("海克斯助手：读到了字但对不上海克斯的名字：" + " / ".join(str(c.get("text") or "") for c in cards))
                self.events.put(("cards", layout, cards))     # 读到字了但对不上：把读到的显示出来
                self.shown_bits, self.miss = bits, 0
            else:
                logger.info("海克斯助手：机器人没在截图里读到字")
                self.events.put(("msg", layout, "没看到海克斯卡片，等卡片翻开再按一次"))
        except HttpError as e:
            text = {401: "令牌失效了，看插件窗口的提示", 404: "机器人还不支持海克斯助手",
                    429: "识别得太频繁了，等几秒", 503: "机器人暂时认不了，稍后再试"}.get(e.status, f"机器人出错（{e.status}）")
            if auto:
                # 自动的不往游戏画面上弹错误；这类错误短时间内再试也一样，歇一会儿（期间按热键仍然可以手动试）
                pause = 600 if e.status in (401, 404) else 20
                if time.time() >= self.auto_off_until:
                    logger.info(f"海克斯助手：{text}，自动识别先停 {pause} 秒")
                self.auto_off_until = time.time() + pause
            else:
                logger.info(f"海克斯助手：{text}")
                self.events.put(("msg", layout, text))
        except Exception as e:
            if auto:
                logger.debug(f"海克斯助手：没连上机器人（{e}）")
                self.auto_off_until = time.time() + 10
            else:
                logger.info(f"海克斯助手：没连上机器人（{e}）")
                self.events.put(("msg", layout, "没连上机器人，稍后再按"))
        finally:
            self.busy = False


def aram_card_text(card: dict) -> str:
    """一张卡在标注条上的字（梯队字母另外用大字显示）"""
    if not card.get("matched"):
        text = str(card.get("text") or "").strip()
        return f"{text[:8]} · 没有数据" if text else "没认出来"
    rank, rarity_rank = card.get("rank"), card.get("rarity_rank")
    if not isinstance(rank, (int, float)):
        return "没有统计数据"
    text = ("总第 " if card.get("source") == "champion" else "全英雄第 ") + str(int(rank))
    if isinstance(rarity_rank, (int, float)) and card.get("rarity_label"):
        text += f" · {card['rarity_label']}第 {int(rarity_rank)}"
    return text


def decode_png(raw: bytes):
    """把 8 位、不隔行的 RGB / RGBA PNG 解成 (宽, 高, 每像素几个字节, 像素)。别的格式返回 None。
    客户端的英雄头像、装备图标、海克斯图标都是这种；用它是因为 Tk 自己读图拿不到透明度"""
    import struct
    import zlib
    if raw[:8] != bytes([137, 80, 78, 71, 13, 10, 26, 10]):
        return None
    pos, idat, head = 8, [], None
    while pos + 8 <= len(raw):
        length, kind = struct.unpack(">I4s", raw[pos:pos + 8])
        body = raw[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    if not head:
        return None
    width, height, depth, ctype, _, _, interlace = head
    if depth != 8 or ctype not in (2, 6) or interlace or not 0 < width <= 512 or not 0 < height <= 512:
        return None
    bpp = 3 if ctype == 2 else 4
    data, stride = zlib.decompress(b"".join(idat)), width * bpp
    if len(data) < height * (stride + 1):
        return None
    out, prev, p = bytearray(height * stride), bytearray(stride), 0
    for y in range(height):
        kind, line = data[p], bytearray(data[p + 1:p + 1 + stride])
        p += 1 + stride
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif kind == 3:
            for i in range(stride):
                line[i] = (line[i] + (((line[i - bpp] if i >= bpp else 0) + prev[i]) >> 1)) & 255
        elif kind == 4:
            for i in range(stride):
                a, b, c = (line[i - bpp] if i >= bpp else 0), prev[i], (prev[i - bpp] if i >= bpp else 0)
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, bpp, bytes(out)


def shrink_icon(raw: bytes, size: int, cut: float = 0.0, bg: str = "#0A1428"):
    """把一张图标缩成 size 像素见方，返回可以直接喂给 Tk 的一行行颜色；解不了返回 None。
    cut：四周各裁掉多少（英雄头像有一圈黑边）；透明的地方垫上 bg。按块取平均，不会有锯齿"""
    decoded = decode_png(raw)
    if not decoded:
        return None
    width, height, bpp, px = decoded
    edge = int(min(width, height) * cut)
    x0, y0, sw, sh = edge, edge, width - 2 * edge, height - 2 * edge
    if sw <= 0 or sh <= 0:
        return None
    br, bgc, bb = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
    rows = []
    for dy in range(size):
        ya, yb = y0 + dy * sh // size, y0 + max(dy * sh // size + 1, (dy + 1) * sh // size)
        line = []
        for dx in range(size):
            xa, xb = x0 + dx * sw // size, x0 + max(dx * sw // size + 1, (dx + 1) * sw // size)
            r = g = b = alpha = 0
            for y in range(ya, yb):
                base = (y * width + xa) * bpp
                for i in range(base, base + (xb - xa) * bpp, bpp):
                    a = px[i + 3] if bpp == 4 else 255
                    r, g, b, alpha = r + px[i] * a, g + px[i + 1] * a, b + px[i + 2] * a, alpha + a
            full = (yb - ya) * (xb - xa) * 255
            rest = full - alpha
            line.append(f"#{(r + br * rest) // full:02x}{(g + bgc * rest) // full:02x}{(b + bb * rest) // full:02x}")
        rows.append("{" + " ".join(line) + "}")
    return rows


def aram_card_title(card: dict) -> str:
    """标注条第一行：认出来的海克斯名字（玩家能和卡片对一眼，认错了看得出来）。没认出来的照旧写读到了什么"""
    if card.get("matched") and card.get("name"):
        return str(card["name"])
    return aram_card_text(card)


def aram_card_stats(card: dict, short: bool = False) -> tuple[str, str]:
    """标注条第二行：排名、胜率、比这个英雄的平均高低多少、场次。返回 (文字, 颜色)；没认出来返回 ("", "")。
    short=True 省掉「比平均」三个字（放不下时用）"""
    if not card.get("matched"):
        return "", ""
    win, games, delta, rank = card.get("win_rate"), card.get("games"), card.get("delta"), card.get("rank")
    world = card.get("source") == "global"      # 这个英雄没有这张的数据，用的是全英雄的统计：和英雄自己的胜率不是一把尺子，要写明白
    parts = []
    if isinstance(rank, (int, float)):
        parts.append(("全英雄第 " if world else "第 ") + str(int(rank)))
    if isinstance(win, (int, float)):
        parts.append(f"胜率 {win * 100:.1f}%")
    if isinstance(delta, (int, float)):
        parts.append(("" if short else "比平均 ") + f"{delta * 100:+.1f}%")
    if isinstance(games, (int, float)) and games > 0:
        parts.append(f"{games / 10000:.1f} 万场" if games >= 10000 else f"{int(games)} 场")
    if card.get("low_sample"):
        parts.append("样本少")
    if not parts:
        return "没有统计数据", HEX["muted"]
    color = HEX["muted"]
    if isinstance(delta, (int, float)) and not card.get("low_sample"):
        color = HEX["ok"] if delta >= 0.02 else HEX["bad"] if delta <= -0.02 else HEX["muted"]
    return " · ".join(parts), color


def aram_card_combo(card: dict, short: bool = False) -> str:
    """标注条第三行：这张和哪张一起拿是天胡组合。short=True 不带场次（长名字放不下时用）"""
    combos = [c for c in card.get("combos") or [] if isinstance(c, dict) and c.get("with")]
    if not card.get("matched") or not combos:
        return ""
    best = combos[0]
    text = f"天胡 + {best['with']}"
    if isinstance(best.get("win_rate"), (int, float)):
        text += f" {best['win_rate'] * 100:.1f}%"
    if not short and isinstance(best.get("games"), (int, float)):
        text += f"（{int(best['games'])} 场）"
    return text


def aram_card_line(card: dict) -> str:
    """日志里的一张卡"""
    if not card.get("matched"):
        return f"？{card.get('text') or ''}"
    return f"{card.get('name')} {card.get('tier') or '-'}" + ("（推荐）" if card.get("best") else "（陷阱）" if card.get("trap") else "")


# 窗口：海克斯金色主题。插件本身照常在后台线程里跑，窗口只负责显示和启动游戏
# ----------------------------------------------------------------------------
HEX = {"bg": "#0A1428", "title": "#010A13", "gold": "#C8AA6E", "gold_hi": "#F0E6D2", "text": "#F0E6D2",
       "muted": "#A09B8C", "btn": "#1E2328", "btn_hover": "#1E282D", "btn_down": "#111820", "line": "#3C3C41",
       "ok": "#0AC8B9", "bad": "#E84057", "log": "#8C8B85"}
UI_FONT = "Microsoft YaHei UI"
LOG_LINES = 300


class _UiLogHandler(logging.Handler):
    def __init__(self, q):
        super().__init__(logging.INFO)
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M"))

    def emit(self, record):
        try:
            self.q.put(self.format(record))
        except Exception:
            pass


ARAM_KEY_COLOR = "#FE01FD"        # 悬浮层的「透明色」：这个颜色的地方看得见后面的游戏
ARAM_TIER_COLORS = {"S": "#F0C75E", "A": HEX["ok"], "B": HEX["text"]}
ARAM_BAR_BG, ARAM_BAR_EDGE = HEX["title"], "#785A28"
ARAM_SHOW_MS, ARAM_MSG_MS = 60000, 3000     # 标注平时由后台盯着卡片、卡片没了就收；60 秒只是保险
ARAM_DROP = (-8, -4, -2, -1, 0)   # 标注条落下来的位移（像素，先快后慢），每 30 毫秒一步


class AramOverlay:
    """海克斯助手的标注：盖在游戏画面上的一层透明窗口，鼠标点得穿、不抢焦点，
    三张卡片上方各一条深色金边的标注（梯队字母 + 认出来的海克斯名字），推荐的那条描边变亮、带「推荐」角标。
    下面一行小字（排名、胜率、比这个英雄的平均高低多少、场次）；这张能凑天胡组合的再多一行金字；
    选的人多却掉胜率的是「陷阱」，描边和角标是红的。行数多的条往上长，下沿始终贴着卡片。
    过程：三选一出来、后台认出三张卡 → 三条标注从左到右各隔 60 毫秒落下 8 像素到位 → 最后推荐的那条亮起来
    → 一直留到选完（后台发现卡片没了就收掉）；重掷换了卡时原地换字，不重放动画；
    游戏窗口移动、切走时立刻消失。按热键手动识别时，先在中间那张卡上方显示「识别中」和三个依次亮起的小点"""

    def __init__(self, root, assistant: AramAssistant):
        import tkinter as tk
        self.tk, self.root, self.assistant = tk, root, assistant
        self.layout = None
        self.jobs: list = []            # 排着队的动画步骤，换画面时要全部取消
        self.cards_on = False           # 现在显示的是不是一组卡的结果（不是「识别中」或提示）
        self.data: dict = {}            # 后台准备好的选人 / 读条数据
        self.panel = None               # 攻略侧栏（AramPanel），由 CompanionWindow 接上
        self.badges = None              # 读条界面上的十人战绩标签（LoadingBadges）
        self.dot_step = 0
        win = self.win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.config(bg=ARAM_KEY_COLOR)
        win.attributes("-topmost", True)
        win.attributes("-transparentcolor", ARAM_KEY_COLOR)
        win.geometry("1x1+0+0")
        # Tk 这边只在这里显示一次，之后的显示 / 隐藏都直接用系统接口（带「不激活」）：
        # Tk 自己的 deiconify 会把窗口激活，对局中一激活游戏就丢了焦点
        win.deiconify()
        win.update_idletasks()
        w = _aram_win()
        self.hwnd = int(w.GetAncestor(win.winfo_id(), 2) or 0)          # GA_ROOT
        # 分层 | 鼠标穿透 | 不抢焦点 | 不进任务栏
        w.SetWindowLongW(self.hwnd, -20, w.GetWindowLongW(self.hwnd, -20) | 0x80000 | 0x20 | 0x08000000 | 0x80)
        self._hide_window()
        self.bars = [self._make_bar() for _ in range(3)]
        root.after(40, self._poll)

    def _hide_window(self) -> None:
        # SWP_HIDEWINDOW | NOSIZE | NOMOVE | NOZORDER | NOACTIVATE
        _aram_win().SetWindowPos(self.hwnd, None, 0, 0, 0, 0, 0x0080 | 0x0001 | 0x0002 | 0x0004 | 0x0010)

    def _make_bar(self) -> dict:
        tk = self.tk
        edge = tk.Frame(self.win, bg=ARAM_BAR_EDGE)
        body = tk.Frame(edge, bg=ARAM_BAR_BG)
        tier = tk.Label(body, bg=ARAM_BAR_BG, fg=HEX["text"], padx=0, pady=0)
        text = tk.Label(body, bg=ARAM_BAR_BG, fg=HEX["muted"], padx=0, pady=0, anchor="w")
        tag = tk.Label(body, bg=HEX["gold"], fg=HEX["title"], text="推荐", padx=0, pady=0)
        sub = tk.Label(body, bg=ARAM_BAR_BG, fg=HEX["muted"], padx=0, pady=0, anchor="w")      # 胜率 · 比平均 · 场次
        combo = tk.Label(body, bg=ARAM_BAR_BG, fg="#F0C75E", padx=0, pady=0, anchor="w")       # 天胡 + 另一张
        dots = [tk.Label(body, bg=ARAM_BAR_BG, fg=HEX["line"], text="●", padx=0, pady=0) for _ in range(3)]
        return {"edge": edge, "body": body, "tier": tier, "text": text, "tag": tag, "sub": sub, "combo": combo,
                "dots": dots, "x": 0, "y": 0, "extra": 0}

    # --- 摆放 ---
    def _geometry(self, layout: dict, slot: int) -> tuple[int, int, int, int]:
        """第 slot 条标注在悬浮层里的位置：横向对准卡片中线，贴在卡片上沿的上方"""
        left, top, cw, ch = layout["content"]
        width, height = int(cw * 0.155), max(22, int(ch * 0.043))
        cx = left + cw * ARAM_CARD_CENTERS[slot]
        return int(cx - width / 2) - layout["client"][0], int(top + ch * 0.118) - layout["client"][1], width, height

    def _show_window(self, layout: dict) -> bool:
        if self.layout != layout:
            left, top, width, height = layout["client"]
            # HWND_TOPMOST，SWP_NOACTIVATE | SWP_SHOWWINDOW：用系统接口摆位置，副屏在左边（坐标是负数）时也对
            if not _aram_win().SetWindowPos(self.hwnd, -1, left, top, width, height, 0x0010 | 0x0040):
                return False
            self.layout = layout
            self.win.update_idletasks()               # 让 Tk 知道窗口变大了，下面按新尺寸摆标注条
        self.win.attributes("-alpha", 1.0)
        return True

    def _style(self, bar: dict, layout: dict, *, bright: bool, trap: bool = False) -> None:
        ch = layout["content"][3]
        px = max(11, int(ch * 0.0165))
        bar["tier"].config(font=(UI_FONT, -int(px * 1.35), "bold"))
        bar["text"].config(font=(UI_FONT, -px))
        bar["tag"].config(font=(UI_FONT, -max(10, px - 2)), padx=max(3, px // 3))
        small = max(10, px - 3)
        bar["sub"].config(font=(UI_FONT, -small))
        bar["combo"].config(font=(UI_FONT, -small, "bold"))
        for d in bar["dots"]:
            d.config(font=(UI_FONT, -max(8, px - 4)))
        edge = 2 if bright else 1
        bar["edge"].config(bg=HEX["gold_hi"] if bright else HEX["bad"] if trap else ARAM_BAR_EDGE)
        bar["body"].place(x=edge, y=edge, relwidth=1, relheight=1, width=-2 * edge, height=-2 * edge)
        bar["extra"] = 0

    def _fill(self, bar: dict, layout: dict, card: dict, *, bright: bool) -> None:
        """把一张卡的结果摆进标注条。第一行：左边梯队大字，中间排名，右边「推荐」或「陷阱」角标；
        第二行（有数据才有）：胜率 · 比平均 · 场次；第三行（能凑天胡才有）：天胡 + 另一张。
        多出来的行高记在 bar["extra"] 里，摆放时整条往上长这么多"""
        trap = bool(card.get("trap") and card.get("matched")) and not bright
        self._style(bar, layout, bright=bright, trap=trap)
        _, _, width, base = self._geometry(layout, 0)
        pad = max(6, int(layout["content"][3] * 0.008))
        for wdg in (bar["tier"], bar["text"], bar["tag"], bar["sub"], bar["combo"], *bar["dots"]):
            wdg.place_forget()
        tier = str(card.get("tier") or "") if card.get("matched") else ""
        mid = base // 2 - 1                        # 第一行的中线（条变高以后不能再用「整条的一半」）
        x = pad
        if tier:
            bar["tier"].config(text=tier, fg=ARAM_TIER_COLORS.get(tier, HEX["muted"]))
            bar["tier"].place(x=x, y=mid, anchor="w")
            bar["tier"].update_idletasks()
            x += bar["tier"].winfo_reqwidth() + pad
        strong = bright or (card.get("matched") and tier in ("S", "A"))
        bar["text"].config(text=aram_card_title(card), fg=HEX["text"] if strong or card.get("matched") else HEX["muted"])
        bar["text"].place(x=x, y=mid, anchor="w")
        if bright or trap:
            bar["tag"].config(text="推荐" if bright else "陷阱", bg=HEX["gold"] if bright else HEX["bad"],
                              fg=HEX["title"] if bright else "#FFFFFF")
            bar["tag"].place(relx=1.0, x=-pad, y=mid, anchor="e")
        line = max(16, int(layout["content"][3] * 0.0235))
        extra = 0
        stats, color = aram_card_stats(card)
        if stats:
            bar["sub"].config(text=stats, fg=color)
            bar["sub"].update_idletasks()
            if bar["sub"].winfo_reqwidth() > width - 2 * pad:             # 放不下：省掉「比平均」三个字
                bar["sub"].config(text=aram_card_stats(card, short=True)[0])
            bar["sub"].place(x=pad, y=base - 4 + extra + line // 2, anchor="w")
            extra += line
        combo = aram_card_combo(card)
        if combo:
            bar["combo"].config(text=combo)
            bar["combo"].update_idletasks()
            if bar["combo"].winfo_reqwidth() > width - 2 * pad:           # 另一张的名字太长：不带场次
                bar["combo"].config(text=aram_card_combo(card, short=True))
            bar["combo"].place(x=pad, y=base - 4 + extra + line // 2, anchor="w")
            extra += line
        bar["extra"] = extra

    # --- 三种画面 ---
    def show_busy(self, layout: dict) -> None:
        self.clear(keep_window=True)
        if not self._show_window(layout):
            return
        bar = self.bars[1]
        self._style(bar, layout, bright=False)
        pad = max(6, int(layout["content"][3] * 0.008))
        bar["text"].config(text="识别中", fg=HEX["text"])
        bar["text"].place(x=pad, rely=0.5, anchor="w")
        bar["text"].update_idletasks()
        x = pad + bar["text"].winfo_reqwidth() + pad
        for d in bar["dots"]:
            d.config(fg=HEX["line"])
            d.place(x=x, rely=0.5, anchor="w")
            x += d.winfo_reqwidth() + 2
        bx, by, bw, bh = self._geometry(layout, 1)
        bar["edge"].place(x=bx, y=by, width=bw, height=bh)
        self.dot_step = 0
        self._tick_dots()
        self._later(6000, self.clear)                 # 机器人一直没回：别让「识别中」挂在那儿
        self._watch()

    def _tick_dots(self) -> None:
        """三个小点依次亮起，亮满后全灭再来"""
        dots = self.bars[1]["dots"]
        lit = self.dot_step % 4
        for i, d in enumerate(dots):
            d.config(fg=HEX["gold"] if i < lit else HEX["line"])
        self.dot_step += 1
        self._later(220, self._tick_dots)

    def show_cards(self, layout: dict, cards: list) -> None:
        # 已经显示着一组卡（重掷后重认、鼠标悬停引起的重认）：原地换字，不再重放落下的动画，免得一闪一闪
        animate = not (self.cards_on and self.layout == layout)
        self.clear(keep_window=True)
        if not self._show_window(layout):
            return
        by_slot = {c.get("slot"): c for c in cards if isinstance(c.get("slot"), int)}
        best = None
        for slot in range(3):
            card = by_slot.get(slot)
            if card is None:
                continue
            bar = self.bars[slot]
            is_best = bool(card.get("best") and card.get("matched"))
            bx, by, bw, bh = self._geometry(layout, slot)
            self._fill(bar, layout, card, bright=is_best and not animate)   # 有动画时先都按普通样子落下来，推荐的最后再亮
            by, bh = by - bar["extra"], bh + bar["extra"]                    # 行数多的条往上长，下沿不动
            if animate:
                for step, dy in enumerate(ARAM_DROP):
                    self._later(slot * 60 + step * 30, lambda b=bar, x=bx, y=by + dy, w=bw, h=bh: b["edge"].place(x=x, y=y, width=w, height=h))
                if is_best:
                    best = (bar, card)
            else:
                bar["edge"].place(x=bx, y=by, width=bw, height=bh)
        if best:
            self._later(2 * 60 + (len(ARAM_DROP) - 1) * 30 + 80, lambda: self._fill(best[0], layout, best[1], bright=True))
        self.cards_on = True
        if self.panel is not None:
            self.panel.set_picking(True)              # 侧栏换到海克斯那一页
        self._later(ARAM_SHOW_MS, self._fade_out)
        self._watch()

    def show_message(self, layout: dict, text: str) -> None:
        self.clear(keep_window=True)
        if not self._show_window(layout):
            return
        bar = self.bars[1]
        self._style(bar, layout, bright=False)
        bar["text"].config(text=text, fg=HEX["text"])
        bar["text"].update_idletasks()
        pad = max(6, int(layout["content"][3] * 0.008))
        bar["text"].place(x=pad, rely=0.5, anchor="w")
        bx, by, bw, bh = self._geometry(layout, 1)
        need = bar["text"].winfo_reqwidth() + 2 * pad + 4        # 提示的字比排名长：条跟着变宽，仍然居中
        if need > bw:
            bx, bw = bx - (need - bw) // 2, need
        bar["edge"].place(x=bx, y=by, width=bw, height=bh)
        self._later(ARAM_MSG_MS, self._fade_out)
        self._watch()

    # --- 收尾 ---
    def _fade_out(self, step: int = 0) -> None:
        """整层 150 毫秒淡出（透明色抠掉的部分一直是透的，淡的是标注条）"""
        if step >= 5:
            self.clear()
            return
        try:
            self.win.attributes("-alpha", 1.0 - (step + 1) * 0.2)
        except Exception:
            pass
        self._later(30, lambda: self._fade_out(step + 1))

    def clear(self, keep_window: bool = False) -> None:
        for job in self.jobs:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self.jobs.clear()
        self.cards_on = False
        for bar in self.bars:
            bar["edge"].place_forget()
            for wdg in (bar["tier"], bar["text"], bar["tag"], bar["sub"], bar["combo"], *bar["dots"]):
                wdg.place_forget()
        if not keep_window:
            self.layout = None
            self._hide_window()
            self.win.attributes("-alpha", 1.0)
            if self.panel is not None:
                self.panel.set_picking(False)

    def _later(self, ms: int, fn) -> None:
        self.jobs.append(self.root.after(ms, fn))

    def _watch(self) -> None:
        """显示着的时候每 200 毫秒看一眼：游戏窗口挪了、切走了，标注就对不上位置了，马上收掉"""
        if self.layout is None:
            return
        if game_layout() != self.layout:
            self.clear()
            return
        self._later(200, self._watch)

    def _poll(self) -> None:
        try:
            while True:
                ev = self.assistant.events.get_nowait()
                if ev[0] in ("select", "pregame", "started", "loaded"):
                    self.data[ev[0]] = ev[1]           # 选人梯队 / 读条数据（十人战绩、攻略）/ 读完条了 / 开打一阵了
                    if self.badges is not None:
                        if ev[0] == "pregame":
                            self.badges.set_pregame(ev[1])
                        elif ev[0] == "loaded":
                            self.badges.set_loaded(ev[1])
                    if self.panel is not None:
                        if ev[0] == "select":
                            self.panel.set_select(ev[1])
                        elif ev[0] == "pregame":
                            self.panel.set_pregame(ev[1])
                        elif ev[0] == "started":
                            self.panel.set_game_started(ev[1])
                        elif ev[0] == "loaded":
                            self.panel.set_loaded(ev[1])
                elif ev[0] == "icons":
                    if self.panel is not None:
                        self.panel.fill_icons()            # 英雄头像读到了：把占位换成头像
                elif ev[0] == "clear":
                    self.clear()
                elif game_layout() != ev[1]:
                    self.clear()                       # 等机器人回话的工夫窗口变了：这次结果不画
                elif ev[0] == "busy":
                    self.show_busy(ev[1])
                elif ev[0] == "cards":
                    self.show_cards(ev[1], ev[2])
                elif ev[0] == "msg":
                    self.show_message(ev[1], ev[2])
        except Exception as e:
            if type(e).__name__ != "Empty":
                logger.debug(f"海克斯标注出错: {e}")
                try:
                    self.clear()
                except Exception:
                    pass
        self.root.after(40, self._poll)


_client_hwnd = 0


ARAM_LOAD_CENTERS = (0.1911, 0.3453, 0.4995, 0.6536, 0.8078)   # 读条界面每排五张卡的中线 ÷ 画面宽（1080p 实测）
ARAM_LOAD_BOTTOMS = (0.4889, 0.9741)                            # 上排、下排卡片的下沿 ÷ 画面高


# 按近期 KDA 分的档：(KDA 至少多少, 叫什么)。都不到的是「牛马」；一队里的「大哥」不看 KDA，直接是「精品上等马」。
# 两套线：大乱斗一类的模式助攻多、KDA 普遍高一截，线也高；其它模式（召唤师峡谷等）用低一档的线。
# 用哪套看的是「现在这一局」的模式
HORSE_ARAM_MODES = {"ARAM", "KIWI", "KIWI_JADE"}
HORSE_LINES_ARAM = ((4.5, "上等马"), (3.2, "中等马"), (2.2, "下等马"))
HORSE_LINES_OTHER = ((3.5, "上等马"), (2.5, "中等马"), (1.8, "下等马"))


def player_horse(row: dict, game_mode: str = "") -> tuple[str, str]:
    """一个人是什么马：(叫法, 颜色)。没有战绩的不评。game_mode 是现在这一局的模式名"""
    kda = row.get("kda")
    if not row.get("games") or kda is None:
        return "", ""
    if row.get("boss"):
        return "精品上等马", "#F0C75E"
    lines = HORSE_LINES_ARAM if str(game_mode or "").upper() in HORSE_ARAM_MODES else HORSE_LINES_OTHER
    name = next((label for floor, label in lines if kda >= floor), "牛马")
    return name, {"上等马": HEX["ok"], "中等马": HEX["text"], "下等马": HEX["muted"], "牛马": HEX["bad"]}[name]


def player_badge(row: dict, game_mode: str = "") -> tuple[str, str, str, str, str]:
    """一个人的战绩标签：(主文字, 主文字颜色, 开黑, 是什么马, 马的颜色)。胜率高的青色、低的红色"""
    games, rate, kda = row.get("games") or 0, row.get("win_rate"), row.get("kda")
    if not games or rate is None:
        return "没有近期战绩", HEX["muted"], "", "", ""
    text = f"胜率 {rate * 100:.0f}%"
    if kda is not None:
        text += f" · KDA {kda:g}"
    color = HEX["ok"] if rate >= 0.55 else HEX["bad"] if rate <= 0.45 else HEX["text"]
    size = row.get("premade") or 0
    premade = {2: "双排", 3: "三排", 4: "四排", 5: "五排"}.get(size, "开黑") if size >= 2 else ""
    horse, horse_color = player_horse(row, game_mode)
    return text, color, premade, horse, horse_color


class LoadingBadges:
    """读条界面上的十人战绩：每张英雄卡片的底边贴一条小标签（近 20 局胜率 · KDA，开黑的标「双排 / 三排」，
    再按 KDA 评个「上等马 / 中等马 / 下等马 / 牛马」，一队里近期打得最凶的那个是「精品上等马」）。透明、鼠标点得穿、不抢焦点，和 AramOverlay 是同一种窗口。
    过程：战绩查到了、游戏窗口也出来了 → 十条标签一起 150 毫秒淡入 → 读条结束（游戏里开始走时间）→ 淡出。
    卡片从左到右的顺序用的是客户端的选英雄记录，上排是蓝色方、下排是红色方"""

    def __init__(self, root, assistant: AramAssistant):
        import tkinter as tk
        self.tk, self.root = tk, root
        self.rows = None              # 十个人的战绩；None = 没有要显示的
        self.game_id = None
        self.game_mode = ""           # 这一局的模式名：决定「马」用哪套分档线
        self.loaded_game = None       # 哪一局已经读完条了
        self.layout = None            # 现在摆在哪个画面上；None = 没显示
        self.fading = False
        win = self.win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.config(bg=ARAM_KEY_COLOR)
        win.attributes("-topmost", True)
        win.attributes("-transparentcolor", ARAM_KEY_COLOR)
        win.attributes("-alpha", 0.0)
        win.geometry("1x1+0+0")
        win.deiconify()               # 只在这里用 Tk 显示一次，之后都用系统接口（不激活）
        win.update_idletasks()
        w = _aram_win()
        self.hwnd = int(w.GetAncestor(win.winfo_id(), 2) or 0)
        w.SetWindowLongW(self.hwnd, -20, w.GetWindowLongW(self.hwnd, -20) | 0x80000 | 0x20 | 0x08000000 | 0x80)
        self._hide_now()
        self.bars = []
        for _ in range(10):
            edge = tk.Frame(win, bg=ARAM_BAR_EDGE)
            body = tk.Frame(edge, bg=ARAM_BAR_BG)
            body.place(x=1, y=1, relwidth=1, relheight=1, width=-2, height=-2)
            main = tk.Label(body, bg=ARAM_BAR_BG, fg=HEX["text"], padx=0, pady=0)
            team = tk.Label(body, bg=ARAM_BAR_BG, fg=HEX["gold"], padx=0, pady=0)        # 双排 / 三排
            tag = tk.Label(body, bg=ARAM_BAR_BG, fg=HEX["gold"], padx=0, pady=0)         # 上等马 / 牛马 …
            self.bars.append((edge, main, team, tag))
        root.after(250, self._watch)

    def set_pregame(self, data: dict | None) -> None:
        if data is None:
            self.rows, self.game_id = None, None
        elif data.get("players"):
            self.rows, self.game_id, self.game_mode = data["players"], data.get("game_id"), data.get("game_mode") or ""
        self._update()

    def set_loaded(self, game_id) -> None:
        self.loaded_game = game_id
        self._update()

    def _hide_now(self) -> None:
        _aram_win().SetWindowPos(self.hwnd, None, 0, 0, 0, 0, 0x0080 | 0x0001 | 0x0002 | 0x0004 | 0x0010)
        self.layout, self.fading = None, False

    def _fade(self, step: int, out: bool) -> None:
        if out and not self.fading:
            return
        if step > 5:
            if out:
                self._hide_now()
            return
        try:
            self.win.attributes("-alpha", (5 - step) / 5 if out else step / 5)
        except Exception:
            pass
        self.root.after(30, lambda: self._fade(step + 1, out))

    def _update(self) -> None:
        want = self.rows is not None and self.game_id != self.loaded_game
        layout = game_layout() if want else None
        if layout is None:
            if self.layout is not None and not self.fading:
                self.fading = True
                self._fade(1, True)
            return
        if layout == self.layout and not self.fading:
            self._fill(layout)                    # 位置没变：只更新文字（数据可能刚补齐）
            return
        first = self.layout is None or self.fading
        self.fading = False
        left, top, width, height = layout["client"]
        if not _aram_win().SetWindowPos(self.hwnd, -1, left, top, width, height, 0x0010 | 0x0040):
            return
        self.layout = layout
        self.win.update_idletasks()
        self._fill(layout)
        if first:
            self._fade(0, False)
        else:
            self.win.attributes("-alpha", 1.0)

    def _fill(self, layout: dict) -> None:
        left, top, cw, ch = layout["content"]
        ox, oy = left - layout["client"][0], top - layout["client"][1]
        # 和卡片差不多宽（1080p 上 250x18），正好盖住名字下面那行称号；左边胜率和 KDA，右边开黑和「马」
        bw, bh = int(cw * 0.1305), max(15, int(ch * 0.0172))
        px = max(9, int(ch * 0.0118))
        pad = max(4, int(ch * 0.005))
        used = set()
        for row in self.rows or []:
            team_no, slot = row.get("team"), row.get("slot")
            if team_no not in (1, 2) or not isinstance(slot, int) or not 0 <= slot < 5:
                continue
            index = (team_no - 1) * 5 + slot
            used.add(index)
            edge, main, team, tag = self.bars[index]
            text, color, premade, horse, horse_color = player_badge(row, self.game_mode)
            main.config(text=text, fg=color, font=(UI_FONT, -px, "bold"))
            x = ox + int(cw * ARAM_LOAD_CENTERS[slot]) - bw // 2
            y = oy + int(ch * ARAM_LOAD_BOTTOMS[team_no - 1]) - bh - 1
            team.place_forget()
            tag.place_forget()
            if not horse and not premade:
                main.place(relx=0.5, rely=0.5, anchor="center")
            else:
                main.place(x=pad, rely=0.5, anchor="w")
                right = -pad
                if horse:
                    tag.config(text=horse, fg=horse_color, font=(UI_FONT, -px, "bold"))
                    tag.place(relx=1.0, x=right, rely=0.5, anchor="e")
                    tag.update_idletasks()
                    right -= tag.winfo_reqwidth() + pad
                if premade:
                    team.config(text=premade, fg=HEX["gold"], font=(UI_FONT, -px))
                    team.place(relx=1.0, x=right, rely=0.5, anchor="e")
            edge.config(bg=HEX["gold_hi"] if row.get("is_me") else ARAM_BAR_EDGE)
            edge.place(x=x, y=y, width=bw, height=bh)
        for i, bar in enumerate(self.bars):
            if i not in used:
                bar[0].place_forget()

    def _watch(self) -> None:
        """每 250 毫秒看一眼：游戏窗口出来了 / 挪了 / 切走了，标签跟着出现、移动、收起"""
        try:
            want = self.rows is not None and self.game_id != self.loaded_game
            if want or self.layout is not None:
                if (game_layout() if want else None) != self.layout:
                    self._update()
        except Exception as e:
            logger.debug(f"战绩标签出错: {e}")
        self.root.after(250, self._watch)


def find_client_window() -> int:
    """英雄联盟客户端（大厅 / 选人界面）的窗口。没开就是 0。
    客户端有十来个同类名的窗口，只有一个是看得见的大窗口，其余是 198x56 的隐藏小窗口，所以不能随便拿第一个：
    把同类名的都过一遍，取看得见的里面最大的。找到后记住，窗口还在就不用每次重找"""
    global _client_hwnd
    w = _aram_win()
    cls = w.ctypes.create_unicode_buffer(32)
    if _client_hwnd and w.IsWindowVisible(_client_hwnd) and w.GetClassNameW(_client_hwnd, cls, 32) and cls.value == "RCLIENT":
        return _client_hwnd
    best = [0, 0]

    def visit(hwnd, _):
        if w.GetClassNameW(hwnd, cls, 32) and cls.value == "RCLIENT" and w.IsWindowVisible(hwnd):
            rect = w.wintypes.RECT()
            if w.GetWindowRect(hwnd, w.ctypes.byref(rect)):
                area = (rect.right - rect.left) * (rect.bottom - rect.top)
                if rect.right - rect.left >= 600 and area > best[1]:
                    best[0], best[1] = int(hwnd or 0), area
        return True
    w.EnumWindows(w.ENUMPROC(visit), 0)
    _client_hwnd = best[0]
    return _client_hwnd


def client_window_rect() -> tuple[int, int, int, int] | None:
    """客户端窗口在最前面时，返回它在屏幕上的位置 (左, 上, 右, 下)；切走了、最小化了返回 None"""
    w = _aram_win()
    hwnd = find_client_window()
    if not hwnd or hwnd != int(w.GetForegroundWindow() or 0) or w.IsIconic(hwnd) or not w.IsWindowVisible(hwnd):
        return None
    rect = w.wintypes.RECT()
    if not w.GetWindowRect(hwnd, w.ctypes.byref(rect)) or rect.right - rect.left < 400:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def monitor_work_area(left: int, top: int, right: int, bottom: int) -> tuple[int, int, int, int]:
    """这块区域所在那台显示器的可用范围（去掉任务栏）。查不到就当作只有这块区域"""
    w = _aram_win()
    try:
        rect = w.wintypes.RECT(left, top, right, bottom)
        info = w.MONITORINFO()
        info.cbSize = w.ctypes.sizeof(w.MONITORINFO)
        if w.GetMonitorInfoW(w.MonitorFromRect(w.ctypes.byref(rect), 2), w.ctypes.byref(info)):     # MONITOR_DEFAULTTONEAREST
            return info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom
    except Exception:
        pass
    return left, top, right, bottom


class AramPanel:
    """选人梯队 + 英雄攻略的侧栏：一条窄窗口，鼠标点得穿、不抢焦点。
    - 选人时贴在客户端右边（右边放不下就贴左边，两边都放不下就压在客户端右侧里面），跟着客户端移动；
      显示我现在的英雄的梯队、候选席上每个英雄的梯队（按强到弱排）、这个英雄的技能加点 / 召唤师技能 / 出装 / 最强海克斯
    - 对局里贴在游戏画面右侧，每个时刻只显示当下用得上的一页（所以每页都短，游戏占满屏幕也放得下）：
      读条和刚开局 = AI 按这局阵容写的建议 + 出装 + 加点，开局 40 秒后自己收起；
      三选一的标注显示着 = 这个英雄的天胡组合、陷阱、最强海克斯，选完跟着收起；
      按住 Tab = 出装（核心 / 出门 / 后续 / 另一套）。装备和海克斯都带图标（客户端自带的图）
    - 收起以后，这一局里按住 Tab（看计分板的键）侧栏跟着出来，松开就收起。只是看一眼按键状态，不拦截，游戏照样收到 Tab
    - 标题左边是我这个英雄的头像，候选席每个英雄前面也有小头像。图是从本机客户端读的（不联网），
      还没读到时先放一块同样大小的深色方块占位，读到了原地换上，版面不动
    过程：出现时从右边滑进来 12 像素、同时 150 毫秒淡入；攻略还没取到时先显示几条灰色占位条（一明一暗地闪），
    取到了原地换成内容；换英雄时标题马上换，下面先变回占位条再填上；离开选人界面 / 切到别的窗口时淡出"""

    def __init__(self, root, assistant: AramAssistant, px: float = 1.0):
        import tkinter as tk
        self.tk, self.root, self.assistant, self.px = tk, root, assistant, max(1.0, px)
        self.select = None            # 选人界面的数据（None = 不在选人）
        self.pregame = None           # 读条的数据（None = 不在对局）
        self.started_game = None      # 哪一局已经开打了：那一局的侧栏收起
        self.loaded_game = None       # 哪一局已经读完条、进了游戏
        self.inside = False           # 这次是不是压在游戏画面里面（游戏占满屏幕，外面没地方放）
        self.pregame_at = 0.0         # 这一局的读条数据是什么时候来的（太久了也收起，防止一直挂着）
        self.peek = False             # 现在是不是按着 Tab（收起以后按住再看一眼）
        self.picking = False          # 三选一的标注现在是不是显示着（是的话侧栏显示海克斯那一页）
        self.tab_noted = None         # 哪一局已经在日志里说过「按了 Tab」了（一局只说一次）
        self.keys_checked = None      # 哪一局已经查过「游戏的权限是不是比插件高」了
        self.keys_warned = None       # 哪一局已经提示过「系统不让插件看到按键」了
        self.input_game = None        # 下面三样是哪一局的
        self.input_seen = False       # 这一局看到过按键 / 鼠标点击没有（看到过 = 没被挡）
        self.input_quiet = 0.0        # 这一局游戏在最前面、却什么按键都没看到，累计多少秒了
        self.input_at = 0.0
        self.icons: dict = {}         # (英雄编号, 边长) -> 缩好的头像图；None = 这张图解不开，不再试
        self.icon_slots: list = []    # 现在画面上还在等头像的位置：(英雄编号, 边长, 那个 Label)
        self.show_gen = 0             # 第几次「滑进来」：收起 / 淡出以后，上一次滑进来剩下的几步就不走了
        self.pad = int(12 * max(1.0, px))
        self.shown = None             # 现在摆在哪：(模式, 目标矩形)；None = 没显示
        self.rendered = None          # 现在画的是哪份内容（没变就不重画）
        self.fading = False
        self.jobs: list = []
        self.pulse_on = False
        self.skeletons: list = []
        self.full_width = int(300 * self.px)
        self.width = self.full_width  # 这次显示用多宽（游戏占满屏幕时只能放进读条界面右侧的空白，会窄一些）
        win = self.win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.config(bg=ARAM_BAR_EDGE)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.0)
        win.geometry(f"{self.width}x100+0+0")
        self.body = tk.Frame(win, bg=HEX["bg"])
        self.body.place(x=1, y=1, relwidth=1, relheight=1, width=-2, height=-2)
        win.deiconify()               # 只在这里用 Tk 显示一次，之后都用系统接口（不激活），理由同 AramOverlay
        win.update_idletasks()
        w = _aram_win()
        self.hwnd = int(w.GetAncestor(win.winfo_id(), 2) or 0)
        w.SetWindowLongW(self.hwnd, -20, w.GetWindowLongW(self.hwnd, -20) | 0x80000 | 0x20 | 0x08000000 | 0x80)
        self._hide_now()
        root.after(200, self._watch)
        root.after(300, self._watch_key)

    # --- 后台送来的数据 ---
    def set_select(self, view: dict | None) -> None:
        self.select = view
        self._update()

    def set_pregame(self, data: dict | None) -> None:
        if data is not None and (self.pregame or {}).get("game_id") != data.get("game_id"):
            self.pregame_at = time.time()
        self.pregame = data
        self._update()

    def set_game_started(self, game_id) -> None:
        self.started_game = game_id
        self._update()

    def set_loaded(self, game_id) -> None:
        self.loaded_game = game_id
        self._update()

    def set_picking(self, on: bool) -> None:
        if self.picking != bool(on):
            self.picking = bool(on)
            self._update()

    def _page(self, mode: str) -> str:
        """对局里这会儿该显示哪一页：augments = 海克斯（三选一显示着）/ build = 出装（按着 Tab）/ plan = 怎么打"""
        if mode != "game":
            return "select"
        return "augments" if self.picking else "build" if self.peek else "plan"

    def _compact(self, mode: str) -> bool:
        """压在游戏画面里、而且已经进了游戏：右下角有队友头像和小地图，侧栏要短，只留 AI 建议和最要紧的几行"""
        return mode == "game" and self.inside and (self.pregame or {}).get("game_id") == self.loaded_game

    # --- 该不该显示、摆在哪 ---
    def _has_guide(self) -> bool:
        """现在这一局有没有攻略可看（海克斯大乱斗、攻略卡或 AI 建议至少到了一样）"""
        p = self.pregame
        return bool(p and p.get("mayhem") and (p.get("card") or p.get("guide")))

    def _target(self):
        """返回 (模式, 贴靠的矩形 (左, 上, 右, 下))；现在不该显示就返回 None"""
        if self._has_guide() and (self.peek or self.picking or (self.pregame.get("game_id") != self.started_game
                                                and time.time() - self.pregame_at < 240)):
            layout = game_layout()
            if layout:
                left, top, width, height = layout["client"]
                return "game", (left, top, left + width, top + height)
            return None
        if self.select and (self.select.get("mine") or self.select.get("bench")):
            rect = client_window_rect()
            if rect:
                return "select", rect
        return None

    def _place(self, mode: str, rect: tuple, height: int) -> tuple[int, int]:
        left, top, right, bottom = rect
        gap = int(8 * self.px)
        wl, wt, wr, wb = monitor_work_area(*rect)
        if right + gap + self.width <= wr:
            x = right + gap
        elif left - gap - self.width >= wl:
            x = left - gap - self.width
        elif mode == "game":          # 游戏占满了屏幕：压在画面里，靠右、偏上（读条界面最右边一列卡片的右侧是空的）
            return right - self.width - int(6 * self.px), top + int((bottom - top) * 0.14)
        else:
            x = right - self.width - gap
        return x, max(wt, min(top, wb - height))

    def _width_for(self, mode: str, rect: tuple) -> int:
        """这次侧栏用多宽。能贴在窗口外面就用正常宽度；游戏占满屏幕时只有读条界面右边那条空白（画面宽的 12.6%）"""
        left, top, right, bottom = rect
        gap = int(8 * self.px)
        wl, wt, wr, wb = monitor_work_area(*rect)
        if mode == "select" or right + gap + self.full_width <= wr or left - gap - self.full_width >= wl:
            self.inside = False
            return self.full_width
        self.inside = True
        width, height = right - left, bottom - top
        content = min(width, round(height * 16 / 9))
        margin = (width - content) / 2 + content * 0.126
        return min(self.full_width, max(int(margin - 8 * self.px), int(170 * self.px)))

    def _update(self) -> None:
        target = self._target()
        if target is None:
            if self.shown is not None and not self.fading:
                self._fade_out()
            return
        mode, rect = target
        self.width = self._width_for(mode, rect)
        content = self._content(mode)
        if content != self.rendered or self.shown is None:
            self._render(mode, content)
            self.rendered = content
        self.body.update_idletasks()
        # 选人：和客户端一样高；读条：画面的 76%；全屏对局中：到 66% 屏高为止（再往下是队友头像和小地图）
        limit = (rect[3] - rect[1]) if mode == "select" else int((rect[3] - rect[1]) * (0.52 if self._compact(mode) else 0.76))
        height = max(int(60 * self.px), min(limit, self.inner.winfo_reqheight() + 2 * self.pad + 2))
        x, y = self._place(mode, rect, height)
        first = self.shown is None
        self.shown = (mode, rect)
        self._cancel_fade()
        w = _aram_win()
        if first:                     # 从右边滑进来 12 像素，同时淡入
            slide = int(12 * self.px)
            self.show_gen += 1
            for step in range(6):
                self._later(step * 30, lambda s=step, gen=self.show_gen: gen == self.show_gen and not self.fading and (
                    w.SetWindowPos(self.hwnd, -1, x + slide - slide * s // 5, y, self.width, height, 0x0010 | 0x0040),
                    self.win.attributes("-alpha", s / 5)))
        else:
            w.SetWindowPos(self.hwnd, -1, x, y, self.width, height, 0x0010 | 0x0040)
            self.win.attributes("-alpha", 1.0)       # 完全不透：留一点透明度的话，后面窗口的字会隐约透出来

    # --- 英雄头像 ---
    def _icon(self, key, size: int):
        """这张图标缩到 size 像素见方的样子；图还没读到返回 None。key 同 AramAssistant.icon_bytes。
        英雄头像四周有一圈黑边，各裁掉十二分之一；装备和海克斯图标不裁。海克斯图标是透明底的，垫上侧栏的底色"""
        cache_key = (key, size)
        if cache_key in self.icons:
            return self.icons[cache_key]
        raw = getattr(self.assistant, "icon_bytes", lambda c: None)(key)
        if not raw:
            return None
        try:
            rows = shrink_icon(raw, size, cut=1 / 12 if isinstance(key, int) else 0.0, bg=HEX["bg"])
            if rows is None:
                rows = self._icon_rows_tk(raw, size)       # 不是常见格式的 PNG：让 Tk 自己读（拿不到透明度）
            out = self.tk.PhotoImage(width=size, height=size)
            out.put(" ".join(rows))
            self.icons[cache_key] = out
        except Exception as e:
            logger.debug(f"图标 {key} 解不开: {e}")
            self.icons[cache_key] = None
        return self.icons[cache_key]

    def _icon_rows_tk(self, raw: bytes, size: int) -> list:
        call, split = self.root.tk.call, self.root.tk.splitlist
        src = self.tk.PhotoImage(data=base64.b64encode(raw))
        px = [[(int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)) for c in split(r)] for r in split(call(src.name, "data"))]
        sw, sh = len(px[0]), len(px)
        rows = []
        for dy in range(size):
            y0, y1 = dy * sh // size, max(dy * sh // size + 1, (dy + 1) * sh // size)
            line = []
            for dx in range(size):
                x0, x1 = dx * sw // size, max(dx * sw // size + 1, (dx + 1) * sw // size)
                n = (y1 - y0) * (x1 - x0)
                r = g = b = 0
                for y in range(y0, y1):
                    for cr, cg, cb in px[y][x0:x1]:
                        r, g, b = r + cr, g + cg, b + cb
                line.append(f"#{r // n:02x}{g // n:02x}{b // n:02x}")
            rows.append("{" + " ".join(line) + "}")
        return rows

    def _icon_slot(self, parent, key, size: int, bg: str):
        """放图标的位置：一个固定大小的方块。图没到时是一块深色占位，到了由 fill_icons 原地换上"""
        slot = self.tk.Label(parent, bg=bg, bd=0, padx=0, pady=0, image=self._blank(size), width=size, height=size)
        if (isinstance(key, int) and key > 0) or (isinstance(key, tuple) and len(key) == 2 and isinstance(key[1], int) and key[1] > 0):
            self.icon_slots.append((key, size, slot))
        return slot

    def _blank(self, size: int):
        key = ("blank", size)
        if key not in self.icons:
            self.icons[key] = self.tk.PhotoImage(width=size, height=size)
        return self.icons[key]

    def fill_icons(self) -> None:
        """把已经读到的头像放进画面上等着的位置；还没读到的，让后台去读"""
        waiting, missing = [], []
        for champion_id, size, slot in self.icon_slots:
            try:
                image = self._icon(champion_id, size)
                if image is not None:
                    slot.config(image=image)
                elif (champion_id, size) not in self.icons:
                    waiting.append((champion_id, size, slot))
                    missing.append(champion_id)
            except Exception:
                pass                  # 这个位置已经随着重画没了
        self.icon_slots = waiting
        if missing and hasattr(self.assistant, "want_icons"):
            self.assistant.want_icons(missing)

    # --- 内容 ---
    def _content(self, mode: str) -> tuple:
        """这次该画什么：整理成一个能比较的元组，没变就不重画"""
        if mode == "game":
            card, guide = self.pregame.get("card"), self.pregame.get("guide")
            champ = (card or {}).get("champion") or {}
            mine = {"name": champ.get("name"), "tier": champ.get("tier"), "rank": champ.get("rank"), "win_rate": champ.get("win_rate"),
                    "champion_id": self.pregame.get("champion_id")}
            bench, waiting = (), guide is None and not self.pregame.get("guide_failed")
        else:
            card, guide, waiting = self.select.get("card"), None, False
            mine = self.select.get("mine") or {}
            bench = tuple((b.get("name"), b.get("tier"), b.get("rank"), b.get("champion_id")) for b in self.select.get("bench") or [])
        return (mode, json.dumps(mine, sort_keys=True, ensure_ascii=False), bench,
                json.dumps(card, sort_keys=True, ensure_ascii=False) if card else None,
                json.dumps(guide, ensure_ascii=False) if guide else None, waiting, self.width, self._compact(mode),
                self._page(mode))

    def _render(self, mode: str, content: tuple) -> None:
        tk = self.tk
        for job in self.jobs:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self.jobs.clear()
        self.fading = False
        self.skeletons = []
        self.icon_slots = []
        for child in self.body.winfo_children():
            child.destroy()
        pad = self.pad
        wrap = self.width - 2 * pad - 2
        inner = self.inner = tk.Frame(self.body, bg=HEX["bg"])
        inner.pack(fill="x", padx=pad, pady=(pad, pad))
        mine = json.loads(content[1])
        compact = bool(content[7])
        card = json.loads(content[3]) if content[3] else None
        guide = json.loads(content[4]) if content[4] else None

        def text(s, color=HEX["text"], size=10, bold=False, top=0):
            tk.Label(inner, text=s, bg=HEX["bg"], fg=color, font=(UI_FONT, size, "bold" if bold else "normal"),
                     wraplength=wrap, justify="left", anchor="w").pack(fill="x", pady=(int(top * self.px), 0))

        def section(title, extra=""):
            """一段的小标题；extra 是靠右的一句灰色说明"""
            row = tk.Frame(inner, bg=HEX["bg"])
            row.pack(fill="x", pady=(int(10 * self.px), 0))
            tk.Label(row, text=title, bg=HEX["bg"], fg=HEX["gold"], font=(UI_FONT, 9), anchor="w").pack(side="left")
            if extra:
                tk.Label(row, text=extra, bg=HEX["bg"], fg=HEX["line"], font=(UI_FONT, 8), anchor="e").pack(side="right")

        def skeleton(widths):
            for frac in widths:
                bar = tk.Frame(inner, bg=HEX["btn"], height=int(12 * self.px), width=int(wrap * frac))
                bar.pack(anchor="w", pady=(int(8 * self.px), 0))
                self.skeletons.append(bar)

        # 标题：头像 + 梯队大字 + 英雄名，名字下面是排名胜率
        top = tk.Frame(inner, bg=HEX["bg"])
        top.pack(fill="x")
        mine_id = mine.get("champion_id")
        if isinstance(mine_id, int) and mine_id > 0:
            frame = tk.Frame(top, bg=HEX["gold"])                 # 头像外面一圈细金边
            frame.pack(side="left", padx=(0, int(10 * self.px)))
            self._icon_slot(frame, mine_id, int(44 * self.px), HEX["btn"]).pack(padx=1, pady=1)
        right = tk.Frame(top, bg=HEX["bg"])
        right.pack(side="left", fill="x", expand=True)
        head = tk.Frame(right, bg=HEX["bg"])
        head.pack(fill="x")
        tier = str(mine.get("tier") or "")
        if tier:
            tk.Label(head, text=tier, bg=HEX["bg"], fg=ARAM_TIER_COLORS.get(tier, HEX["muted"]),
                     font=(UI_FONT, 18, "bold")).pack(side="left", padx=(0, int(8 * self.px)))
        tk.Label(head, text=mine.get("name") or "还没选英雄", bg=HEX["bg"], fg=HEX["text"], font=(UI_FONT, 12, "bold")).pack(side="left")
        bits = []
        if isinstance(mine.get("rank"), (int, float)):
            bits.append(f"第 {int(mine['rank'])}")
        if isinstance(mine.get("win_rate"), (int, float)):
            bits.append(f"胜率 {mine['win_rate'] * 100:.1f}%")
        if bits:
            tk.Label(right, text=" · ".join(bits), bg=HEX["bg"], fg=HEX["muted"], font=(UI_FONT, 9), anchor="w").pack(fill="x")
        page = content[8]
        if mode == "game":            # 放在最上面：全屏对局里侧栏限高，放最下面会被裁掉
            text({"augments": "选海克斯时自动显示 · 选完收起", "build": "出装 · 松开 Tab 收起"}.get(
                page, f"开局 {ARAM_PANEL_HIDE_AT} 秒后收起 · 按住 Tab 看出装"), HEX["gold"], 8, top=2)
        # 「大乱斗调整」（伤害 +5% 之类）不显示：那是普通大乱斗的平衡补丁，海克斯大乱斗里不生效

        if content[2]:                # 候选席：按强到弱；梯队比我手上这个高的描边亮起来
            section("候选席")
            flow = tk.Frame(inner, bg=HEX["bg"])
            flow.pack(fill="x", pady=(int(2 * self.px), 0))
            order = "SABCD"
            my_tier = order.find(str(mine.get("tier") or "?")) if str(mine.get("tier") or "?") in order else len(order)
            line, used = None, wrap + 1
            for name, btier, brank, bid in content[2][:10]:
                better = str(btier or "?") in order and order.find(str(btier)) < my_tier     # 梯队比我手上这个高才亮
                chip = tk.Frame(flow, bg=HEX["gold_hi"] if better else HEX["line"])
                box = tk.Frame(chip, bg=HEX["title"])
                box.pack(padx=1, pady=1)
                if isinstance(bid, int) and bid > 0:              # 小头像贴着左边，和这一格一样高
                    self._icon_slot(box, bid, int(22 * self.px), HEX["btn"]).pack(side="left")
                if btier:
                    tk.Label(box, text=btier, bg=HEX["title"], fg=ARAM_TIER_COLORS.get(btier, HEX["muted"]),
                             font=(UI_FONT, 9, "bold")).pack(side="left", padx=(int(5 * self.px), 0))
                tk.Label(box, text=name or "？", bg=HEX["title"], fg=HEX["text"] if better else HEX["muted"],
                         font=(UI_FONT, 9)).pack(side="left", padx=int(5 * self.px))
                chip.update_idletasks()
                need = chip.winfo_reqwidth() + int(5 * self.px)
                if line is None or used + need > wrap:          # 这一行放不下了：另起一行
                    line = tk.Frame(flow, bg=HEX["bg"])
                    line.pack(fill="x", pady=(0, int(4 * self.px)))
                    used = 0
                chip.pack(in_=line, side="left", padx=(0, int(5 * self.px)))
                chip.lift()
                used += need

        big, mid, small = int(26 * self.px), int(22 * self.px), int(20 * self.px)
        gap = int(5 * self.px)

        def icon_row(keys, size, arrows=False, label="", parent=None):
            """一排图标；arrows=True 时中间放箭头（出装的先后顺序）"""
            row = parent or tk.Frame(inner, bg=HEX["bg"])
            if parent is None:
                row.pack(fill="x", pady=(gap, 0))
            if label:
                tk.Label(row, text=label, bg=HEX["bg"], fg=HEX["line"], font=(UI_FONT, 8)).pack(side="left", padx=(0, gap))
            for n, key in enumerate(keys):
                if arrows and n:
                    tk.Label(row, text="→", bg=HEX["bg"], fg=HEX["line"], font=(UI_FONT, 9)).pack(side="left", padx=int(3 * self.px))
                frame = tk.Frame(row, bg=HEX["line"])
                frame.pack(side="left", padx=(0, 0 if arrows else int(3 * self.px)))
                self._icon_slot(frame, key, size, HEX["btn"]).pack(padx=1, pady=1)
            return row

        def ai_block(limit=5):
            if guide:
                section("这局怎么打（AI）")
                for ln in guide[:limit]:
                    text(f"{ln.get('label')}：{ln.get('text')}", top=3)
            elif content[5]:
                section("这局怎么打（AI 分析中）")
                skeleton((0.9, 0.7))

        def build_block(build, title, extras):
            """一套出装：核心几件的图标（带先后顺序）+ 名字；extras=True 时再加出门装和后续装"""
            names = [n for n in build.get("items") or [] if n]
            if not names:
                return
            ids = [i for i in build.get("item_ids") or [] if isinstance(i, int)]
            label, pick = str(build.get("type_label") or ""), build.get("type_pick_rate")
            share = f"{pick * 100:.0f}% 的人出" if isinstance(pick, (int, float)) and pick >= 0.01 else ""
            section(title, " · ".join(x for x in (label, share) if x))
            if ids:
                icon_row([("item", i) for i in ids[:4]], big, arrows=True)
            text(" → ".join(names), HEX["text"] if not ids else HEX["muted"], 10 if not ids else 9, top=3)
            if not extras:
                return
            starter_ids = [i for i in build.get("starter_ids") or [] if isinstance(i, int)]
            late_ids = [x.get("id") for x in card.get("late_items") or [] if isinstance(x.get("id"), int)]
            if starter_ids or late_ids:
                row = tk.Frame(inner, bg=HEX["bg"])
                row.pack(fill="x", pady=(gap + 2, 0))
                if starter_ids:
                    icon_row([("item", i) for i in starter_ids[:3]], small, label="出门", parent=row)
                if late_ids:
                    if starter_ids and wrap < int(250 * self.px):      # 侧栏窄（压在游戏画面里）时一行放不下两组：后续另起一行
                        row = tk.Frame(inner, bg=HEX["bg"])
                        row.pack(fill="x", pady=(gap, 0))
                    elif starter_ids:
                        tk.Frame(row, bg=HEX["bg"], width=int(8 * self.px)).pack(side="left")
                    icon_row([("item", i) for i in late_ids[:3]], small, label="后续", parent=row)
            else:                                     # 旧机器人没给编号：照老样子写名字
                if build.get("starter"):
                    text("出门：" + "、".join(build["starter"]), HEX["muted"], 9)
                late = [x.get("name") for x in card.get("late_items") or [] if x.get("name")]
                if late:
                    text("后续：" + "、".join(late), HEX["muted"], 9)

        def number(row, main, color, sub=""):
            box = tk.Frame(row, bg=HEX["bg"])
            box.pack(side="right")
            tk.Label(box, text=main, bg=HEX["bg"], fg=color, font=(UI_FONT, 10, "bold"), pady=0).pack(anchor="e")
            if sub:
                tk.Label(box, text=sub, bg=HEX["bg"], fg=HEX["line"], font=(UI_FONT, 8), pady=0).pack(anchor="e")

        def aug_icon(row, aug):
            self._icon_slot(row, ("aug", aug.get("id")) if isinstance(aug.get("id"), int) else None, mid, HEX["btn"]).pack(side="left")

        def combo_rows(limit):
            combos = [c for c in card.get("combos") or [] if len(c.get("augments") or []) == 2]
            if not combos:
                return
            section("天胡组合", "两张一起拿")
            for c in combos[:limit]:
                a, b = c["augments"]
                row = tk.Frame(inner, bg=HEX["bg"])
                row.pack(fill="x", pady=(gap, 0))
                games = c.get("games") or 0
                number(row, f"{(c.get('win_rate') or 0) * 100:.1f}%", HEX["ok"], f"{games / 10000:.1f} 万场" if games >= 10000 else f"{games} 场")
                aug_icon(row, a)
                tk.Label(row, text="+", bg=HEX["bg"], fg=HEX["line"], font=(UI_FONT, 8)).pack(side="left", padx=int(2 * self.px))
                aug_icon(row, b)
                tk.Label(row, text=f"{a.get('name')} + {b.get('name')}", bg=HEX["bg"], fg=HEX["text"], font=(UI_FONT, 9), justify="left",
                         anchor="w", wraplength=max(60, wrap - 2 * mid - int(78 * self.px))).pack(side="left", padx=(gap, 0))

        def trap_rows(limit):
            traps = [t for t in card.get("traps") or [] if t.get("name")]
            if not traps:
                return
            section("陷阱", "选的人多但掉胜率")
            for t in traps[:limit]:
                row = tk.Frame(inner, bg=HEX["bg"])
                row.pack(fill="x", pady=(gap, 0))
                number(row, f"{(t.get('delta') or 0) * 100:+.1f}%", HEX["bad"])
                aug_icon(row, t)
                tk.Label(row, text=t["name"], bg=HEX["bg"], fg=HEX["text"], font=(UI_FONT, 9), anchor="w").pack(side="left", padx=(gap, 0))

        def top_rows(limit):
            augs = card.get("augments") or []
            if not augs:
                return
            section("最强海克斯")
            for a in augs[:limit]:
                row = tk.Frame(inner, bg=HEX["bg"])
                row.pack(fill="x", pady=(gap, 0))
                if isinstance(a.get("win_rate"), (int, float)):
                    number(row, f"{a['win_rate'] * 100:.1f}%", HEX["text"])
                aug_icon(row, a)
                tk.Label(row, text=str(a.get("tier") or "-"), bg=HEX["bg"], fg=ARAM_TIER_COLORS.get(a.get("tier"), HEX["muted"]),
                         font=(UI_FONT, 9, "bold")).pack(side="left", padx=(gap, 0))
                tk.Label(row, text=a.get("name") or "", bg=HEX["bg"], fg=HEX["text"], font=(UI_FONT, 9), anchor="w").pack(side="left", padx=(gap, 0))

        def skill_block():
            skills = card.get("skills") or []
            if skills:
                section("技能加点")
                text(str(skills[0].get("order") or "").replace(">", " > "), size=11)
                for extra in skills[1:2] if not compact else []:
                    text(f"{extra.get('label')}：{str(extra.get('order') or '').replace('>', ' > ')}", HEX["muted"], 9)
            summ = card.get("summoners") or []
            if summ and summ[0].get("spells"):
                section("召唤师技能")
                text(" + ".join(summ[0]["spells"]))

        builds = [b for b in (card or {}).get("builds") or [] if b.get("items")]
        if page == "plan":            # 读条和刚开局：AI 按这局阵容写的建议放最前面，下面是出装和加点
            ai_block()
        if mine.get("name") and not card:
            skeleton((0.5, 0.75, 0.6, 0.85))              # 攻略还在路上：先放占位条
        elif card and page == "plan":
            if builds:
                build_block(builds[0], "出装", extras=False)
            skill_block()
        elif card and page == "augments":                 # 三选一显示着：这个英雄该凑什么、该躲什么
            combo_rows(3)
            trap_rows(3)
            top_rows(3)
        elif card and page == "build":                    # 按着 Tab：出装看全
            if builds:
                build_block(builds[0], "出装", extras=True)
            if len(builds) > 1:
                build_block(builds[1], "另一套", extras=False)
            ai_block(2)
        elif card:                                        # 选人界面：地方够，都列出来
            skill_block()
            if builds:
                build_block(builds[0], "出装", extras=True)
            combo_rows(2)
            top_rows(3)
        if not compact:
            text("数据：ARAMKit", HEX["line"], 8, top=10)
        self.fill_icons()
        if self.skeletons:
            self._pulse()

    def _pulse(self) -> None:
        """占位条一明一暗，表示内容正在路上"""
        self.pulse_on = not self.pulse_on
        for bar in self.skeletons:
            try:
                bar.config(bg=HEX["line"] if self.pulse_on else HEX["btn"])
            except Exception:
                pass
        if self.skeletons:
            self._later(450, self._pulse)

    # --- 显示 / 收起 ---
    def _later(self, ms: int, fn) -> None:
        self.jobs.append(self.root.after(ms, fn))

    def _hide_now(self) -> None:
        _aram_win().SetWindowPos(self.hwnd, None, 0, 0, 0, 0, 0x0080 | 0x0001 | 0x0002 | 0x0004 | 0x0010)
        self.shown, self.fading = None, False
        self.show_gen += 1

    def _cancel_fade(self) -> None:
        self.fading = False

    def _fade_out(self, step: int = 0) -> None:
        if step == 0:
            self.fading = True
        if not self.fading:
            return                    # 淡到一半又该显示了
        if step >= 4:
            self._hide_now()
            self.win.attributes("-alpha", 0.0)
            return
        self.win.attributes("-alpha", max(0.0, 1.0 - (step + 1) * 0.25))
        self.root.after(30, lambda: self._fade_out(step + 1))

    def _watch(self) -> None:
        """每 200 毫秒看一眼：客户端 / 游戏窗口挪了就跟过去，切走了就收起，回来了再出来"""
        try:
            target = self._target()
            if target != self.shown and not (target is None and self.shown is None):
                self._update()
        except Exception as e:
            logger.debug(f"攻略侧栏出错: {e}")
        self.root.after(200, self._watch)

    def _watch_input(self, w, game) -> None:
        """系统让不让插件看到按键，看实际情况最准：进了游戏、游戏窗口在最前面，累计 ARAM_INPUT_QUIET_SECONDS 秒
        一次按键、一次鼠标点击都没看到 → 多半是被挡了（游戏的权限比插件高），在日志里说一次；
        只要看到过一次，这一局就不用再查。读条时不算（那时本来就没人按）"""
        now = time.time()
        if game != self.input_game:
            self.input_game, self.input_seen, self.input_quiet, self.input_at = game, False, 0.0, now
        step, self.input_at = min(max(now - self.input_at, 0.0), 0.2), now
        if self.input_seen or self.keys_warned == game or game != self.loaded_game:
            return
        hwnd = find_game_window()
        if not hwnd or hwnd != int(w.GetForegroundWindow() or 0):
            return
        if any(w.GetAsyncKeyState(vk) for vk in ARAM_INPUT_KEYS):
            self.input_seen = True
            return
        self.input_quiet += step
        if self.input_quiet >= ARAM_INPUT_QUIET_SECONDS:
            self.keys_warned = game
            logger.warning(f"海克斯助手：进游戏后游戏窗口在最前面已经 {ARAM_INPUT_QUIET_SECONDS // 60} 分钟，插件一次按键和鼠标点击都没看到。"
                           "多半是游戏以管理员身份运行、插件是普通权限，系统不让插件看按键：按住 Tab 看出装会没有反应，"
                           "右键插件选「以管理员身份运行」就可以了。如果这段时间你确实没碰键盘鼠标，不用管这条")

    def _watch_key(self) -> None:
        """海克斯大乱斗对局里，每 40 毫秒看一眼 Tab 是不是按着：按下去侧栏出来，松开收起。
        同时按着 Alt 的不算（那是在切窗口）；游戏不在最前面时 _target 本来就不让显示"""
        wait = 300
        try:
            peek = False
            game = (self.pregame or {}).get("game_id") if (self.pregame or {}).get("mayhem") else None
            if game is not None:
                wait = 40
                w = _aram_win()
                down = bool(w.GetAsyncKeyState(ARAM_PEEK_VK) & 0x8000) and not w.GetAsyncKeyState(0x12) & 0x8000
                if game != self.keys_checked and find_game_window():
                    self.keys_checked = game
                    if game_blocks_keys():
                        self.keys_warned = game
                        logger.warning("海克斯助手：游戏是用管理员身份运行的，插件是普通权限，系统不让插件看到按键："
                                       "按住 Tab 看出装会没有反应。右键插件选「以管理员身份运行」就可以了（三选一的标注和侧栏不受影响）")
                self._watch_input(w, game)
                if down and game != self.tab_noted and game_layout():
                    self.tab_noted = game             # 这一局第一次在游戏里按 Tab：记一行，出了问题好查
                    if self._has_guide():
                        logger.info("海克斯助手：按住 Tab，显示出装页（松开收起）")
                    elif self.pregame.get("card_failed"):
                        logger.info("海克斯助手：按了 Tab，但这局的英雄攻略没取到，没有内容可显示")
                    else:
                        logger.info("海克斯助手：按了 Tab，但这局的英雄攻略还没取到（或者攻略侧栏在细项里关掉了），没有内容可显示")
                peek = down and self._has_guide()
            if peek != self.peek:
                self.peek = peek
                self._update()
        except Exception as e:
            logger.debug(f"攻略侧栏（Tab）出错: {e}")
        self.root.after(wait, self._watch_key)


class CompanionWindow:
    def __init__(self, companion, keeper: LocaleKeeper, worker: threading.Thread, log_q):
        import tkinter as tk
        from tkinter import messagebox
        self.tk, self.messagebox = tk, messagebox
        self.companion, self.keeper, self.worker, self.log_q = companion, keeper, worker, log_q
        self.launch_until = 0.0
        self.btn_mode = "launch"          # launch：没开客户端 / show：大厅开着，点了切过去 / ingame：对局中，点了没反应
        self.hint_override = ("", 0.0)
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)     # 高分屏上字不发虚
        except Exception:
            pass
        self.root = root = tk.Tk()
        self.px = max(1.0, root.winfo_fpixels("1i") / 96)
        root.title(f"托子的小警察 v{VERSION}")
        root.configure(bg=HEX["bg"])
        root.minsize(int(380 * self.px), int(465 * self.px))
        root.geometry(f"{int(400 * self.px)}x{int(480 * self.px)}")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._dark_title_bar()
        self._set_icon()
        self._build()
        self.overlay = None
        if self.aram is not None:
            try:
                self.overlay = AramOverlay(root, self.aram)        # 海克斯助手的标注层（平时看不见）
                self.overlay.panel = AramPanel(root, self.aram, self.px)   # 选人梯队 + 英雄攻略的侧栏
                self.overlay.badges = LoadingBadges(root, self.aram)       # 读条界面上的十人战绩标签
            except Exception as e:
                logger.warning(f"海克斯助手的标注层打不开，这次用不了: {e}")
        root.after(100, self._tick_logs)
        root.after(300, self._tick_state)

    def _dark_title_bar(self):
        """Windows 10/11 的标题栏改成深色，和窗口一个颜色系"""
        try:
            import ctypes
            self.root.update()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            on = ctypes.c_int(1)
            for attr in (20, 19):           # DWMWA_USE_IMMERSIVE_DARK_MODE（新 / 旧版本号）
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                    break
        except Exception:
            pass

    def _set_icon(self):
        """标题栏 / 任务栏图标：和 exe 同一个 tuoz1.ico（打包时 --add-data 带进去）；找不到就画一个简化版，换掉 Tk 默认的羽毛"""
        try:
            ico = Path(getattr(sys, "_MEIPASS", "") or Path(__file__).resolve().parent) / "tuoz1.ico"
            if ico.is_file():
                self.root.iconbitmap(default=str(ico))
                return
        except Exception:
            pass
        try:
            n = 32
            img = self.tk.PhotoImage(width=n, height=n)
            c, R, w = (n - 1) / 2, 15.0, 2.6
            rows = []
            for y in range(n):
                row = []
                for x in range(n):
                    dx, dy = abs(x - c), abs(y - c)
                    d = max(dx * 0.866 + dy * 0.5, dy)       # 尖朝上的正六边形「半径」
                    if d <= R - w:
                        row.append(HEX["title"] if d > R - w - 2.2 else HEX["bg"])
                    elif d <= R:
                        row.append(HEX["gold"])
                    else:
                        row.append(HEX["bg"])
                rows.append("{" + " ".join(row) + "}")
            img.put(" ".join(rows))
            for y in range(n):                                # 中间一颗金色的点
                for x in range(n):
                    if (x - c) ** 2 + (y - c) ** 2 <= 20:
                        img.put(HEX["gold_hi"], (x, y))
            self._icon = img
            self.root.iconphoto(True, img)
        except Exception:
            pass

    def _p(self, n):
        return int(n * self.px)

    def _build(self):
        tk = self.tk
        root, pad = self.root, self._p(18)
        body = tk.Frame(root, bg=HEX["bg"])
        body.pack(fill="both", expand=True, padx=pad, pady=(self._p(14), pad))

        head = tk.Frame(body, bg=HEX["bg"])
        head.pack(fill="x")
        tk.Label(head, text="托子的小警察", bg=HEX["bg"], fg=HEX["gold"], font=(UI_FONT, 13, "bold")).pack(side="left")
        tk.Label(head, text=f"v{VERSION}", bg=HEX["bg"], fg=HEX["muted"], font=(UI_FONT, 9)).pack(side="left", padx=(6, 0), pady=(4, 0))
        self.log_btn = None
        if console_is_ours():
            self.log_btn = tk.Label(head, text="完整日志", bg=HEX["bg"], fg=HEX["muted"], font=(UI_FONT, 9), cursor="hand2")
            self.log_btn.pack(side="right", pady=(4, 0))
            self.log_btn.bind("<Button-1>", lambda e: self.toggle_console())
            self.log_btn.bind("<Enter>", lambda e: self.log_btn.config(fg=HEX["gold"]))
            self.log_btn.bind("<Leave>", lambda e: self.log_btn.config(fg=HEX["muted"]))

        st = tk.Frame(body, bg=HEX["bg"])
        st.pack(fill="x", pady=(self._p(10), 0))
        self.dot = tk.Label(st, text="●", bg=HEX["bg"], fg=HEX["ok"], font=(UI_FONT, 9))
        self.dot.pack(side="left")
        self.state_lbl = tk.Label(st, text="已连上机器人", bg=HEX["bg"], fg=HEX["muted"], font=(UI_FONT, 9))
        self.state_lbl.pack(side="left", padx=(4, 0))
        self.count_lbl = tk.Label(st, text="", bg=HEX["bg"], fg=HEX["muted"], font=(UI_FONT, 9))
        self.count_lbl.pack(side="right")
        # 当前登录的号有问题（没绑定 / 令牌是别人的）时才出现的一行红字，详细说明在下面的日志里
        self.issue_lbl = tk.Label(body, text="", bg=HEX["bg"], fg=HEX["bad"], font=(UI_FONT, 9), anchor="w")
        self.issue_after = st
        self.issue_shown = False

        zh = tk.Frame(body, bg=HEX["bg"], cursor="hand2")
        zh.pack(fill="x", pady=(self._p(16), 0))
        self.box = tk.Label(zh, text="", width=2, bg=HEX["bg"], fg=HEX["gold"], font=(UI_FONT, 9, "bold"),
                            highlightthickness=1, highlightbackground=HEX["gold"], highlightcolor=HEX["gold"])
        self.box.pack(side="left")
        self.zh_lbl = tk.Label(zh, text="简中汉化（文字 + 语音）", bg=HEX["bg"], fg=HEX["text"], font=(UI_FONT, 11))
        self.zh_lbl.pack(side="left", padx=(self._p(8), 0))
        for w in (zh, self.box, self.zh_lbl):
            w.bind("<Button-1>", lambda e: self.toggle_zh())
        self.hint = tk.Label(body, text="", bg=HEX["bg"], fg=HEX["muted"], font=(UI_FONT, 9), anchor="w", justify="left")
        self.hint.pack(fill="x", pady=(self._p(4), 0))

        # 自动接受对局：和简中汉化同样的勾选框
        self.accepter = getattr(self.companion, "accepter", None)
        if self.accepter is not None:
            acc = tk.Frame(body, bg=HEX["bg"], cursor="hand2")
            acc.pack(fill="x", pady=(self._p(10), 0))
            self.acc_box = tk.Label(acc, text="✓" if self.accepter.enabled else "", width=2, bg=HEX["bg"], fg=HEX["gold"],
                                    font=(UI_FONT, 9, "bold"), highlightthickness=1,
                                    highlightbackground=HEX["gold"], highlightcolor=HEX["gold"])
            self.acc_box.pack(side="left")
            acc_lbl = tk.Label(acc, text="自动接受对局", bg=HEX["bg"], fg=HEX["text"], font=(UI_FONT, 11))
            acc_lbl.pack(side="left", padx=(self._p(8), 0))
            for w in (acc, self.acc_box, acc_lbl):
                w.bind("<Button-1>", lambda e: self.toggle_accept())

        # 海克斯助手：同样的勾选框
        self.aram = getattr(self.companion, "aram", None)
        if self.aram is not None:
            hx = tk.Frame(body, bg=HEX["bg"], cursor="hand2")
            hx.pack(fill="x", pady=(self._p(10), 0))
            self.aram_box = tk.Label(hx, text="✓" if self.aram.enabled else "", width=2, bg=HEX["bg"], fg=HEX["gold"],
                                     font=(UI_FONT, 9, "bold"), highlightthickness=1,
                                     highlightbackground=HEX["gold"], highlightcolor=HEX["gold"])
            self.aram_box.pack(side="left")
            hx_lbl = tk.Label(hx, text="海克斯助手", bg=HEX["bg"], fg=HEX["text"], font=(UI_FONT, 11))
            hx_lbl.pack(side="left", padx=(self._p(8), 0))
            for w in (self.aram_box, hx_lbl):
                w.bind("<Button-1>", lambda e: self.toggle_aram())
            # 细项：点一下在下面展开几个小勾选框（缩进一格），再点收起；窗口跟着变高 / 变矮
            self.aram_more = tk.Label(hx, text="细项 ▸", bg=HEX["bg"], fg=HEX["muted"], font=(UI_FONT, 9), cursor="hand2")
            self.aram_more.pack(side="right")
            self.aram_more.bind("<Button-1>", lambda e: self.toggle_aram_menu())
            self.aram_more.bind("<Enter>", lambda e: self.aram_more.config(fg=HEX["gold"]))
            self.aram_more.bind("<Leave>", lambda e: self.aram_more.config(fg=HEX["muted"]))
            self.aram_row = hx
            self.aram_open = False
            self.aram_sub = tk.Frame(body, bg=HEX["bg"])
            self.aram_sub_rows = {}
            for key, label in ARAM_OPTIONS:
                row = tk.Frame(self.aram_sub, bg=HEX["bg"], cursor="hand2")
                row.pack(fill="x", pady=(self._p(7), 0), padx=(self._p(30), 0))
                box = tk.Label(row, text="", width=2, bg=HEX["bg"], fg=HEX["gold"], font=(UI_FONT, 7, "bold"),
                               highlightthickness=1, highlightbackground=HEX["line"], highlightcolor=HEX["line"])
                box.pack(side="left")
                lbl = tk.Label(row, text=label, bg=HEX["bg"], fg=HEX["text"], font=(UI_FONT, 10))
                lbl.pack(side="left", padx=(self._p(8), 0))
                for w in (row, box, lbl):
                    w.bind("<Button-1>", lambda e, k=key: self.toggle_aram_option(k))
                self.aram_sub_rows[key] = (box, lbl)
            self._refresh_aram_sub()

        # 大按钮：金色描边。鼠标移上去描边变亮、底色提一点；按下去底色压暗；点完 8 秒内显示「启动中…」
        self.btn_frame = tk.Frame(body, bg=HEX["gold"], cursor="hand2")
        self.btn_frame.pack(fill="x", pady=(self._p(16), 0))
        self.btn = tk.Label(self.btn_frame, text="英雄联盟，启动！", bg=HEX["btn"], fg=HEX["gold_hi"],
                            font=(UI_FONT, 15, "bold"), pady=self._p(12), cursor="hand2")
        self.btn.pack(fill="both", padx=2, pady=2)
        for w in (self.btn_frame, self.btn):
            w.bind("<Enter>", lambda e: self._btn_look("hover"))
            w.bind("<Leave>", lambda e: self._btn_look("normal"))
            w.bind("<ButtonPress-1>", lambda e: self._btn_look("down"))
            w.bind("<ButtonRelease-1>", self._btn_release)

        tk.Frame(body, bg=HEX["line"], height=1).pack(fill="x", pady=(self._p(16), self._p(8)))
        self.log = tk.Text(body, bg=HEX["bg"], fg=HEX["log"], font=(UI_FONT, 9), relief="flat", borderwidth=0,
                           highlightthickness=0, wrap="char", height=8, cursor="arrow", state="disabled",
                           selectbackground=HEX["line"], spacing1=self._p(2))
        self.log.pack(fill="both", expand=True)
        try:
            from tkinter import font as tkfont
            indent = tkfont.Font(font=(UI_FONT, 9)).measure("00:00  ")
        except Exception:
            indent = 0
        self.log.tag_configure("ln", lmargin2=indent)
        self._refresh_zh()

    # --- 交互 ---
    def _btn_look(self, kind):
        if time.time() < self.launch_until:
            return
        if self.btn_mode == "ingame":                       # 对局中：按钮压暗，鼠标移上去也不亮
            self.btn.config(bg=HEX["btn"])
            self.btn_frame.config(bg=HEX["line"])
            return
        bg = {"hover": HEX["btn_hover"], "down": HEX["btn_down"]}.get(kind, HEX["btn"])
        self.btn.config(bg=bg)
        self.btn_frame.config(bg=HEX["gold_hi"] if kind in ("hover", "down") else HEX["gold"])

    def _btn_release(self, e):
        inside = 0 <= e.x_root - self.btn_frame.winfo_rootx() <= self.btn_frame.winfo_width() and \
            0 <= e.y_root - self.btn_frame.winfo_rooty() <= self.btn_frame.winfo_height()
        self._btn_look("hover" if inside else "normal")
        if inside and time.time() >= self.launch_until:
            if self.btn_mode == "show":
                self.show_client()
            elif self.btn_mode == "ingame":
                self.hint_override = ("正在对局中，打完再切回大厅", time.time() + 4)
            else:
                self.start_game()

    def show_client(self):
        """客户端已经开着：让它自己把大厅窗口调到前面（最小化了也会还原），不再重复启动"""
        lcu = self.companion.lcu

        def work():
            try:
                lcu.post("/riotclient/ux-show", timeout=5)
                msg = "已切到游戏大厅"
            except Exception as e:
                msg = "没切过去，请在任务栏点英雄联盟"
                logger.info(f"切到游戏大厅失败: {e}")
            self.hint_override = (msg, time.time() + 4)
        if lcu is not None:
            threading.Thread(target=work, daemon=True).start()

    def start_game(self):
        self.launch_until = time.time() + 8
        self.btn.config(text="启动中…", bg=HEX["btn_down"], fg=HEX["muted"])
        self.btn_frame.config(bg=HEX["line"])

        def work():
            try:
                self.keeper.ensure()
                msg = launch_lol()
            except Exception as e:
                msg = f"启动失败：{e}"
                logger.warning(msg)
            self.hint_override = (msg, time.time() + 6)
        threading.Thread(target=work, daemon=True).start()

    def toggle_accept(self):
        on = not self.accepter.enabled
        self.acc_box.config(text="✓" if on else "")
        threading.Thread(target=self.accepter.set_enabled, args=(on,), daemon=True).start()

    def _refresh_aram_sub(self):
        """细项的勾和颜色：总开关关着时细项都变灰（勾还留着，表示重新打开后会恢复成这样）"""
        master = self.aram.enabled
        for key, (box, lbl) in self.aram_sub_rows.items():
            on = self.aram.option(key)
            box.config(text="✓" if on else "", fg=HEX["gold"] if master else HEX["line"],
                       highlightbackground=HEX["gold"] if master and on else HEX["line"])
            lbl.config(fg=HEX["text"] if master else HEX["muted"])

    def toggle_aram_option(self, key):
        if not self.aram.enabled:
            self.hint_override = ("先把上面的「海克斯助手」勾上", time.time() + 3)
            return
        on = not self.aram.option(key)
        self.aram.config[key] = on                    # 先改内存里的，勾马上变；写文件放后台
        self._refresh_aram_sub()
        threading.Thread(target=self.aram.set_option, args=(key, on), daemon=True).start()

    def toggle_aram_menu(self):
        """展开 / 收起细项。窗口高度分 4 步、约 120 毫秒变到位，细项在窗口开始变高时就出现"""
        self.aram_open = not self.aram_open
        self.aram_more.config(text="细项 ▾" if self.aram_open else "细项 ▸")
        root = self.root
        root.update_idletasks()
        if self.aram_open:
            self.aram_sub.pack(fill="x", after=self.aram_row)
            root.update_idletasks()
        delta = self.aram_sub.winfo_reqheight() * (1 if self.aram_open else -1)
        if not self.aram_open:
            self.aram_sub.pack_forget()
        width, height = root.winfo_width(), root.winfo_height()
        min_w = int(380 * self.px)
        base_min = int(465 * self.px)
        root.minsize(min_w, base_min + (self.aram_sub.winfo_reqheight() if self.aram_open else 0))
        for step in range(1, 5):
            root.after(step * 30, lambda s=step: root.geometry(f"{width}x{max(base_min, height + delta * s // 4)}"))

    def toggle_aram(self):
        on = not self.aram.enabled
        self.aram_box.config(text="✓" if on else "")
        self.aram.config["aram_helper"] = on          # 细项的灰 / 亮马上跟着变
        self._refresh_aram_sub()
        self.hint_override = (f"游戏要用无边框模式；没自动出来可以按 {self.aram.hotkey_name}" if on
                              else "海克斯助手已关闭", time.time() + 5)
        threading.Thread(target=self.aram.set_enabled, args=(on,), daemon=True).start()

    def toggle_zh(self):
        """勾选框马上变，改文件和查客户端进程放后台（查进程要一两百毫秒，别让窗口卡一下）"""
        on = not self.keeper.enabled
        self.box.config(text="✓" if on else "")
        self.hint_override = ("正在改成简体中文…" if on else "正在恢复原来的语言…", time.time() + 1)
        threading.Thread(target=self.keeper.set_enabled, args=(on,), daemon=True).start()

    def _refresh_zh(self):
        self.box.config(text="✓" if self.keeper.enabled else "")
        msg, until = self.hint_override
        self.hint.config(text=msg if time.time() < until else self.keeper.status(),
                         fg=HEX["bad"] if self.keeper.last_error else HEX["muted"])

    def toggle_console(self):
        show_console(UI_STATE["console_hidden"])

    def on_close(self):
        if self.messagebox.askokcancel("退出小警察", "退出后不再上报战绩和开局情报，简中汉化也不会再自动保持。\n确定退出吗？",
                                       parent=self.root):
            self.root.destroy()

    # --- 定时刷新 ---
    def _tick_logs(self):
        lines = []
        try:
            while True:
                lines.append(self.log_q.get_nowait())
        except Exception:
            pass
        if lines:
            self.log.config(state="normal")
            for ln in lines:
                self.log.insert("end", ln + "\n", "ln")
            over = int(self.log.index("end-1c").split(".")[0]) - LOG_LINES
            if over > 0:
                self.log.delete("1.0", f"{over + 1}.0")
            self.log.see("end")
            self.log.config(state="disabled")
        self.root.after(200, self._tick_logs)

    def _tick_state(self):
        if not self.worker.is_alive():          # 主循环退出（比如自动更新换了新版本）：窗口跟着关
            self.root.destroy()
            return
        c = self.companion
        issue = getattr(c, "account_issue", "") if c.lcu is not None else ""
        if c.lcu is not None:
            name = c.summoner.get("gameName") or c.summoner.get("displayName") or ""
            self.state_lbl.config(text=f"已连上机器人 · 客户端 {name}".rstrip())
        else:
            self.state_lbl.config(text="已连上机器人 · 等待客户端")
        self.dot.config(fg=HEX["bad"] if issue else HEX["ok"])
        if issue:
            self.issue_lbl.config(text=f"⚠ {issue}，怎么办看下面的日志")
            if not self.issue_shown:
                self.issue_lbl.pack(fill="x", after=self.issue_after, pady=(self._p(2), 0))
                self.issue_shown = True
        elif self.issue_shown:
            self.issue_lbl.pack_forget()
            self.issue_shown = False
        day, n = UI_STATE["uploads"]
        self.count_lbl.config(text=f"今天播报 {n if day == time.strftime('%Y-%m-%d') else 0} 局")
        if self.log_btn is not None:
            self.log_btn.config(text="收起日志" if not UI_STATE["console_hidden"] else "完整日志")
        if self.launch_until and time.time() >= self.launch_until:
            self.launch_until = 0.0
            self.btn_mode = ""                  # 下面按当前状态重新设置按钮
        if not self.launch_until:
            if c.lcu is None:
                mode = "launch"
            elif c.last_phase in IN_GAME_PHASES:
                mode = "ingame"
            else:
                mode = "show"
            if mode != self.btn_mode:
                self.btn_mode = mode
                text = {"launch": "英雄联盟，启动！", "show": "切到游戏大厅", "ingame": "对局中"}[mode]
                self.btn.config(text=text, fg=HEX["muted"] if mode == "ingame" else HEX["gold_hi"],
                                cursor="arrow" if mode == "ingame" else "hand2")
                self.btn_frame.config(cursor="arrow" if mode == "ingame" else "hand2")
                self._btn_look("normal")
        self._refresh_zh()
        self.root.after(1000, self._tick_state)

    def run(self):
        self.root.mainloop()


def run_with_window(companion, keeper: LocaleKeeper) -> int:
    """插件主循环放后台线程，窗口在主线程。窗口起不来就退回纯命令行"""
    import queue
    try:
        import tkinter  # noqa: F401
    except Exception as e:
        logger.info(f"窗口模块不可用（{e}），只用命令行。")
        companion.run()
        return 0
    log_q = queue.Queue()
    handler = _UiLogHandler(log_q)
    logging.getLogger().addHandler(handler)

    def work():
        try:
            companion.run()
        except Exception as e:
            logger.error(f"插件主循环出错: {e}", exc_info=True)
    worker = threading.Thread(target=work, daemon=True, name="companion")
    try:
        win = CompanionWindow(companion, keeper, worker, log_q)
    except Exception as e:
        logging.getLogger().removeHandler(handler)
        logger.warning(f"窗口打不开（{e}），只用命令行。")
        companion.run()
        return 0
    worker.start()
    logger.info("窗口已打开。黑色命令行窗口已隐藏，点窗口右上角「完整日志」可以再显示。" if console_is_ours()
                else "窗口已打开。")
    show_console(False)
    win.run()
    return 0


def main() -> int:
    setup_console_utf8()
    parser = argparse.ArgumentParser(description=f"{APP_NAME} — Tuoz1 Bot 客户端插件")
    parser.add_argument("--server", help=f"调试用：临时覆盖机器人地址（默认 {SERVER_URL}），不写入配置文件")
    parser.add_argument("--token", help="/companion_token 获取的令牌")
    parser.add_argument("--interval", type=int, default=60, help="平时检查新战绩的间隔（秒），默认 60")
    parser.add_argument("--send-latest", action="store_true", help="启动后立刻把最近一场比赛上报一次（用于测试）")
    parser.add_argument("--dump-dir", help="把上报的数据另存到此目录（调试用）")
    parser.add_argument("--reset", action="store_true", help="清除已保存的令牌并重新输入")
    parser.add_argument("--no-update", action="store_true", help="不自动检查/安装新版本")
    parser.add_argument("--import-blacklist", metavar="文件", help="批量导入黑名单：League Akari 的标记玩家导出，或带 riot_id 的 JSON 列表")
    parser.add_argument("file", nargs="?", help=argparse.SUPPRESS)   # 把文件拖到 exe 上时，路径就在这里
    parser.add_argument("--no-gui", action="store_true", help="不开窗口，只用命令行")
    parser.add_argument("--ocr-selftest", action="store_true", help="检查本机认字能不能用（模型和运行库在不在），然后退出")
    parser.add_argument("--no-elevate", action="store_true", help="启动时不申请管理员权限（对局里按住 Tab 看出装可能没反应）")
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)    # 申请权限之后开出来的那个自己带着它
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.ocr_selftest:
        return ocr_selftest()

    setup_logging(args.verbose)
    logger.info(f"{APP_NAME} v{VERSION} — Tuoz1 Bot 客户端插件  (配置: {CONFIG_PATH})")
    if ensure_elevated(args):
        return 0
    cleanup_old_binary()
    if check_for_update(not args.no_update):
        return 0

    config = load_config()
    if args.reset:
        config.pop("token", None)
    if args.token:
        config["token"] = args.token
    # 机器人地址一律以代码里的 SERVER_URL 为准：老用户的配置文件里存的是旧服务器的 IP，
    # 必须无条件覆盖并写回，否则搬家之后他们的插件会一直连不上
    if config.get("server") != SERVER_URL:
        if config.get("server"):
            logger.info(f"机器人地址已更新为 {SERVER_URL}")
        config["server"] = SERVER_URL

    if not config.get("token"):
        config["token"] = prompt("请输入 /companion_token 获取的令牌: ")
    if not config.get("token"):
        logger.error("缺少令牌。")
        return 2
    save_config(config)

    server = SERVER_URL
    if args.server:      # 调试用的临时覆盖：只在这次运行生效，不写进配置文件
        server = args.server if args.server.startswith(("http://", "https://")) else "http://" + args.server
        logger.warning(f"[调试] 本次临时连接 {server}（不会保存）")
    bot = BotAPI(server, config["token"])
    bot.config = config                 # 连机器人时顺带报开关状态（见 feature_flags）
    bot.gui = bool(IS_WINDOWS and not args.no_gui and not (args.import_blacklist or args.file))
    companion = Companion(config, bot, args)
    while True:
        status = companion.check_bot()
        if status is True:
            break
        if status != "unauthorized":
            logger.warning("机器人暂时没响应（可能正在重启），30 秒后再试…")   # 以前直接退出，机器人重启那一分钟启动的插件就没了
            time.sleep(30)
            continue
        new_token = prompt("请粘贴新的令牌（在 Discord 输入 /companion_token 查看；直接回车退出）: ")
        if not new_token:
            return 1
        config["token"] = new_token
        save_config(config)
        bot.headers = {"Authorization": f"Bearer {new_token}"}
    import_file = args.import_blacklist or args.file
    if import_file:
        code = run_blacklist_import(Path(import_file), companion, bot)
        pause_before_exit()      # 拖文件启动的窗口会直接关掉，得让人看到结果
        return code
    keeper = LocaleKeeper(config)       # 简中汉化：开着就一直保持（不管是不是从窗口点的启动）
    companion.accepter = AutoAccepter(config, companion)
    if companion.accepter.enabled:
        logger.info("自动接受对局已开启")
    if IS_WINDOWS and not args.no_gui:      # 海克斯助手要靠窗口线程画标注，纯命令行模式没有
        try:
            companion.aram = AramAssistant(config, companion, bot)
        except Exception as e:
            logger.warning(f"海克斯助手启动失败，这次用不了: {e}")
    if keeper.enabled:
        logger.info(f"简中汉化已开启（{keeper.status()}）")
    try:
        if IS_WINDOWS and not args.no_gui:
            return run_with_window(companion, keeper)
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
