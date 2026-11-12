#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""英雄联盟头像墙：从客户端自带的头像里挑一个换上，被客户端改回去就再设一次。

后台（这个文件，只用标准库）连本机的英雄联盟客户端，在 127.0.0.1 上给界面（web/ 里的网页）提供数据；
界面用电脑自带的 Edge 开成没有地址栏的独立窗口。
两种用法：单独运行（python avatar_picker.py，或者打包成 exe）——关掉窗口，后台也跟着退出，不再重设头像；
嵌在小警察插件里当「彩蛋功能」（那边用下面的 Wall）——插件开着就一直重设，关窗口不影响。

换头像走客户端的两个本机接口（做法参考 league-profile-tool，MIT）：
- 账号头像  PUT /lol-summoner/v1/current-summoner/icon   只对已经拥有的头像有效
- 聊天资料  PUT /lol-chat/v1/me {"icon": 编号}            任何编号都能填；临时的，客户端刷新状态时会改回去
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = "lol-avatar-picker"
PORT = 47584
# 打包成 exe 以后，界面文件在 exe 自己解开的临时文件夹里（sys._MEIPASS），不在 exe 旁边
HERE = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
WEB = HERE / "web"
DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP
CONFIG_PATH = DATA / "config.json"
CATALOG_PATH = DATA / "catalog.json"        # 上次读到的头像目录：客户端没开时也能先翻着看
ICON_DIR = DATA / "icons"                   # 看过的头像图存一份，下次不用再问客户端

TICK_SECONDS = 1.5              # 多久看一眼客户端
RETRY_GAP_SECONDS = 4.5         # 两次重设之间至少隔这么久
FIGHT_LIMIT = 3                 # 连着被改回这么多次就停手，不和客户端死磕
CALM_SECONDS = 20               # 设上以后这么久没再被改回，就算稳住了，次数清零
LOG_KEEP = 40
RECENT_KEEP = 12

PHASES = {"None": "大厅", "Lobby": "房间里", "Matchmaking": "排队", "ReadyCheck": "对局确认", "ChampSelect": "选人",
          "GameStart": "开局", "InProgress": "对局中", "WaitingForStats": "等结算", "PreEndOfGame": "结算",
          "EndOfGame": "结算", "Reconnect": "重连"}


# ----------------------------------------------------------------------------
# 找到英雄联盟客户端的端口和密码
# ----------------------------------------------------------------------------
_PORT_RE = re.compile(r'--app-port[=\s]+"?(\d+)"?')
_TOKEN_RE = re.compile(r'--remoting-auth-token[=\s]+"?([\w\-]+)"?')
_DIR_RE = re.compile(r'--install-directory[=\s]+"?([^"\r\n]+?)"?(?:\s+--|\s*$)')


def _run_hidden(cmd: list, timeout: float = 15) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="ignore",
                              creationflags=0x08000000 if os.name == "nt" else 0)      # 不弹黑窗口
        return proc.stdout or ""
    except Exception:
        return ""


def find_client():
    """返回 (端口, 密码)；客户端没开返回 None。系统工具用绝对路径，不按名字找"""
    sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
    powershell = os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    out = ""
    if os.path.exists(powershell):
        out = _run_hidden([powershell, "-NoProfile", "-NonInteractive", "-Command",
                           "Get-CimInstance Win32_Process -Filter \"Name='LeagueClientUx.exe'\" | Select-Object -ExpandProperty CommandLine"])
    port, token = _PORT_RE.search(out), _TOKEN_RE.search(out)
    if port and token:
        return int(port.group(1)), token.group(1)
    folders = [m.group(1).strip() for m in [_DIR_RE.search(out)] if m] + [r"C:\Riot Games\League of Legends", r"D:\Riot Games\League of Legends"]
    for folder in folders:
        try:
            parts = (Path(folder) / "lockfile").read_text(encoding="utf-8", errors="ignore").strip().split(":")
            if len(parts) >= 5 and parts[2].isdigit():           # LeagueClient:PID:端口:密码:https
                return int(parts[2]), parts[3]
        except OSError:
            pass
    return None


