#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""clipboard_daemon: 本地剪贴板记录守护进程 + 无 GUI HTTP API。

设计约束（改代码前先读）：
1. 纯本地：只监听 127.0.0.1，不联网、不上传、无第三方依赖、无界面。
2. 默认暂停：进程启动后 recording=False，必须由用户显式开启。
3. 敏感格式跳过：密码管理器 / 隐身窗口标记的剪贴板一律不记录。
4. 覆盖剪贴板只发生在用户点「复制回剪贴板」时，且写回后抑制自身产生的重复条目。
5. 原子落盘：先写 .tmp 再 os.replace；损坏文件自动备份为 .corrupt.json。
6. 记录状态持久化到 settings.json：只有用户显式开过一次，重启后才会恢复记录；
   从未开启过则始终保持暂停（默认不采集这条红线不变）。

上游与移植说明见包根 NOTICE.md / CHANGES.md（MIT，Copyright (c) 2026 飞哥 (FeiGe)）。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac
import html as html_mod
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, quote, urlparse

APP_NAME = "clipboard-history"
VERSION = "1.9.0"
DEFAULT_PORT = 8765
# 8765 被其它程序占用时的降级范围：依次尝试 8766～8775，实际端口写进
# runtime.json，工具条 / 页面 / 监督进程通过 resolve_port() 自动跟随。
PORT_FALLBACK = 10

# 端口发现结果缓存：工具条每 2 秒轮询一次，不能每次都全端口扫一遍。
_port_cache: dict = {"port": None, "ok_until": 0.0, "fail_until": 0.0}


def resolve_port(preferred: int = DEFAULT_PORT, timeout: float = 0.8) -> int | None:
    """找到正在运行的守护进程端口；找不到返回 None。

    顺序：缓存 → runtime.json 里上次实际端口 → 逐个探活 preferred..preferred+PORT_FALLBACK。
    命中缓存 10 秒；全部落空时 3 秒内不再重扫（避免轮询期间反复空扫）。
    """
    now = time.time()
    c = _port_cache
    if c["port"] and now < c["ok_until"]:
        return c["port"]
    if now < c["fail_until"]:
        return None
    hi = preferred + PORT_FALLBACK
    try:
        data = json.loads((data_dir() / "runtime.json").read_text(encoding="utf-8"))
        rp = data.get("port")
        if isinstance(rp, int) and preferred <= rp <= hi and http_health(rp, timeout=timeout):
            c.update(port=rp, ok_until=now + 10.0)
            return rp
    except (OSError, ValueError):
        pass
    for p in range(preferred, hi + 1):
        if http_health(p, timeout=timeout):
            c.update(port=p, ok_until=now + 10.0)
            return p
    c["fail_until"] = now + 3.0
    return None
POLL_INTERVAL = 1.0
MAX_ITEMS = 200
MAX_CHARS = 20000
GMEM_MOVEABLE = 0x0002
CF_UNICODETEXT = 13

# 「secret clipboard」格式名：操作系统、主流浏览器与密码管理器公开使用的
# 敏感内容标记（公开事实，来源见包根 NOTICE 与 Microsoft 官方文档）。
# 保守策略：只要出现这些格式名就跳过；CanInclude* / CanUpload* 取值为 1 时也会少记，
# 宁可少记，不可误存。
SECRET_FORMAT_NAMES = {
    "Clipboard Viewer Ignore",
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard",
}

MSG = {
    "rec-on": ("已开始记录。复制任意文字后，返回本页即可看到。", "ok"),
    "rec-off": ("已暂停记录。已保存的条目不会消失。", "ok"),
    "copied": ("已写回系统剪贴板，去目标窗口按 Ctrl+V。", "ok"),
    "copy-fail": ("写入系统剪贴板失败，请检查剪贴板是否被其它程序占用。", "err"),
    "deleted": ("已删除该条。", "ok"),
    "pinned": ("已置顶。", "ok"),
    "unpinned": ("已取消置顶。", "ok"),
    "cleared": ("已清空未置顶的记录。", "ok"),
    "clear-confirm": ("清空需要确认，未执行。", "err"),
    "not-found": ("条目不存在，可能已被删除。", "err"),
    "bad-token": ("访问令牌无效，请用技能提供的完整链接打开。", "err"),
}