class Client:
    """本机的英雄联盟客户端。它的证书是自己签的，所以不验证书（只连 127.0.0.1）"""

    def __init__(self, port: int, token: str):
        self.base = f"https://127.0.0.1:{port}"
        self.auth = "Basic " + base64.b64encode(f"riot:{token}".encode()).decode()
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def call(self, method: str, path: str, body=None, timeout: float = 8):
        """返回 (状态码, 解析好的内容)。连不上抛 OSError"""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": self.auth, "Accept": "application/json",
                                              **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read() or b"", e.code
        try:
            return status, json.loads(raw.decode("utf-8", "ignore")) if raw else None
        except ValueError:
            return status, None

    def get(self, path: str, timeout: float = 8):
        status, data = self.call("GET", path, timeout=timeout)
        return data if status == 200 else None

    def raw(self, path: str, timeout: float = 8) -> bytes:
        req = urllib.request.Request(self.base + path, headers={"Authorization": self.auth})
        with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
            return resp.read()


# ----------------------------------------------------------------------------
# 存在本机的东西：想用哪个头像、要不要自动重设、收藏、最近用过
# ----------------------------------------------------------------------------
def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def tidy_catalog(icons, sets) -> dict:
    """客户端给的头像目录 -> 界面用的精简版：{"icons": [{id, t 名字, y 年份, s [系列下标]}], "sets": [{name, n}]}。
    没有图的不要；名字末尾的「图标」两个字去掉（五千个都带，等于没有）"""
    in_sets, names = {}, []
    for s in sets if isinstance(sets, list) else []:
        ids = [i for i in (s.get("icons") or []) if isinstance(i, int)] if isinstance(s, dict) else []
        name = str((s or {}).get("displayName") or "").strip() if isinstance(s, dict) else ""
        if name and ids:
            for i in ids:
                in_sets.setdefault(i, []).append(len(names))
            names.append({"name": name, "n": len(ids)})
    rows = []
    for x in icons if isinstance(icons, list) else []:
        if not isinstance(x, dict) or not isinstance(x.get("id"), int) or not x.get("imagePath"):
            continue
        title = re.sub(r"\s*图标$", "", str(x.get("title") or "").strip()) or f"{x['id']} 号"
        rows.append({"id": x["id"], "t": title, "y": x.get("yearReleased") or 0, "s": in_sets.get(x["id"], [])})
    rows.sort(key=lambda r: r["id"], reverse=True)
    present = {r["id"] for r in rows}
    for n, s in enumerate(names):                # 系列里的数量只算有图的
        s["n"] = sum(1 for i, where in in_sets.items() if n in where and i in present)
    return {"icons": rows, "sets": names}