GATE_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>剪贴板记录 · 需要令牌</title>
<style>body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#fff;color:#1f2937;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.b{max-width:460px;padding:28px;border:1px solid #e4e7ec;border-radius:12px;line-height:1.8}
h1{font-size:16px;font-weight:500;margin:0 0 10px}p{font-size:13px;color:#667085;margin:0}
code{background:#f7f8fa;padding:2px 6px;border-radius:4px}</style></head>
<body><div class="b"><h1>剪贴板记录 · 需要访问令牌</h1>
<p>请用技能输出的完整链接（含 <code>?t=…</code>）重新打开工作台。令牌每次启动都会变化。</p></div></body></html>"""


def data_dir() -> Path:
    override = os.environ.get("CH_DATA_DIR")
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- 设置与进程管理
# 只持久化「用户显式做过的选择」，不记录任何内容。缺省状态下 recording=False。
DEFAULT_SETTINGS = {"recording": False, "user_copy_only": True}


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data.update({k: raw[k] for k in DEFAULT_SETTINGS if k in raw})
    except (OSError, ValueError):
        pass
    return data


def save_settings(**changes) -> dict:
    data = load_settings()
    data.update({k: v for k, v in changes.items() if k in DEFAULT_SETTINGS})
    p = settings_path()
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass
    return data


def ensure_stdout() -> None:
    """pythonw.exe 下 sys.stdout 为 None，任何 print 都会抛异常，这里兜住。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def emit(payload: dict) -> None:
    ensure_stdout()
    print(json.dumps(payload, ensure_ascii=False), flush=True)


# 本机直连 opener：系统代理（如 127.0.0.1:15610）的绕过列表不一定包含
# 127.0.0.1，走代理会把对自家 API 的请求变成 502。全部本机请求都绕开代理。
_direct_opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))