class Engine(threading.Thread):
    """后台那条线：盯着客户端，按需要设头像、重设头像。界面只读它的 snapshot()，改东西走它的几个方法"""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.RLock()
        cfg = _read_json(CONFIG_PATH, {})
        self.desired = cfg.get("desired") if isinstance(cfg.get("desired"), int) else None
        self.auto = cfg.get("auto") is not False
        self.favs = [i for i in cfg.get("favs") or [] if isinstance(i, int)]
        self.recent = [i for i in cfg.get("recent") or [] if isinstance(i, int)][:RECENT_KEEP]
        self.catalog = _read_json(CATALOG_PATH, {})
        self.client = None
        self.connected = False
        self.region = ""
        self.me = ""
        self.account_icon = None
        self.chat_icon = None
        self.owned: set = set()
        self.phase = ""
        self.log: list = []
        self.fights = 0
        self.resets = 0                 # 这次打开以来一共重设过几次（记录只留最近 40 条，次数要另外数）
        self.stopped = False
        self.last_attempt = 0.0
        self.catalog_from_client = False
        self.next_find = 0.0
        self.alive = True

    # --- 给界面的 ---
    def snapshot(self) -> dict:
        with self.lock:
            if not self.connected:
                status = "no_client"
            elif self.desired is None:
                status = "idle"
            elif self.stopped:
                status = "stopped"
            elif self.chat_icon == self.desired:
                status = "on"
            else:
                status = "reverted" if self.auto else "off"
            state = {"connected": self.connected, "region": self.region, "me": self.me, "phase": PHASES.get(self.phase, ""),
                     "account_icon": self.account_icon, "chat_icon": self.chat_icon, "desired": self.desired,
                     "auto": self.auto, "status": status, "owned": sorted(self.owned), "favs": list(self.favs),
                     "recent": list(self.recent), "log": list(self.log), "resets": self.resets,
                     "icons": len(self.catalog.get("icons") or [])}
            return state

    def _save(self) -> None:
        try:
            _write_json(CONFIG_PATH, {"desired": self.desired, "auto": self.auto, "favs": self.favs, "recent": self.recent})
        except OSError:
            pass

    def _note(self, kind: str, text: str, icon_id=None) -> None:
        """记一笔。kind 在界面上是一个带颜色的小标（换上 / 重设 / 停手……），所以 text 里不用再说一遍"""
        self.log.insert(0, {"t": int(time.time()), "kind": kind, "text": text, "id": icon_id})
        del self.log[LOG_KEEP:]

    # --- 界面让做的 ---
    def choose(self, icon_id: int) -> dict:
        """换成这个头像。返回 {"ok": 成没成, "text": 给人看的一句话}"""
        with self.lock:
            if not self.connected:
                return {"ok": False, "text": "客户端没开，开了再换"}
            self.desired, self.fights, self.stopped = icon_id, 0, False
            self.recent = ([icon_id] + [i for i in self.recent if i != icon_id])[:RECENT_KEEP]
            self._save()
            ok = self._apply(icon_id)
            if ok:
                self._note("set", f"{icon_id} 号", icon_id)
            else:
                self._note("error", f"{icon_id} 号没换上，客户端没接受")
            return {"ok": ok, "text": f"换成 {icon_id} 号" if ok else self.log[0]["text"]}

    def restore(self) -> dict:
        """不再用挑的头像，换回账号自己的那个"""
        with self.lock:
            self.desired, self.fights, self.stopped = None, 0, False
            self._save()
            if not self.connected or self.account_icon is None:
                return {"ok": True, "text": "已经不再重设"}
            ok = self._put_chat(self.account_icon)
            if ok:
                self._note("restore", f"自己的头像，{self.account_icon} 号", self.account_icon)
            else:
                self._note("error", "没换回自己的头像，客户端没接受")
            return {"ok": ok, "text": "换回自己的头像了" if ok else self.log[0]["text"]}

    def set_auto(self, on: bool) -> None:
        with self.lock:
            self.auto, self.fights, self.stopped = bool(on), 0, False
            self._save()

    def set_fav(self, icon_id: int, on: bool) -> None:
        with self.lock:
            self.favs = [i for i in self.favs if i != icon_id]
            if on:
                self.favs.insert(0, icon_id)
            self._save()


    # --- 和客户端打交道（都在拿着 lock 的时候调用）---
    def _put_chat(self, icon_id: int) -> bool:
        try:
            status, _ = self.client.call("PUT", "/lol-chat/v1/me", {"icon": icon_id})
            if status not in (200, 201, 204):
                return False
            for n in range(6):                              # 客户端接受以后要过一会儿才真的换上
                me = self.client.get("/lol-chat/v1/me") or {}
                if me.get("icon") == icon_id:
                    self.chat_icon = icon_id
                    return True
                time.sleep(0.35)
            return True                                     # 接受了但还没看到：下一轮再看
        except OSError:
            return False

    def _apply(self, icon_id: int) -> bool:
        self.last_attempt = time.time()
        if icon_id in self.owned and icon_id != self.account_icon:       # 自己有的：连账号头像一起换，到处都看得到
            try:
                status, _ = self.client.call("PUT", "/lol-summoner/v1/current-summoner/icon", {"profileIconId": icon_id})
                if status in (200, 201, 204):
                    self.account_icon = icon_id
            except OSError:
                pass
        return self._put_chat(icon_id)

    def _connect(self) -> bool:
        found = find_client()
        if not found:
            return False
        client = Client(*found)
        try:
            me = client.get("/lol-summoner/v1/current-summoner")
        except OSError:
            return False
        if not isinstance(me, dict) or not me.get("puuid"):
            return False                                    # 客户端开了但还没登录好
        self.client = client
        self.me = str(me.get("gameName") or me.get("displayName") or "")
        self.account_icon = me.get("profileIconId") if isinstance(me.get("profileIconId"), int) else None
        try:
            self.region = str((client.get("/riotclient/region-locale") or {}).get("region") or "")
            owned = client.get("/lol-inventory/v2/inventory/SUMMONER_ICON", timeout=15) or []
            self.owned = {x["itemId"] for x in owned if isinstance(x, dict) and isinstance(x.get("itemId"), int)
                          and (x.get("owned") or x.get("ownershipType") == "OWNED")} | set(range(0, 29))   # 0–28 号人人都有
            if not self.catalog_from_client:
                icons = client.get("/lol-game-data/assets/v1/summoner-icons.json", timeout=30)
                sets = client.get("/lol-game-data/assets/v1/summoner-icon-sets.json", timeout=30)
                fresh = tidy_catalog(icons, sets)
                if fresh["icons"]:
                    self.catalog, self.catalog_from_client = fresh, True
                    try:
                        _write_json(CATALOG_PATH, fresh)
                    except OSError:
                        pass
        except OSError:
            pass
        self.connected = True
        return True

    def _look(self) -> None:
        """看一眼客户端现在的样子；被改回去了、又该重设的话就重设"""
        phase = self.client.get("/lol-gameflow/v1/gameflow-phase", timeout=4)
        chat = self.client.get("/lol-chat/v1/me", timeout=4)
        if not isinstance(chat, dict):
            raise OSError("客户端没回应")
        phase = phase if isinstance(phase, str) else ""
        if phase != self.phase:
            self.phase, self.fights = phase, 0
            if self.stopped:
                self.stopped = False                        # 换了阶段：再给一次机会
        self.chat_icon = chat.get("icon") if isinstance(chat.get("icon"), int) else None
        now = time.time()
        if self.desired is None or not self.auto or self.stopped:
            return
        if self.chat_icon == self.desired:
            if self.fights and now - self.last_attempt > CALM_SECONDS:
                self.fights = 0
            return
        if now - self.last_attempt < RETRY_GAP_SECONDS:
            return
        where = PHASES.get(self.phase) or "客户端"
        was = f"变回 {self.chat_icon} 号" if self.chat_icon is not None else "被换掉了"
        self.fights += 1
        if self._apply(self.desired):
            self.resets += 1
            self._note("reset", f"{where}时{was}")        # 界面上这一行是：琥珀色的「改回」+ 这句话 + 绿色的「已重设」
        else:
            self._note("error", f"{where}时{was}，重设没成功")
        if self.fights >= FIGHT_LIMIT:
            self.stopped = True
            self._note("stop", f"连着 {FIGHT_LIMIT} 次被改回。客户端换个阶段，或者你再点一次头像，会重新开始")

    def run(self) -> None:
        while self.alive:
            try:
                with self.lock:
                    if not self.connected:
                        if time.time() >= self.next_find:
                            self.next_find = time.time() + 4
                            if self._connect() and self.desired is not None and self.auto:
                                self._note("resume", f"连上客户端，接着用 {self.desired} 号", self.desired)
                    else:
                        self._look()
            except OSError:
                with self.lock:
                    if self.connected:
                        self.connected, self.client, self.chat_icon, self.phase = False, None, None, ""
                        self._note("offline", "客户端关了，等它再开")
            except Exception as e:                           # 这条线不能死：出什么错都记一笔接着转
                with self.lock:
                    self._note("error", f"出了点问题：{type(e).__name__}")
            time.sleep(TICK_SECONDS)

    def icon_bytes(self, icon_id: int) -> bytes | None:
        """一张头像图。先看本机存的，没有再问客户端（并存下来）"""
        path = ICON_DIR / f"{icon_id}.jpg"
        try:
            return path.read_bytes()
        except OSError:
            pass
        client = self.client
        if client is None:
            return None
        try:
            raw = client.raw(f"/lol-game-data/assets/v1/profile-icons/{icon_id}.jpg")
        except (OSError, urllib.error.HTTPError):
            return None
        if raw[:2] != b"\xff\xd8":
            return None
        try:
            ICON_DIR.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        except OSError:
            pass
        return raw


# ----------------------------------------------------------------------------
# 给界面用的本机小服务器（只听 127.0.0.1）
# ----------------------------------------------------------------------------
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2", ".json": "application/json"}


def make_handler(engine: Engine, key: str, port: int):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def send(self, status: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, data, status: int = 200) -> None:
            self.send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def allowed(self, need_key: bool) -> bool:
            """只认本机这个地址来的请求；会改东西的还要带上页面里的那串钥匙（别的网页拿不到）"""
            if (self.headers.get("Host") or "") not in hosts:
                return False
            return not need_key or secrets.compare_digest(self.headers.get("X-Key") or "", key)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if not self.allowed(path.startswith("/api/")):
                return self.send(403, b"", "text/plain")
            if path == "/api/state":
                return self.json(engine.snapshot())
            if path == "/api/catalog":
                with engine.lock:
                    return self.json(engine.catalog or {"icons": [], "sets": []})
            m = re.fullmatch(r"/icon/(\d{1,6})\.jpg", path)
            if m:
                raw = engine.icon_bytes(int(m.group(1)))
                if raw is None:
                    return self.send(404, b"", "image/jpeg")
                return self.send(200, raw, "image/jpeg", "public, max-age=31536000, immutable")
            name = "index.html" if path == "/" else path.lstrip("/")
            target = (WEB / name).resolve()
            if WEB.resolve() not in target.parents or not target.is_file():
                return self.send(404, b"", "text/plain")
            body = target.read_bytes()
            if name == "index.html":
                body = body.replace(b"{{KEY}}", key.encode())
            return self.send(200, body, TYPES.get(target.suffix, "application/octet-stream"))

        def do_POST(self):
            if not self.allowed(True):
                return self.send(403, b"", "text/plain")
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
            except ValueError:
                body = {}
            body = body if isinstance(body, dict) else {}
            icon = body.get("id") if isinstance(body.get("id"), int) and 0 <= body.get("id") < 1000000 else None
            path = self.path.split("?", 1)[0]
            if path == "/api/choose" and icon is not None:
                return self.json({**engine.choose(icon), "state": engine.snapshot()})
            if path == "/api/restore":
                return self.json({**engine.restore(), "state": engine.snapshot()})
            if path == "/api/auto":
                engine.set_auto(bool(body.get("on")))
                return self.json({"ok": True, "state": engine.snapshot()})
            if path == "/api/fav" and icon is not None:
                engine.set_fav(icon, bool(body.get("on")))
                return self.json({"ok": True, "state": engine.snapshot()})
            self.send(404, b"", "text/plain")

    return Handler