def http_health(port: int | None = None, timeout: float = 1.5) -> bool:
    if port is None:
        port = resolve_port(timeout=min(timeout, 0.8))
        if port is None:
            return False
    try:
        with _direct_opener.open(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as resp:
            return resp.status == 200 and b'"ok": true' in resp.read().replace(b'"ok":true', b'"ok": true')
    except (urlerror.URLError, OSError, ValueError):
        return False


def read_runtime(port: int | None = None) -> dict | None:
    p = data_dir() / "runtime.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if port is None:
        # 未指定端口：接受任意合法端口（serve --port 任意起点降级后都写在
        # runtime.json 里，该文件只由活着的守护进程写入，可信）。
        rp = data.get("port")
        if not isinstance(rp, int) or not 1024 <= rp <= 65535:
            return None
        return data
    if data.get("port") != port:
        return None
    return data


def api_call(path: str, payload: dict | None = None, port: int | None = None,
             token: str | None = None, timeout: float = 3.0) -> dict:
    """工具条与 CLI 共用的一层极薄 HTTP 客户端（只访问 127.0.0.1）。

    port 不传时经 resolve_port() 自动发现实际端口（含降级端口），调用方无需感知。
    """
    if port is None:
        port = resolve_port() or DEFAULT_PORT
    rt = read_runtime(port) or {}
    token = token or rt.get("token", "")
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urlrequest.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("X-CH-Token", token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with _direct_opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urlerror.HTTPError as exc:
        try:
            return json.loads(exc.read() or b"{}")
        except ValueError:
            return {"ok": False, "error": f"http-{exc.code}"}
    except (urlerror.URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def spawn_detached(argv: list[str]) -> None:
    """脱离当前进程/控制台启动，关掉终端也不会带走守护进程。"""
    exe = sys.executable
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        pyw = Path(exe).with_name("pythonw.exe")
        if pyw.exists():
            exe = str(pyw)
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED|NEW_GROUP|NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([exe, *argv], **kwargs)


def ensure_running(port: int = DEFAULT_PORT, timeout: float = 20.0,
                   script: Path | None = None) -> dict:
    """幂等：已在跑就复用（含降级端口上的实例），没跑就后台拉起。"""
    hi = port + PORT_FALLBACK

    def found(p: int, started: bool) -> dict:
        rt = read_runtime(p) or {}
        return {"ok": True, "started": started, "url": rt.get("url"), "port": p,
                "pid": rt.get("pid"), "recording": _recording_state(p)}

    # 已有实例在跑（可能在降级端口上）→ 直接复用，不重复拉起
    for p in range(port, hi + 1):
        if http_health(p) and (read_runtime(p) or {}).get("url"):
            return found(p, False)
    script = script or Path(__file__).resolve()
    argv = [str(script), "serve", "--port", str(port)]   # serve 内部自行降级
    spawn_escaped(argv)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for p in range(port, hi + 1):
            if http_health(p) and (read_runtime(p) or {}).get("url"):
                return found(p, True)
        time.sleep(0.2)
    return {"ok": False, "started": False,
            "error": f"启动超时：{port}～{hi} 端口均不可用", "port": port}


def _recording_state(port: int = DEFAULT_PORT) -> bool | None:
    state = api_call("/api/state", port=port)
    return state.get("recording") if state.get("ok") else None


def stop_running(port: int | None = None) -> dict:
    if port is None:
        port = resolve_port() or DEFAULT_PORT
    if not http_health(port):
        return {"ok": True, "stopped": False, "message": "服务本来就没在运行"}
    api_call("/api/shutdown", {}, port=port)
    deadline = time.time() + 6.0
    while time.time() < deadline:
        if not http_health(port):
            return {"ok": True, "stopped": True}
        time.sleep(0.2)
    return {"ok": False, "stopped": False, "error": "停止超时，可能仍有请求占用"}


def spawn_escaped(argv: list[str], exe: str | None = None) -> bool:
    """脱离进程树启动：DETACHED_PROCESS + BREAKAWAY_FROM_JOB + 无窗口。

    不用 WMI / PowerShell：实测本机安全策略会拦 Invoke-CimMethod，一次阻塞 20 秒，
    而钩子超时只有 10 秒。这里直接 CreateProcess，毫秒级返回。
    """
    exe = exe or _windows_pyw()
    base: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                  "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform != "win32":
        subprocess.Popen([exe, *argv], start_new_session=True, **base)
        return True
    DETACHED, NEW_GROUP, NO_WINDOW, BREAKAWAY = 0x08, 0x200, 0x08000000, 0x01000000
    try:
        subprocess.Popen([exe, *argv], creationflags=DETACHED | NEW_GROUP | NO_WINDOW | BREAKAWAY,
                         **base)
    except OSError:
        # 某些父进程的 Job 不允许 breakaway，退一步仍用 detached
        subprocess.Popen([exe, *argv], creationflags=DETACHED | NEW_GROUP | NO_WINDOW, **base)
    return True


def _windows_pyw() -> str:
    exe = sys.executable
    pyw = Path(exe).with_name("pythonw.exe")
    return str(pyw if pyw.exists() else Path(exe))


class Store:
    """线程安全的本地条目存储。items 按插入顺序保存，展示时置顶优先。"""

    def __init__(self, path: Path | None = None, max_items: int = MAX_ITEMS,
                 ignore_patterns: list[str] | None = None):
        self.path = path or (data_dir() / "items.json")
        self.max_items = max_items
        self.ignore_patterns = [re.compile(p, re.I) for p in (ignore_patterns or [])]
        self.lock = threading.RLock()
        self.items: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                self.items = payload["items"][: self.max_items]
        except (OSError, ValueError, TypeError):
            try:
                shutil.copy2(self.path, self.path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            self.items = []

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {"version": 1, "updated_at": time.time(), "items": self.items}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, text: str, kind: str = "text", ts: float | None = None,
            app: str | None = None) -> dict | None:
        text = (text or "").replace("\r\n", "\n")
        if not text.strip():
            return None
        truncated = len(text) > MAX_CHARS
        if truncated:
            text = text[:MAX_CHARS] + "\n…[已截断]"
        for pat in self.ignore_patterns:
            if pat.search(text):
                return None
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self.lock:
            if self.items and self.items[0].get("hash") == digest:
                return None
            item = {
                "id": uuid.uuid4().hex[:12],
                "ts": ts or time.time(),
                "kind": kind,
                "text": text,
                "chars": len(text),
                "hash": digest,
                "pinned": False,
                "truncated": truncated,
                "app": app or None,
            }
            self.items.insert(0, item)
            overflow = len(self.items) - self.max_items
            if overflow > 0:
                kept, removed = [], 0
                for it in reversed(self.items):
                    if removed < overflow and not it.get("pinned"):
                        removed += 1
                        continue
                    kept.append(it)
                self.items = list(reversed(kept))
            self._save()
            return item

    def get(self, item_id: str) -> dict | None:
        with self.lock:
            return next((i for i in self.items if i["id"] == item_id), None)

    def delete(self, item_id: str) -> bool:
        with self.lock:
            n = len(self.items)
            self.items = [i for i in self.items if i["id"] != item_id]
            if len(self.items) != n:
                self._save()
                return True
            return False

    def clear(self) -> int:
        with self.lock:
            n = len(self.items)
            self.items = [i for i in self.items if i.get("pinned")]
            self._save()
            return n - len(self.items)

    def pin(self, item_id: str, pinned: bool) -> bool:
        with self.lock:
            item = self.get(item_id)
            if not item:
                return False
            item["pinned"] = bool(pinned)
            self._save()
            return True

    def list(self, query: str = "", limit: int = MAX_ITEMS) -> list[dict]:
        q = (query or "").strip().lower()
        with self.lock:
            rows = [dict(i) for i in self.items]
            rows.sort(key=lambda i: (not i.get("pinned"), -i.get("ts", 0)))
        if q:
            rows = [i for i in rows if q in i.get("text", "").lower()]
        return rows[:limit]


class Win32Clipboard:
    """用 ctypes 直接读/写 Windows 剪贴板，避免每轮起 PowerShell 进程。"""

    def __init__(self):
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        u, k = self.u, self.k
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.CloseClipboard.argtypes = []
        u.CloseClipboard.restype = wintypes.BOOL
        u.EmptyClipboard.argtypes = []
        u.EmptyClipboard.restype = wintypes.BOOL
        u.EnumClipboardFormats.argtypes = [wintypes.UINT]
        u.EnumClipboardFormats.restype = wintypes.UINT
        u.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
        u.GetClipboardFormatNameW.restype = ctypes.c_int
        u.GetClipboardData.argtypes = [wintypes.UINT]
        u.GetClipboardData.restype = ctypes.c_void_p
        u.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
        u.SetClipboardData.restype = ctypes.c_void_p
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = [ctypes.c_void_p]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        k.GlobalUnlock.restype = wintypes.BOOL
        k.GlobalFree.argtypes = [ctypes.c_void_p]
        k.GlobalFree.restype = ctypes.c_void_p

    def read(self) -> dict | None:
        if not self.u.OpenClipboard(None):
            return None
        try:
            names, fmt, buf = [], 0, ctypes.create_unicode_buffer(256)
            while True:
                fmt = self.u.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                if self.u.GetClipboardFormatNameW(fmt, buf, 256):
                    names.append(buf.value)
                if len(names) > 200:
                    break
            text = None
            handle = self.u.GetClipboardData(CF_UNICODETEXT)
            if handle:
                ptr = self.k.GlobalLock(handle)
                if ptr:
                    try:
                        text = ctypes.wstring_at(ptr)
                    finally:
                        self.k.GlobalUnlock(handle)
            return {"text": text, "formats": names}
        finally:
            self.u.CloseClipboard()

    def write(self, text: str) -> bool:
        raw = text.encode("utf-16-le") + b"\x00\x00"
        handle = self.k.GlobalAlloc(GMEM_MOVEABLE, len(raw))
        if not handle:
            return False
        ptr = self.k.GlobalLock(handle)
        if not ptr:
            self.k.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, ctypes.create_string_buffer(raw), len(raw))
        self.k.GlobalUnlock(handle)
        if not self.u.OpenClipboard(None):
            self.k.GlobalFree(handle)
            return False
        try:
            self.u.EmptyClipboard()
            if not self.u.SetClipboardData(CF_UNICODETEXT, handle):
                self.k.GlobalFree(handle)
                return False
            handle = None  # 所有权已交给系统
            return True
        finally:
            self.u.CloseClipboard()


_FG_CACHE: dict = {}


def foreground_app() -> str | None:
    """当前前台窗口的进程名，例如 chrome、explorer、代码编辑器。

    剪贴板是全系统共享的，谁写进去不会留名。记录时顺手抓一下前台窗口，
    用户回头问「这条我没复制过，哪来的」才有据可查——是哪个程序在写。
    任何异常都只返回 None，绝不因为标注来源影响记录本身。
    """
    if os.name != "nt":
        return None
    try:
        pair = _FG_CACHE.get("api")
        if pair is None:
            u = ctypes.WinDLL("user32", use_last_error=True)
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            u.GetForegroundWindow.restype = wintypes.HWND
            u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            u.GetWindowThreadProcessId.restype = wintypes.DWORD
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k.OpenProcess.restype = wintypes.HANDLE
            k.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            k.QueryFullProcessImageNameW.restype = wintypes.BOOL
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            k.CloseHandle.restype = wintypes.BOOL
            pair = (u, k)
            _FG_CACHE["api"] = pair
        u, k = pair
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        # PROCESS_QUERY_LIMITED_INFORMATION：只查路径，不需要读内存，权限最小
        handle = k.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            return Path(buf.value).stem or None
        finally:
            k.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return None


def _window_pid(hwnd) -> int | None:
    try:
        u = ctypes.WinDLL("user32")
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value or None
    except (OSError, AttributeError, ValueError):
        return None


def clipboard_owner_pid() -> int | None:
    """最后一次向剪贴板写入数据的窗口所属进程；取不到返回 None。

    用户在应用里按 Ctrl+C 时，写剪贴板的正是那个前台应用；后台程序
    （自动化脚本、别的会话）写入时通常不在前台。用它做判定不需要
    全局键盘钩子——v1.6 的 WH_KEYBOARD_LL 钩子回调跑在 Python 线程里
    要抢 GIL，守护进程繁忙时回调被拖延，实测会把全系统键盘卡死，
    v1.7.0 起彻底废弃。
    """
    if os.name != "nt":
        return None
    try:
        u = ctypes.WinDLL("user32")
        return _window_pid(u.GetClipboardOwner())
    except (OSError, AttributeError, ValueError):
        return None


def foreground_pid() -> int | None:
    """当前前台窗口所属进程；取不到返回 None。"""
    if os.name != "nt":
        return None
    try:
        u = ctypes.WinDLL("user32")
        return _window_pid(u.GetForegroundWindow())
    except (OSError, AttributeError, ValueError):
        return None


class Clipboard:
    def __init__(self):
        self.platform = "windows" if os.name == "nt" else ("macos" if sys.platform == "darwin" else "linux")
        self._win = Win32Clipboard() if os.name == "nt" else None

    @property
    def secret_filter(self) -> bool:
        return self._win is not None

    def read(self) -> dict | None:
        if self._win:
            return self._win.read()
        try:
            cmd = ["pbpaste"] if self.platform == "macos" else ["wl-paste", "--no-newline"]
            out = subprocess.run(cmd, capture_output=True, timeout=2)
            if out.returncode != 0 and self.platform == "linux":
                out = subprocess.run(["xclip", "-o", "-selection", "clipboard"],
                                     capture_output=True, timeout=2)
            if out.returncode != 0:
                return None
            return {"text": out.stdout.decode("utf-8", "replace"), "formats": []}
        except (OSError, subprocess.SubprocessError):
            return None

    def write(self, text: str) -> bool:
        if self._win:
            return self._win.write(text)
        try:
            cmd = ["pbcopy"] if self.platform == "macos" else ["wl-copy"]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"), timeout=2)
            return p.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


class Runtime:
    def __init__(self, port: int, store: Store, clip: Clipboard):
        self.port = port
        self.store = store
        self.clip = clip
        self.token = secrets.token_urlsafe(24)
        self.state = {
            "recording": False,
            "stop": threading.Event(),
            "ignored": set(),      # 删除/复制/清空后剪贴板里残留的文本，暂不收录
            "last_clip": None,     # 上一轮剪贴板文本，用来区分“残留”与“用户又复制了”
            "last_added": None,
            "last_skip": None,
            "last_error": None,
            "started_at": time.time(),
        }

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def write_runtime_file(self) -> Path:
        p = data_dir() / "runtime.json"
        payload = {
            "pid": os.getpid(), "port": self.port, "token": self.token,
            "url": self.url, "started_at": self.state["started_at"],
            "storage_path": str(self.store.path), "version": VERSION,
        }
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return p


def set_recording(rt: Runtime, value: bool) -> bool:
    """切换记录状态并持久化，作为「用户显式选择过」的凭据。"""
    rt.state["recording"] = bool(value)
    save_settings(recording=bool(value))
    return rt.state["recording"]


def monitor_loop(rt: Runtime) -> None:
    st, store, clip = rt.state, rt.store, rt.clip
    while not st["stop"].is_set():
        if st["recording"]:
            try:
                res = clip.read()
                if res is None:
                    st["last_error"] = "clipboard-busy"
                else:
                    st["last_error"] = None
                    text, formats = res.get("text"), res.get("formats", [])
                    if any(f in SECRET_FORMAT_NAMES for f in formats):
                        st["last_skip"] = "secret-format"
                    elif not text or not text.strip():
                        st["last_skip"] = "non-text"
                    else:
                        prev = st.get("last_clip")
                        st["last_clip"] = text
                        skip = False
                        # 只收录「前台应用写入」的剪贴板内容（用户主动复制的
                        # 几乎总是来自焦点窗口）：后台程序写的（自动化脚本、
                        # 别的会话）跳过。属主或前台取不到时放行，宁可多记。
                        if st.get("user_copy_only", True):
                            owner = clipboard_owner_pid()
                            fg = foreground_pid()
                            # 属主取不到 = 无窗口的程序写入（脚本/自动化），同样跳过；
                            # 用户在应用里 Ctrl+C 时属主一定是那个前台应用。
                            if owner is None or (fg is not None and owner != fg):
                                st["last_skip"] = "bg-write"
                                skip = True
                        if not skip and text in st.get("ignored", ()):
                            if text != prev:
                                # 剪贴板刚变成它 = 一次新的写入（用户又复制了），解除忽略并照常记录
                                st["ignored"].discard(text)
                            else:
                                # 删除/复制/清空后剪贴板里残留的同一内容，不收录
                                st["last_skip"] = "dropped"
                                skip = True
                        if not skip:
                            item = store.add(text, app=foreground_app())
                            if item:
                                st["last_added"] = item["ts"]
                                st["last_skip"] = None
            except Exception as exc:  # 守护线程不能因为单次异常退出
                st["last_error"] = f"{type(exc).__name__}: {exc}"
        st["stop"].wait(POLL_INTERVAL)


def _esc(value) -> str:
    return html_mod.escape(str(value), quote=True)


def _fmt_ts(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m/%d %H:%M:%S")
    except (ValueError, OSError, TypeError):
        return "—"


def render_items(rt: Runtime, query: str = "") -> str:
    rows = rt.store.list(query)
    if not rows:
        if query:
            return ('<div class="empty"><strong>没有匹配的记录</strong>换个关键词试试，或点「刷新」看全部。</div>')
        return ('<div class="empty"><strong>还没有记录</strong>点右上角「开始记录」，'
                '然后复制任意文字，回到本页刷新即可看到。</div>')
    token = _esc(rt.token)
    out: list[str] = []
    for n, it in enumerate(rows, 1):
        text = it.get("text", "")
        long_text = len(text) > 180 or text.count("\n") > 3
        preview = text if not long_text else text[:180].rstrip() + "…"
        if long_text:
            body = (f'<details class="textbox"><summary>{_esc(preview)}</summary>'
                    f'<div class="full">{_esc(text)}</div></details>')
        else:
            body = f'<div class="text">{_esc(text)}</div>'
        pin_label = "取消置顶" if it.get("pinned") else "置顶"
        pin_value = "0" if it.get("pinned") else "1"
        badges = []
        if it.get("app"):
            badges.append(f'<span class="badge app" title="写入这条时处于前台的程序，'
                          f'用来判断内容来源">{_esc(it["app"])}</span>')
        badges.append(f'<span class="badge">文本 · {it.get("chars", 0)} 字</span>')
        if it.get("truncated"):
            badges.append('<span class="badge cut">已截断</span>')
        if it.get("pinned"):
            badges.append('<span class="badge pin">已置顶</span>')
        out.append(f'''<div class="card{' pinned' if it.get("pinned") else ''}">
  <div class="meta"><span class="idx">#{n}</span><span class="time">{_esc(_fmt_ts(it.get("ts", 0)))}</span>{''.join(badges)}</div>
  {body}
  <div class="actions">
    <form method="post" action="/form/copy"><input type="hidden" name="t" value="{token}"><input type="hidden" name="id" value="{_esc(it["id"])}"><button type="submit">复制回剪贴板</button></form>
    <form method="post" action="/form/pin"><input type="hidden" name="t" value="{token}"><input type="hidden" name="id" value="{_esc(it["id"])}"><input type="hidden" name="pinned" value="{pin_value}"><button type="submit">{pin_label}</button></form>
    <form method="post" action="/form/delete"><input type="hidden" name="t" value="{token}"><input type="hidden" name="id" value="{_esc(it["id"])}"><button type="submit" class="danger">删除</button></form>
  </div>
</div>''')
    return "\n".join(out)


def render_page(rt: Runtime, query: str = "", msg: str = "") -> str:
    template_path = Path(__file__).resolve().parent.parent / "web" / "index.html"
    tpl = template_path.read_text(encoding="utf-8")
    recording = bool(rt.state["recording"])
    msg_html = ""
    if msg in MSG:
        text, kind = MSG[msg]
        msg_html = f'<div class="msg {kind}">{_esc(text)}</div>'
    meta = '<meta http-equiv="refresh" content="4">' if (recording and not query) else ""
    values = {
        "__META_REFRESH__": meta,
        "__STATE_CLASS__": "status on" if recording else "status",
        "__STATE_TEXT__": "记录中" if recording else "已暂停",
        "__RECORD_LABEL__": "暂停记录" if recording else "开始记录",
        "__RECORD_CLASS__": "" if recording else "primary",
        "__NOTICE_STYLE__": "none" if recording else "block",
        "__MSG_HTML__": msg_html,
        "__TOKEN__": _esc(rt.token),
        "__QUERY__": _esc(query),
        "__COUNT__": str(len(rt.store.items)),
        "__STORAGE__": _esc(rt.store.path),
        "__VERSION__": VERSION,
    }
    for key, val in values.items():
        tpl = tpl.replace(key, val)
    return tpl.replace("__ITEMS__", render_items(rt, query))


def _origin_allowed(origin: str | None) -> bool:
    """预览面板可能用非同源容器；只认本机来源，安全性由一次性令牌保证。"""
    if not origin or origin == "null":
        return True
    try:
        p = urlparse(origin)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and p.hostname in ("127.0.0.1", "localhost", "::1")


def make_handler(rt: Runtime):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"ClipboardHistory/{VERSION}"

        def log_message(self, *_args):
            pass

        def _cors_headers(self):
            origin = self.headers.get("Origin")
            if origin and _origin_allowed(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CH-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Vary", "Origin")

        def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, bytes):
                data = body
            elif isinstance(body, str):
                data = body.encode("utf-8")
            else:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self._cors_headers()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

        def _redirect(self, msg: str):
            loc = f"/?t={quote(rt.token)}&msg={quote(msg)}"
            self.send_response(303)
            self.send_header("Location", loc)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self._cors_headers()
            self.end_headers()

        def _read_body(self) -> dict:
            n = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(n) if n else b""
            ctype = self.headers.get("Content-Type", "")
            if "application/json" in ctype:
                try:
                    obj = json.loads(raw.decode("utf-8"))
                    return obj if isinstance(obj, dict) else {}
                except (ValueError, UnicodeDecodeError):
                    return {}
            if "application/x-www-form-urlencoded" in ctype:
                parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
                return {k: v[-1] for k, v in parsed.items()}
            return {}

        def _token_ok(self, body: dict | None = None, allow_query: bool = False) -> bool:
            supplied = self.headers.get("X-CH-Token", "")
            if not supplied and body:
                supplied = str(body.get("t", ""))
            if not supplied and allow_query:
                supplied = parse_qs(urlparse(self.path).query).get("t", [""])[0]
            return bool(supplied) and hmac.compare_digest(supplied, rt.token)

        def _api_ok(self, body: dict | None = None, allow_query: bool = False) -> bool:
            origin = self.headers.get("Origin")
            if not _origin_allowed(origin):
                self._json(403, {"error": "origin-not-allowed"})
                return False
            if not self._token_ok(body, allow_query):
                self._json(403, {"error": "bad-token", "hint": "请用技能提供的完整链接打开工作台"})
                return False
            return True

        def do_OPTIONS(self):
            if not _origin_allowed(self.headers.get("Origin")):
                self._send(403, "", "text/plain; charset=utf-8")
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._cors_headers()
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if parsed.path == "/api/health":
                self._json(200, {"ok": True, "version": VERSION})
                return
            if parsed.path in ("/", "/index.html"):
                token = qs.get("t", [""])[0]
                if not token or not hmac.compare_digest(token, rt.token):
                    self._send(200, GATE_HTML, "text/html; charset=utf-8")
                    return
                self._send(200, render_page(rt, qs.get("q", [""])[0], qs.get("msg", [""])[0]),
                           "text/html; charset=utf-8")
                return
            if parsed.path == "/partial/items":
                if not self._api_ok(allow_query=True):
                    return
                self._send(200, render_items(rt, qs.get("q", [""])[0]), "text/html; charset=utf-8")
                return
            if not self._api_ok(allow_query=True):
                return
            if parsed.path == "/api/state":
                self._json(200, {
                    "ok": True, "version": VERSION,
                    "recording": rt.state["recording"],
                    "count": len(rt.store.items),
                    "storage_path": str(rt.store.path),
                    "platform": rt.clip.platform,
                    "secret_filter": rt.clip.secret_filter,
                    "poll_interval": POLL_INTERVAL,
                    "max_items": rt.store.max_items,
                    "last_added": rt.state["last_added"],
                    "last_skip": rt.state["last_skip"],
                    "last_error": rt.state["last_error"],
                    "user_copy_only": rt.state.get("user_copy_only", True),
                })
            elif parsed.path == "/api/items":
                try:
                    limit = int(qs.get("limit", [MAX_ITEMS])[0])
                except ValueError:
                    limit = MAX_ITEMS
                self._json(200, {"ok": True, "items": rt.store.list(qs.get("q", [""])[0], limit)})
            else:
                self._json(404, {"error": "not-found"})

        def do_POST(self):
            body = self._read_body()
            path = urlparse(self.path).path
            if path.startswith("/form/"):
                if not self._token_ok(body):
                    self._send(403, GATE_HTML, "text/html; charset=utf-8")
                    return
                if path == "/form/toggle-recording":
                    set_recording(rt, not rt.state["recording"])
                    self._redirect("rec-on" if rt.state["recording"] else "rec-off")
                elif path == "/form/copy":
                    item = rt.store.get(str(body.get("id", "")))
                    if not item:
                        self._redirect("not-found")
                        return
                    ok = rt.clip.write(item["text"])
                    if ok:
                        rt.state["ignored"].add(item["text"])
                        rt.state["last_clip"] = item["text"]  # 监控视作“剪贴板没变”
                    self._redirect("copied" if ok else "copy-fail")
                elif path == "/form/delete":
                    it = rt.store.get(str(body.get("id", "")))
                    ok = rt.store.delete(str(body.get("id", "")))
                    if ok and it:
                        # 该内容往往还在系统剪贴板里，屏蔽掉，否则下一秒就“复活”
                        rt.state["ignored"].add(it["text"])
                    self._redirect("deleted" if ok else "not-found")
                elif path == "/form/pin":
                    ok = rt.store.pin(str(body.get("id", "")), str(body.get("pinned", "1")) == "1")
                    self._redirect("pinned" if ok and str(body.get("pinned", "1")) == "1"
                                   else ("unpinned" if ok else "not-found"))
                elif path == "/form/clear":
                    if body.get("confirm") != "清空":
                        self._redirect("clear-confirm")
                    else:
                        cur = (rt.clip.read() or {}).get("text")
                        rt.store.clear()
                        if cur:
                            rt.state["ignored"].add(cur)
                        self._redirect("cleared")
                else:
                    self._redirect("not-found")
                return
            if not self._api_ok(body):
                return
            if path == "/api/recording":
                set_recording(rt, bool(body.get("enabled")))
                self._json(200, {"ok": True, "recording": rt.state["recording"]})
            elif path == "/api/copy":
                item = rt.store.get(str(body.get("id", "")))
                if not item:
                    self._json(404, {"error": "item-not-found"})
                    return
                ok = rt.clip.write(item["text"])
                if ok:
                    rt.state["ignored"].add(item["text"])
                    rt.state["last_clip"] = item["text"]  # 监控视作“剪贴板没变”
                self._json(200 if ok else 500,
                           {"ok": ok, "text": item["text"] if ok else None,
                            "error": None if ok else "写入系统剪贴板失败"})
            elif path == "/api/delete":
                it = rt.store.get(str(body.get("id", "")))
                ok = rt.store.delete(str(body.get("id", "")))
                if ok and it:
                    rt.state["ignored"].add(it["text"])
                self._json(200, {"ok": ok})
            elif path == "/api/pin":
                self._json(200, {"ok": rt.store.pin(str(body.get("id", "")),
                                                    bool(body.get("pinned", True)))})
            elif path == "/api/clear":
                if body.get("confirm") != "清空":
                    self._json(400, {"error": "confirm-required"})
                    return
                cur = (rt.clip.read() or {}).get("text")
                removed = rt.store.clear()
                if cur:
                    rt.state["ignored"].add(cur)
                self._json(200, {"ok": True, "removed": removed})
            elif path == "/api/shutdown":
                self._json(200, {"ok": True, "message": "即将停止"})
                rt.state["stop"].set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json(404, {"error": "not-found"})

    return Handler


class LocalServer(ThreadingHTTPServer):
    """只绑 127.0.0.1 的 HTTP 服务。

    Windows 上必须关掉 SO_REUSEADDR：它允许两个进程同时绑同一端口，结果两个实例
    各自持有一份随机令牌，页面拿着 runtime.json 里的令牌却打到另一个实例上，
    表现为「令牌无效」这种极难排查的错。关掉后第二个实例绑定直接失败，语义清晰。
    """
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def serve(port: int = DEFAULT_PORT, store_path: Path | None = None,
          recording: bool | None = None, max_items: int = MAX_ITEMS,
          resume: bool = True) -> Runtime | None:
    ensure_stdout()
    if http_health(port):
        existing = read_runtime(port) or {}
        emit({"ok": True, "already_running": True, "url": existing.get("url"),
              "pid": existing.get("pid"), "port": port, "version": VERSION})
        return None
    store = Store(store_path, max_items=max_items)
    rt = Runtime(port, store, Clipboard())
    # recording 为 None 时沿用上次用户显式选择；从未开启过则保持暂停
    rt.state["recording"] = bool(load_settings()["recording"]) if recording is None and resume \
        else bool(recording)
    threading.Thread(target=monitor_loop, args=(rt,), daemon=True, name="clipboard-monitor").start()
    # 用户复制意图检测：只记 Ctrl+C/X，其它程序写入剪贴板的内容不收录
    rt.state["user_copy_only"] = bool(load_settings().get("user_copy_only", True))
    try:
        httpd = LocalServer(("127.0.0.1", port), make_handler(rt))
    except OSError as exc:
        # 端口被占：可能是另一个实例刚起来（探活有时间差）。先确认，别把状态文件写脏。
        if http_health(port):
            emit({"ok": True, "already_running": True, "port": port,
                  "url": (read_runtime(port) or {}).get("url")})
            return None
        # 自动降级：8765 被其它程序长期占用 → 依次尝试 8766～8775
        bound = None
        for alt in range(port + 1, port + PORT_FALLBACK + 1):
            if http_health(alt):        # 已有我们的实例落在该端口
                emit({"ok": True, "already_running": True, "port": alt,
                      "url": (read_runtime(alt) or {}).get("url")})
                return None
            try:
                httpd = LocalServer(("127.0.0.1", alt), make_handler(rt))
                bound = alt
                break
            except OSError:
                continue
        if bound is None:
            emit({"ok": False,
                  "error": f"端口 {port}～{port + PORT_FALLBACK} 全部被占用", "port": port})
            return None
        rt.port = bound                 # 降级成功：runtime.json / 页面 URL 全部跟随
        try:
            with (data_dir() / "boot.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  "
                         f"port {port} busy -> fallback {bound}\n")
        except OSError:
            pass
    rt.write_runtime_file()
    emit({"ok": True, "url": rt.url, "port": rt.port, "pid": os.getpid(),
          "recording": rt.state["recording"], "storage_path": str(store.path),
          "platform": rt.clip.platform, "version": VERSION})
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        rt.state["stop"].set()
        httpd.server_close()
    return rt


def main() -> int:
    ensure_stdout()
    ap = argparse.ArgumentParser(description="剪贴板记录本地服务（v" + VERSION + "）")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="启动本地服务（默认沿用上次记录状态，从未开启则暂停）")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--recording", action="store_true", help="本次强制开始记录")
    s.add_argument("--paused", action="store_true", help="本次强制暂停（忽略上次状态）")
    s.add_argument("--max-items", type=int, default=MAX_ITEMS)
    s.add_argument("--store", type=Path, default=None)

    e = sub.add_parser("ensure", help="幂等自启：已在跑就复用，没跑就在后台拉起（推荐入口）")
    e.add_argument("--port", type=int, default=DEFAULT_PORT)

    sub.add_parser("stop", help="停止本地服务")
    sub.add_parser("status", help="打印运行状态与数据位置")

    args = ap.parse_args()

    if args.cmd == "serve":
        recording = True if args.recording else (False if args.paused else None)
        serve(args.port, args.store, recording, args.max_items,
              resume=not args.paused)
        return 0
    if args.cmd == "ensure":
        res = ensure_running(args.port)
        emit(res)
        return 0 if res.get("ok") else 1
    if args.cmd == "stop":
        res = stop_running()
        emit(res)
        return 0 if res.get("ok") else 1

    rt = read_runtime()
    if not rt:
        emit({"ok": False, "running": False,
              "storage_path": str(data_dir() / "items.json"),
              "data_dir": str(data_dir())})
        return 1
    emit({"ok": True, "running": http_health(), "url": rt.get("url"), "pid": rt.get("pid"),
          "port": rt.get("port"), "storage_path": rt.get("storage_path"),
          "recording": _recording_state(), "data_dir": str(data_dir())})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