def find_browser() -> str:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        for tail in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            path = os.path.join(base or "", tail)
            if base and os.path.exists(path):
                return path
    return ""


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/app.json", timeout=1.5) as resp:
            return (json.loads(resp.read().decode()) or {}).get("app") == APP
    except Exception:
        return False


def open_window(url: str):
    """用 Edge（没有就 Chrome）开一个没有地址栏的独立窗口。返回那个进程；电脑上两个都没有返回 None"""
    browser = find_browser()
    if not browser:
        return None
    # 单独的资料夹：窗口是一个独立的进程，关掉窗口这边就知道了
    return subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={DATA / 'window'}", "--window-size=1280,860",
                             "--no-first-run", "--no-default-browser-check"])


def set_data_dir(folder) -> None:
    """换一个地方存东西（嵌在别的程序里时，跟着那个程序的数据文件夹走）。要在 Wall / Engine 建出来之前调用"""
    global DATA, CONFIG_PATH, CATALOG_PATH, ICON_DIR
    DATA = Path(folder)
    CONFIG_PATH, CATALOG_PATH, ICON_DIR = DATA / "config.json", DATA / "catalog.json", DATA / "icons"


def saved_choice():
    """上次挑的头像编号——只有「挑过、而且开着被改回就重设」才算，否则返回 None。只读一下配置文件"""
    cfg = _read_json(CONFIG_PATH, {})
    return cfg.get("desired") if isinstance(cfg.get("desired"), int) and cfg.get("auto") is not False else None


class Wall:
    """后台那一套（盯客户端的那条线 + 给界面用的小服务器）包在一起。单独运行时 main() 用它；
    嵌在别的程序里（小警察插件的「彩蛋功能」）时那边也用它：start() 起后台，open() 再开窗口"""

    def __init__(self, port: int = PORT):
        self.port, self.url = port, f"http://127.0.0.1:{port}/"
        self.engine = None
        self.server = None
        self.guard = threading.Lock()

    def start(self) -> str:
        """把后台起起来。返回 "" = 好了（已经起过、或者别处已经开着一个头像墙，也算好了）；否则是起不来的原因"""
        with self.guard:
            if self.server is not None or already_running(self.port):
                return ""
            try:
                DATA.mkdir(parents=True, exist_ok=True)
                engine = Engine()
                server = ThreadingHTTPServer(("127.0.0.1", self.port), make_handler(engine, secrets.token_urlsafe(24), self.port))
            except OSError:
                return f"端口 {self.port} 被别的程序占着"
            server.daemon_threads = True
            engine.start()
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.engine, self.server = engine, server
            return ""

    def open(self) -> str:
        """起后台（没起的话）并开窗口。返回 "" = 开了；否则是开不了的原因"""
        problem = self.start()
        if problem:
            return problem
        try:
            return "" if open_window(self.url) is not None else "电脑上没找到 Edge 或 Chrome"
        except OSError as e:
            return f"窗口没开出来（{type(e).__name__}）"


def say(text: str) -> None:
    """说一句必须让人看到的话。双击 exe 开的时候没有黑窗口，print 没人看得见，所以弹一个框"""
    if sys.stdout is not None:
        print(text)
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "头像墙", 0x30)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="英雄联盟头像墙")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-window", action="store_true", help="只开后台，不开窗口（调试用）")
    args = parser.parse_args()

    wall = Wall(args.port)
    url = wall.url
    if already_running(args.port):
        print("已经开着了：", url)
        if not args.no_window:
            open_window(url)            # 已经有一个在后台了：再双击一次，就把窗口再开出来
        return 0
    problem = wall.start()
    if problem:
        say(f"头像墙开不起来：{problem}。关掉占着它的程序再开一次。")
        return 1
    engine, server = wall.engine, wall.server
    if server is None:                  # 就在刚才别处开起来了一个：那就用那个
        return 0
    print("头像墙开在", url)

    proc = None if args.no_window else open_window(url)
    try:
        if proc is not None:
            proc.wait()
        else:
            if not args.no_window:
                webbrowser.open(url)
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    engine.alive = False
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
