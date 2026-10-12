"""
Focus Lock v3 - a Windows study timer that is hard to escape.

Run:   python focus_lock.py          (Windows, Python 3.8+, no extra installs)

TWO MODES
  Full lock   Black screen + big timer on EVERY monitor. Nothing else usable.
              Blocks Win key, Alt+Tab, Alt+Esc, Ctrl+Esc.
  Study mode  Only the apps you allow stay usable; everything else is
              minimised the moment it appears. Blocks the Win key (Alt+Tab
              still works so you can hop between allowed apps).

CHROME TAB LOCK (Study mode)
  1. Open the page you want to study in your normal Chrome.
  2. Press Start. Focus Lock locks THAT tab:
       - no new tabs or windows (they are closed instantly),
       - the tab's address can't be changed (you get bounced straight back),
       - you can't switch to any other tab or Chrome window,
       - closing the tab just reopens it.
  This needs a small companion extension. Click "Install Chrome extension..."
  once in the app - it writes the extension files and walks you through it.

EMERGENCY EXITS (deliberately left open so you can never be trapped)
  - Click "End early" and type the phrase.
  - Ctrl+Alt+Del -> Task Manager. While Task Manager is in front, Full lock
    steps aside so you can end it.
  - If this program dies, Chrome unlocks itself ~10 seconds later and the
    keyboard hook disappears with the process.
"""

import atexit
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IS_WINDOWS = sys.platform == "win32"

# ---- Look & feel -----------------------------------------------------------
BG = "#0f172a"
PANEL = "#1e293b"
FG = "#e2e8f0"
MUTED = "#64748b"
ACCENT = "#38bdf8"
GOOD = "#4ade80"
DANGER = "#f87171"
FONT = "Segoe UI"

QUIT_PHRASE = "i am choosing to be distracted"
PRESETS = [15, 25, 45, 60]

# ---- Paths / settings ------------------------------------------------------
TAB_PORT = 9334
_APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
_LOCAL = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
SETTINGS_FILE = os.path.join(_APPDATA, "FocusLock", "settings.json")
EXT_DIR = os.path.join(_LOCAL, "FocusLock", "extension")

DEFAULTS = {"minutes": "25", "mode": "full", "locktab": True, "exact": True, "extra": ""}


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except OSError:
        pass


# ---- Windows that are never touched in Study mode --------------------------
ALWAYS_ALLOWED = {
    "taskmgr.exe",  # your escape hatch - never touch it
    "shellexperiencehost.exe", "startmenuexperiencehost.exe", "searchhost.exe",
    "searchapp.exe", "textinputhost.exe", "lockapp.exe", "dwm.exe", "sihost.exe",
    "ctfmon.exe", "consent.exe", "logonui.exe", "winlogon.exe", "csrss.exe",
    "fontdrvhost.exe", "securityhealthsystray.exe",
}
SKIP_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland",
    "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow",
}

# ---- Win32 constants -------------------------------------------------------
VK_TAB, VK_SHIFT, VK_CONTROL, VK_ESCAPE, VK_F4 = 0x09, 0x10, 0x11, 0x1B, 0x73
VK_LWIN, VK_RWIN = 0x5B, 0x5C
CHROME_BLOCKED_CTRL = {0x54, 0x4E, 0x4F, 0x4C, 0x57}  # Ctrl + T N O L W
LLKHF_ALTDOWN = 0x20
WH_KEYBOARD_LL = 13
WM_QUIT = 0x12
KEY_MESSAGES = (0x100, 0x101, 0x104, 0x105)  # key down/up, syskey down/up
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x80, 0x40000, 0x08000000
SW_MINIMIZE = 6
DWMWA_CLOAKED = 14

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")

    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    def _sig(fn, argtypes, restype):
        fn.argtypes = argtypes
        fn.restype = restype

    _sig(user32.SetWindowsHookExW,
         [ctypes.c_int, HOOKPROC, wintypes.HANDLE, wintypes.DWORD], wintypes.HANDLE)
    _sig(user32.CallNextHookEx,
         [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t)
    _sig(user32.UnhookWindowsHookEx, [wintypes.HANDLE], wintypes.BOOL)
    _sig(user32.GetMessageW,
         [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], ctypes.c_int)
    _sig(user32.TranslateMessage, [ctypes.POINTER(wintypes.MSG)], wintypes.BOOL)
    _sig(user32.DispatchMessageW, [ctypes.POINTER(wintypes.MSG)], ctypes.c_ssize_t)
    _sig(user32.PostThreadMessageW,
         [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL)
    _sig(user32.GetAsyncKeyState, [ctypes.c_int], ctypes.c_short)
    _sig(user32.GetForegroundWindow, [], wintypes.HWND)
    _sig(user32.GetWindowThreadProcessId,
         [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
    _sig(user32.EnumWindows, [WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL)
    _sig(user32.IsWindowVisible, [wintypes.HWND], wintypes.BOOL)
    _sig(user32.IsIconic, [wintypes.HWND], wintypes.BOOL)
    _sig(user32.GetWindowTextLengthW, [wintypes.HWND], ctypes.c_int)
    _sig(user32.GetWindowTextW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
    _sig(user32.GetClassNameW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
    _sig(user32.GetWindowLongW, [wintypes.HWND, ctypes.c_int], ctypes.c_long)
    _sig(user32.ShowWindowAsync, [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
    _sig(user32.EnumDisplayMonitors,
         [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM],
         wintypes.BOOL)
    _sig(kernel32.GetModuleHandleW, [wintypes.LPCWSTR], wintypes.HANDLE)
    _sig(kernel32.GetCurrentThreadId, [], wintypes.DWORD)
    _sig(kernel32.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
    _sig(kernel32.QueryFullProcessImageNameW,
         [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)],
         wintypes.BOOL)
    _sig(kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL)
    _sig(dwmapi.DwmGetWindowAttribute,
         [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD], ctypes.c_long)

    _exe_cache = {}

    def exe_name_for_pid(pid):
        """Lower-case exe name for a process id, or '' if it can't be read."""
        name = _exe_cache.get(pid)
        if name:
            return name
        h = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(h)
        if name:
            if len(_exe_cache) > 1000:
                _exe_cache.clear()
            _exe_cache[pid] = name
        return name or ""

    def foreground_exe():
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return exe_name_for_pid(pid.value) if pid.value else ""

    def get_monitors():
        """[(x, y, width, height), ...] for every connected monitor."""
        rects = []

        def cb(_hmon, _hdc, lprect, _lparam):
            r = lprect.contents
            rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return True

        proc = MONITORENUMPROC(cb)
        user32.EnumDisplayMonitors(None, None, proc, 0)
        return rects

    def list_app_windows(exclude_pids=(), include_minimized=False):
        """Normal, visible, titled top-level windows -> [(hwnd, pid, exe, title)]."""
        out = []

        def cb(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                if user32.IsIconic(hwnd) and not include_minimized:
                    return True
                ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
                if ex & WS_EX_NOACTIVATE:
                    return True
                if (ex & WS_EX_TOOLWINDOW) and not (ex & WS_EX_APPWINDOW):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                tbuf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, tbuf, n + 1)
                cbuf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cbuf, 256)
                if cbuf.value in SKIP_CLASSES:
                    return True
                cloaked = wintypes.DWORD(0)
                if (dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED,
                                                 ctypes.byref(cloaked), 4) == 0
                        and cloaked.value):
                    return True
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in exclude_pids:
                    return True
                out.append((hwnd, pid.value, exe_name_for_pid(pid.value), tbuf.value))
            except Exception:
                pass
            return True

        proc = WNDENUMPROC(cb)
        user32.EnumWindows(proc, 0)
        return out

    def minimize_window(hwnd):
        user32.ShowWindowAsync(hwnd, SW_MINIMIZE)

else:  # lets the logic be imported/tested off-Windows
    def exe_name_for_pid(pid): return ""
    def foreground_exe(): return ""
    def get_monitors(): return []
    def list_app_windows(exclude_pids=(), include_minimized=False): return []
    def minimize_window(hwnd): pass


# ============================================================================
#  Keyboard blocker (low-level hook on its own thread)
# ============================================================================
class KeyBlocker:
    def __init__(self):
        self.block_alt_tab = False
        self.lock_chrome = False
        self._thread = None
        self._tid = 0
        self._hook = None
        self._proc = None           # keep a reference so it isn't garbage collected

    def start(self):
        if self._thread or not IS_WINDOWS:
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self._thread.start()
        ready.wait(2)

    def stop(self):
        if self._thread is None:
            return
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        self._thread.join(2)
        self._thread = None
        self._tid = 0

    def _run(self, ready):
        self._tid = kernel32.GetCurrentThreadId()
        self._proc = HOOKPROC(self._callback)
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        ready.set()
        if not self._hook:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None

    def _callback(self, n_code, w_param, l_param):
        try:
            if n_code == 0 and w_param in KEY_MESSAGES:
                kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if self._should_block(kb.vkCode, kb.flags):
                    return 1  # swallow the key
        except Exception:
            pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _should_block(self, vk, flags):
        if vk in (VK_LWIN, VK_RWIN):
            return True
        alt = bool(flags & LLKHF_ALTDOWN)
        ctrl = bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
        shift = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
        if self.block_alt_tab:
            if alt and vk in (VK_TAB, VK_ESCAPE):
                return True
            if ctrl and not shift and vk == VK_ESCAPE:  # Ctrl+Shift+Esc stays open
                return True
        if self.lock_chrome:
            hit = (ctrl and vk in CHROME_BLOCKED_CTRL) or (alt and vk == VK_F4)
            if hit and foreground_exe() == "chrome.exe":
                return True
        return False


# ============================================================================
#  Local API that the Chrome extension polls
# ============================================================================
def find_chrome():
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(_LOCAL, "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return shutil.which("chrome")


def parse_exes(text):
    out = set()
    for tok in text.replace(";", ",").replace("\n", ",").split(","):
        tok = tok.strip().lower()
        if tok:
            out.add(tok if "." in tok else tok + ".exe")
    return out


class TabServer:
    """
    Tiny HTTP server on 127.0.0.1. The extension polls GET /state once a second
    and POSTs /status ("I locked the tab" / heartbeat / error).
    It keeps running after a session ends (active=False) so Chrome unlocks
    within a second instead of waiting for the "app vanished" timeout.
    """

    def __init__(self):
        self.active = False
        self.exact = True
        self.session = ""
        self.locked_url = ""
        self.status = {}
        self.status_at = 0.0
        self.port = TAB_PORT
        self._httpd = None
        self._thread = None

    def begin(self, exact):
        """Start a new locked session. Returns False if the port is unavailable."""
        self.session = "%d-%d" % (os.getpid(), int(time.time() * 1000))
        self.exact = bool(exact)
        self.locked_url = ""
        self.status = {}
        self.status_at = 0.0
        if self._httpd is None:
            try:
                self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler())
            except OSError:
                return False
            self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
            self._thread.start()
        self.active = True
        return True

    def end(self):
        self.active = False

    def snapshot(self):
        return {"active": self.active, "exact": self.exact,
                "session": self.session, "locked_url": self.locked_url}

    def set_status(self, data):
        if not isinstance(data, dict) or data.get("session") != self.session:
            return
        self.status = data
        self.status_at = time.time()
        if data.get("locked") and data.get("url") and not self.locked_url:
            self.locked_url = str(data["url"])

    def _handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def _origin_ok(self):
                origin = self.headers.get("Origin", "")
                return (not origin) or origin.startswith("chrome-extension://")

            def _send(self, code, obj=None):
                body = json.dumps(obj).encode("utf-8") if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                origin = self.headers.get("Origin", "")
                if origin.startswith("chrome-extension://"):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Access-Control-Allow-Headers", "Content-Type")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._send(204 if self._origin_ok() else 403)

            def do_GET(self):
                if not self._origin_ok():
                    return self._send(403, {})
                if self.path.split("?")[0] == "/state":
                    return self._send(200, srv.snapshot())
                self._send(404, {})

            def do_POST(self):
                if not self._origin_ok():
                    return self._send(403, {})
                if self.path.split("?")[0] != "/status":
                    return self._send(404, {})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    data = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, {})
                srv.set_status(data)
                self._send(200, {"ok": True})

        return Handler


# ============================================================================
#  The Chrome extension (written to disk by the "Install" button)
# ============================================================================
EXT_MANIFEST = r"""{
  "manifest_version": 3,
  "name": "Focus Lock (companion)",
  "version": "1.0",
  "description": "Locks Chrome to a single tab while the Focus Lock app is running a session.",
  "permissions": ["tabs", "webNavigation", "storage", "alarms"],
  "host_permissions": ["http://127.0.0.1/*"],
  "background": { "service_worker": "background.js" },
  "action": { "default_title": "Focus Lock" }
}
"""

EXT_JS = r"""// Focus Lock companion extension (Manifest V3).
// The Focus Lock app serves /state and /status on 127.0.0.1; this script polls it.
const API = "http://127.0.0.1:9334";
const POLL_MS = 1000;
const GIVE_UP_MS = 10000;   // app stopped answering -> unlock (never trap the user)
const BOUNCE_LIMIT = 8;     // 8 bounces in 10s on the same site = page rewrites its own URL

let state = null;           // null when not locked
let lastOk = Date.now();
let lastReport = 0;
let reopening = false;
let bounces = [];

const ready = load();

async function load() {
  try {
    const r = await chrome.storage.session.get("state");
    state = r.state || null;
  } catch (e) {
    state = null;
  }
  badge();
}

function save() {
  return chrome.storage.session.set({ state: state }).catch(function () {});
}

function badge() {
  try {
    chrome.action.setBadgeText({ text: state ? "ON" : "" });
    chrome.action.setBadgeBackgroundColor({ color: "#38bdf8" });
  } catch (e) {}
}

// ---- URL rules -------------------------------------------------------------
function parse(u) {
  try { return new URL(u); } catch (e) { return null; }
}
function bare(h) {
  return h.indexOf("www.") === 0 ? h.slice(4) : h;
}
function key(u) {           // identity of a page: ignores #fragment, www., http/https
  const proto = u.protocol === "file:" ? "file:" : "web:";
  return proto + "//" + bare(u.hostname) + u.pathname.replace(/\/+$/, "") + u.search;
}
function sameSite(a, b) {
  if (a.protocol === "file:" || b.protocol === "file:") return key(a) === key(b);
  if (!/^https?:$/.test(a.protocol)) return false;
  const ha = bare(a.hostname), hb = bare(b.hostname);
  return ha === hb || ha.endsWith("." + hb);
}
function allowed(url) {
  const u = parse(url), b = parse(state.base);
  if (!u || !b) return false;
  if (!sameSite(u, b)) return false;
  if (state.exact) return key(u) === key(b);
  return true;
}

// ---- talking to the app ----------------------------------------------------
async function report(session, locked, error) {
  let incognito = false;
  try { incognito = await chrome.extension.isAllowedIncognitoAccess(); } catch (e) {}
  lastReport = Date.now();
  try {
    await fetch(API + "/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session: session, locked: locked, error: error || "",
        url: state ? state.good : "", incognito: incognito
      })
    });
  } catch (e) {}
}

async function poll() {
  await ready;
  let s;
  try {
    const r = await fetch(API + "/state", { cache: "no-store" });
    s = await r.json();
    lastOk = Date.now();
  } catch (e) {
    if (state && Date.now() - lastOk > GIVE_UP_MS) await unlock();
    return;
  }
  if (s.active) {
    if (!state || state.session !== s.session) {
      await lock(s);
    } else {
      await verify();
      if (state && Date.now() - lastReport > 2000) await report(state.session, true, "");
    }
  } else if (state) {
    await unlock();
  }
}

// ---- lock / unlock ---------------------------------------------------------
async function lock(s) {
  const recovering = !!s.locked_url;   // Chrome was restarted mid-session
  let tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true, windowType: "normal" });
  if (!tabs.length) tabs = await chrome.tabs.query({ active: true, windowType: "normal" });
  const tab = tabs[0];
  if (!tab) {
    return report(s.session, false, "No Chrome window found. Open the page you want to study in Chrome first.");
  }
  let base = tab.url || "";
  if (recovering) {
    const cur = parse(base), want = parse(s.locked_url);
    if (!cur || !want || key(cur) !== key(want)) {
      base = s.locked_url;
      try { await chrome.tabs.update(tab.id, { url: base }); } catch (e) {}
    }
  } else if (!/^(https?|file):/.test(base)) {
    return report(s.session, false,
      "The current Chrome tab is blank or a browser page. Open the page you want to study first, then press Start.");
  }
  const all = await chrome.tabs.query({});
  state = {
    session: s.session, tabId: tab.id, windowId: tab.windowId,
    base: base, good: base, exact: !!s.exact,
    known: all.map(function (t) { return t.id; })
  };
  await save();
  badge();
  try { await chrome.windows.update(tab.windowId, { focused: true }); } catch (e) {}
  await report(s.session, true, "");
}

async function unlock() {
  state = null;
  await save();
  badge();
}

// ---- enforcement -----------------------------------------------------------
async function bounce(url, preCommit) {
  if (!state) return;
  const now = Date.now();
  bounces = bounces.filter(function (b) { return now - b.t < 10000; });
  const kind = !!preCommit;
  for (let i = bounces.length - 1; i >= 0; i--) {      // de-dupe repeats of the same kind only
    if (bounces[i].pre === kind && now - bounces[i].t < 300) return;
  }
  bounces.push({ t: now, pre: kind });
  if (state.exact && bounces.length >= BOUNCE_LIMIT) {
    const u = parse(url), b = parse(state.base);
    if (u && b && sameSite(u, b)) {      // page keeps rewriting its URL: relax to same-site
      state.exact = false;
      await save();
      return;
    }
  }
  try {
    if (preCommit) await chrome.tabs.reload(state.tabId);   // cancels the pending navigation
    else await chrome.tabs.update(state.tabId, { url: state.good });
  } catch (e) {}
}

function check(url, committed) {
  if (!state) return;
  if (allowed(url)) {
    if (committed && state.good !== url) { state.good = url; save(); }
  } else {
    bounce(url, !committed);
  }
}

async function reopen() {
  if (!state || reopening) return;
  reopening = true;
  try {
    const t = await chrome.tabs.create({ url: state.good, active: true });
    state.tabId = t.id;
    state.windowId = t.windowId;
    await save();
  } catch (e) {
  } finally {
    reopening = false;
  }
}

async function verify() {
  if (!state || reopening) return;
  let tab;
  try { tab = await chrome.tabs.get(state.tabId); } catch (e) { return reopen(); }
  const all = await chrome.tabs.query({});
  for (const t of all) {
    if (t.id !== state.tabId && state.known.indexOf(t.id) === -1) {
      chrome.tabs.remove(t.id).catch(function () {});
    }
  }
  if (tab.url && !allowed(tab.url)) {
    bounce(tab.url, false);
  } else if (!tab.active) {
    try { await chrome.tabs.update(state.tabId, { active: true }); } catch (e) {}
  }
}

chrome.tabs.onCreated.addListener(async function (tab) {
  await ready;
  if (!state || reopening) return;     // the tab we create ourselves is adopted in reopen()
  if (tab.id !== state.tabId) chrome.tabs.remove(tab.id).catch(function () {});
});

chrome.tabs.onRemoved.addListener(async function (tabId) {
  await ready;
  if (state && tabId === state.tabId) await reopen();
});

chrome.tabs.onActivated.addListener(async function (info) {
  await ready;
  if (!state || reopening || info.tabId === state.tabId) return;
  try {
    await chrome.tabs.update(state.tabId, { active: true });
    if (info.windowId !== state.windowId) {
      await chrome.windows.update(state.windowId, { focused: true });
    }
  } catch (e) {}
});

chrome.windows.onFocusChanged.addListener(async function (windowId) {
  await ready;
  if (!state || reopening) return;
  if (windowId === chrome.windows.WINDOW_ID_NONE || windowId === state.windowId) return;
  try { await chrome.windows.update(state.windowId, { focused: true }); } catch (e) {}
});

chrome.tabs.onAttached.addListener(async function (tabId, info) {
  await ready;
  if (state && tabId === state.tabId) { state.windowId = info.newWindowId; await save(); }
});

chrome.tabs.onReplaced.addListener(async function (addedId, removedId) {
  await ready;
  if (state && removedId === state.tabId) { state.tabId = addedId; await save(); }
});

chrome.webNavigation.onBeforeNavigate.addListener(async function (d) {
  await ready;
  if (state && d.frameId === 0 && d.tabId === state.tabId) check(d.url, false);
});

chrome.tabs.onUpdated.addListener(async function (tabId, change) {
  await ready;
  if (state && tabId === state.tabId && change.url) check(change.url, true);
});

// ---- keep the service worker alive and polling -----------------------------
let looping = false;
async function loop() {
  if (looping) return;
  looping = true;
  for (;;) {
    try { await poll(); } catch (e) {}
    await new Promise(function (r) { setTimeout(r, POLL_MS); });
    try { chrome.runtime.getPlatformInfo(function () {}); } catch (e) {}  // API call resets idle timer
  }
}
chrome.alarms.create("focuslock-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(function () { loop(); });
chrome.runtime.onStartup.addListener(loop);
chrome.runtime.onInstalled.addListener(loop);
chrome.action.onClicked.addListener(function () { loop(); poll(); });
loop();
"""


def write_extension():
    os.makedirs(EXT_DIR, exist_ok=True)
    with open(os.path.join(EXT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(EXT_MANIFEST)
    with open(os.path.join(EXT_DIR, "background.js"), "w", encoding="utf-8") as f:
        f.write(EXT_JS)
    return EXT_DIR


# ============================================================================
#  UI helpers
# ============================================================================
def lbl(parent, text, size=11, color=FG, bold=False, **kw):
    return tk.Label(parent, text=text, bg=BG, fg=color,
                    font=(FONT, size, "bold" if bold else "normal"), **kw)


def btn(parent, text, command, color=ACCENT, size=11, fg="#0f172a", **kw):
    return tk.Button(parent, text=text, command=command, bg=color, fg=fg,
                     activebackground=color, activeforeground=fg, relief="flat",
                     bd=0, padx=14, pady=7, font=(FONT, size, "bold"),
                     cursor="hand2", **kw)


def entry(parent, var, width=20, **kw):
    return tk.Entry(parent, textvariable=var, width=width, font=(FONT, 11),
                    bg=PANEL, fg=FG, insertbackground=FG, relief="flat", **kw)


# ============================================================================
#  Main app
# ============================================================================
class FocusLock(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Focus Lock")
        self.configure(bg=BG)
        self.settings = load_settings()

        self.locked = False
        self.full_lock = True
        self.tab_locked = False
        self.total_seconds = 0
        self.end_time = 0.0
        self.quit_entry = None
        self.covers = []
        self.allowed = set()
        self.app_choices = []
        self.blocker = KeyBlocker()
        self.tabsrv = TabServer()
        self._last_relaunch = 0.0

        self.container = tk.Frame(self, bg=BG)
        self.container.pack(expand=True, fill="both")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.show_setup()

    # ---- housekeeping ------------------------------------------------------
    def clear(self):
        for w in self.container.winfo_children():
            w.destroy()

    def on_close(self):
        if self.locked:
            return  # ignore the X button / Alt+F4 while locked
        self.shutdown()
        self.destroy()

    def shutdown(self):
        try:
            self.blocker.stop()
            self.tabsrv.end()
        except Exception:
            pass

    def primary_monitor(self):
        mons = get_monitors()
        for m in mons:
            if m[0] == 0 and m[1] == 0:
                return m
        if mons:
            return mons[0]
        return (0, 0, self.winfo_screenwidth(), self.winfo_screenheight())

    def center_on_primary(self):
        self.update_idletasks()
        px, py, pw, ph = self.primary_monitor()
        x = px + (pw - self.winfo_reqwidth()) // 2
        y = py + max(0, (ph - self.winfo_reqheight()) // 3)
        self.geometry("+%d+%d" % (x, y))

    # ---- setup screen ------------------------------------------------------
    def show_setup(self):
        self.clear()
        self.container.configure(padx=0, pady=0)
        self.resizable(False, False)
        self.attributes("-toolwindow", False)
        self.attributes("-topmost", False)
        self.geometry("")
        s = self.settings
        c = self.container

        tk.Frame(c, width=560, height=1, bg=BG).pack()  # minimum width spacer
        lbl(c, "Focus Lock", 24, bold=True).pack(pady=(14, 0))
        lbl(c, "Pick a time. Once it starts, the only way out is the timer.",
            10, MUTED).pack(pady=(0, 10))

        # Duration
        lbl(c, "How long? (minutes)", 10, MUTED).pack(anchor="w", padx=30)
        row = tk.Frame(c, bg=BG)
        row.pack(anchor="w", padx=30, pady=(2, 8))
        self.minutes_var = tk.StringVar(value=str(s["minutes"]))
        for m in PRESETS:
            btn(row, str(m), lambda mm=m: self.minutes_var.set(str(mm)),
                color=PANEL, fg=FG, size=10).pack(side="left", padx=(0, 6))
        entry(row, self.minutes_var, width=6, justify="center").pack(
            side="left", ipady=6, padx=(8, 0))

        # Mode
        lbl(c, "Mode", 10, MUTED).pack(anchor="w", padx=30)
        self.mode_var = tk.StringVar(value=s["mode"])
        for value, text in (
                ("full", "Full lock - black screen + timer on every monitor"),
                ("study", "Study mode - only the apps / Chrome tab I allow")):
            tk.Radiobutton(c, text=text, variable=self.mode_var, value=value,
                           command=self.on_mode_change, bg=BG, fg=FG,
                           selectcolor=PANEL, activebackground=BG,
                           activeforeground=FG, font=(FONT, 10),
                           anchor="w").pack(anchor="w", padx=26)

        # Study-mode options (shown only in study mode)
        sf = tk.Frame(c, bg=BG)
        self.study_frame = sf
        lbl(sf, "Chrome", 10, MUTED).pack(anchor="w")
        self.locktab_var = tk.BooleanVar(value=bool(s["locktab"]))
        self.exact_var = tk.BooleanVar(value=bool(s["exact"]))

        def check(text, var):
            return tk.Checkbutton(sf, text=text, variable=var, bg=BG, fg=FG,
                                  selectcolor=PANEL, activebackground=BG,
                                  activeforeground=FG, font=(FONT, 10), anchor="w")

        check("Lock my current Chrome tab (open your page in Chrome first)",
              self.locktab_var).pack(anchor="w")
        check("Also lock its address (untick to allow links within the same site)",
              self.exact_var).pack(anchor="w")
        btn(sf, "Install Chrome extension...", self.install_extension, color=PANEL,
            fg=FG, size=9).pack(anchor="w", pady=(4, 0))

        lbl(sf, "Allowed apps (click to toggle)", 10, MUTED).pack(anchor="w", pady=(10, 0))
        lf = tk.Frame(sf, bg=BG)
        lf.pack(fill="x", pady=(2, 4))
        self.app_list = tk.Listbox(
            lf, height=5, selectmode="multiple", exportselection=False,
            bg=PANEL, fg=FG, selectbackground=ACCENT, selectforeground="#0f172a",
            relief="flat", highlightthickness=0, font=(FONT, 10))
        sb = tk.Scrollbar(lf, command=self.app_list.yview)
        self.app_list.config(yscrollcommand=sb.set)
        self.app_list.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        r3 = tk.Frame(sf, bg=BG)
        r3.pack(fill="x")
        lbl(r3, "Other apps (exe names, comma separated):", 9, MUTED).pack(side="left")
        btn(r3, "Refresh list", self.refresh_apps, color=PANEL, fg=FG,
            size=9).pack(side="right")
        self.extra_var = tk.StringVar(value=s["extra"])
        entry(sf, self.extra_var, width=50).pack(fill="x", ipady=5, pady=(2, 0))

        self.start_btn = btn(c, "Start", self.start, size=13)
        self.start_btn.pack(pady=(14, 4), ipadx=30)
        self.error_lbl = lbl(c, "", 10, DANGER, wraplength=500)
        self.error_lbl.pack(pady=(0, 12))

        self.on_mode_change(resize=False)
        self.refresh_apps()
        self.center_on_primary()

    def on_mode_change(self, resize=True):
        if self.mode_var.get() == "study":
            self.study_frame.pack(fill="x", padx=30, pady=(8, 0), before=self.start_btn)
        else:
            self.study_frame.pack_forget()
        if resize:
            self.geometry("")

    def set_error(self, text, color=DANGER):
        self.error_lbl.config(text=text, fg=color)

    def selected_exes(self):
        return [self.app_choices[i][0] for i in self.app_list.curselection()]

    def refresh_apps(self):
        keep = set(self.selected_exes())
        seen = {}
        for _h, _pid, exe, title in list_app_windows(
                exclude_pids={os.getpid()}, include_minimized=True):
            if exe and exe not in ALWAYS_ALLOWED:
                seen.setdefault(exe, title)
        self.app_choices = sorted(seen.items())
        self.app_list.delete(0, "end")
        for i, (exe, title) in enumerate(self.app_choices):
            self.app_list.insert("end", "%s   -   %s" % (exe, title[:44]))
            if exe in keep:
                self.app_list.selection_set(i)

    def install_extension(self):
        from tkinter import messagebox
        folder = write_extension()
        try:
            os.startfile(folder)                       # opens the folder in Explorer
        except Exception:
            pass
        chrome = find_chrome()
        if chrome:
            try:
                subprocess.Popen([chrome, "chrome://extensions"])
            except OSError:
                pass
        messagebox.showinfo(
            "Install the Focus Lock extension",
            "Do this once (about 30 seconds):\n\n"
            "1. In the Chrome page that just opened, switch ON 'Developer mode' "
            "(top-right).\n"
            "2. Click 'Load unpacked' and choose the folder that just opened in "
            "Explorer:\n    " + folder + "\n"
            "3. Click 'Details' on Focus Lock and switch ON 'Allow in Incognito' "
            "so private windows can't be a loophole.\n"
            "4. (Optional) Pin it from the puzzle-piece menu - it shows 'ON' "
            "while a session is running.\n\n"
            "If Chrome ever asks about 'local network access' for Focus Lock, "
            "click Allow.")

    # ---- starting a session ------------------------------------------------
    def wait_for_tab_lock(self, timeout=8.0):
        """Wait for the extension to confirm. Returns '' on success, else a message."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            st = self.tabsrv.status
            if st.get("locked"):
                return ""
            if st.get("error"):
                return st["error"]
            self.update()
            time.sleep(0.1)
        return ("Chrome didn't respond. Make sure Chrome is open and the Focus Lock "
                "extension is installed (use 'Install Chrome extension...'). "
                "Clicking the Focus Lock icon in Chrome and pressing Start again can also help.")

    def start(self):
        try:
            minutes = float(self.minutes_var.get())
            if not 0.1 <= minutes <= 600:
                raise ValueError
        except ValueError:
            self.set_error("Enter a number of minutes between 1 and 600.")
            return

        mode = self.mode_var.get()
        allowed, lock_tab = set(), False
        if mode == "study":
            allowed = set(self.selected_exes()) | parse_exes(self.extra_var.get())
            lock_tab = bool(self.locktab_var.get())
            if not allowed and not lock_tab:
                self.set_error("Study mode needs at least one allowed app, or a locked Chrome tab.")
                return

        save_settings({"minutes": self.minutes_var.get(), "mode": mode,
                       "locktab": bool(self.locktab_var.get()),
                       "exact": bool(self.exact_var.get()),
                       "extra": ", ".join(sorted(allowed))})

        if lock_tab:
            self.set_error("Locking your Chrome tab...", MUTED)
            self.start_btn.config(state="disabled")
            self.update()
            if not self.tabsrv.begin(self.exact_var.get()):
                self.start_btn.config(state="normal")
                self.set_error("Couldn't open local port %d. Is another copy of "
                               "Focus Lock already running?" % TAB_PORT)
                return
            err = self.wait_for_tab_lock()
            if err:
                self.tabsrv.end()
                self.start_btn.config(state="normal")
                self.set_error(err)
                return
            allowed.add("chrome.exe")

        self.allowed = allowed
        self.tab_locked = lock_tab
        self.full_lock = (mode == "full")
        self.total_seconds = int(minutes * 60)
        self.end_time = time.monotonic() + self.total_seconds
        self.locked = True
        self.clear()

        if self.full_lock:
            self.enter_full_lock()
        else:
            self.enter_study_bar()

        self.blocker.block_alt_tab = self.full_lock
        self.blocker.lock_chrome = lock_tab
        self.blocker.start()

        self.tick()
        if self.full_lock:
            self.keep_focus()
        else:
            self.guard_tick()

    # ---- lock screens ------------------------------------------------------
    def enter_full_lock(self):
        px, py, pw, ph = self.primary_monitor()
        self.resizable(True, True)
        self.geometry("+%d+%d" % (px, py))
        self.update_idletasks()
        self.attributes("-fullscreen", True)
        self.attributes("-topmost", True)
        self.bind("<Alt-F4>", lambda e: "break")
        self.bind("<Escape>", lambda e: "break")

        c = self.container
        lbl(c, "FOCUS", 20, MUTED, bold=True).pack(pady=(60, 0))
        self.time_lbl = tk.Label(c, text="00:00", bg=BG, fg=FG, font=(FONT, 120, "bold"))
        self.time_lbl.pack(expand=True)
        self.bar = tk.Canvas(c, height=10, bg=PANEL, highlightthickness=0)
        self.bar.pack(fill="x", padx=160, pady=30)
        lbl(c, "Stay on this screen. You've got this.", 14, MUTED).pack()
        self.exit_frame = tk.Frame(c, bg=BG)
        self.exit_frame.pack(side="bottom", pady=30)
        self.exit_btn = tk.Button(
            self.exit_frame, text="End early", bg=BG, fg=MUTED, activebackground=BG,
            activeforeground=DANGER, relief="flat", bd=0, font=(FONT, 10),
            cursor="hand2", command=self.show_quit_prompt)
        self.exit_btn.pack()

        # black covers on every other monitor
        for (x, y, w, h) in get_monitors():
            if (x, y) == (px, py):
                continue
            top = tk.Toplevel(self)
            top.configure(bg="black")
            top.overrideredirect(True)
            top.geometry("%dx%d+%d+%d" % (w, h, x, y))
            top.attributes("-topmost", True)
            tk.Label(top, text="FOCUS", bg="black", fg=PANEL,
                     font=(FONT, 28, "bold")).place(relx=0.5, rely=0.5, anchor="center")
            top.bind("<Alt-F4>", lambda e: "break")
            self.covers.append(top)
        self.focus_force()

    def enter_study_bar(self):
        px, py, pw, ph = self.primary_monitor()
        self.resizable(False, False)
        self.attributes("-toolwindow", True)   # small title bar, no minimise button
        self.attributes("-topmost", True)
        c = self.container
        c.configure(padx=14, pady=6)
        self.time_lbl = tk.Label(c, text="00:00", bg=BG, fg=FG, font=(FONT, 26, "bold"))
        self.time_lbl.pack()
        self.bar = tk.Canvas(c, height=6, width=200, bg=PANEL, highlightthickness=0)
        self.bar.pack(fill="x", pady=(2, 4))
        self.exit_frame = tk.Frame(c, bg=BG)
        self.exit_frame.pack()
        self.exit_btn = tk.Button(
            self.exit_frame, text="End early", bg=BG, fg=MUTED, activebackground=BG,
            activeforeground=DANGER, relief="flat", bd=0, font=(FONT, 9),
            cursor="hand2", command=self.show_quit_prompt)
        self.exit_btn.pack()
        self.update_idletasks()
        x = px + (pw - self.winfo_reqwidth()) // 2
        self.geometry("+%d+%d" % (x, py + 6))

    def show_quit_prompt(self):
        self.exit_btn.pack_forget()
        wrap = 700 if self.full_lock else 250
        tk.Label(self.exit_frame, text='To quit, type:\n"%s"\nthen press Enter' % QUIT_PHRASE,
                 bg=BG, fg=DANGER, font=(FONT, 11 if self.full_lock else 9),
                 wraplength=wrap).pack(pady=(0, 6))
        self.quit_entry = tk.Entry(
            self.exit_frame, width=36 if self.full_lock else 30, justify="center",
            font=(FONT, 13 if self.full_lock else 10), bg=PANEL, fg=FG,
            insertbackground=FG, relief="flat")
        self.quit_entry.pack(ipady=6)
        self.quit_entry.bind("<Return>", self.check_quit_phrase)
        self.quit_entry.focus_force()

    def check_quit_phrase(self, _event=None):
        if self.quit_entry and self.quit_entry.get().strip().lower() == QUIT_PHRASE:
            self.unlock()
            self.show_finished(completed=False)

    # ---- loops -------------------------------------------------------------
    def tick(self):
        if not self.locked:
            return
        remaining = self.end_time - time.monotonic()
        if remaining <= 0:
            self.unlock()
            self.beep()
            self.show_finished(completed=True)
            return
        secs = int(remaining + 0.999)
        self.time_lbl.config(text="%02d:%02d" % (secs // 60, secs % 60))
        w = self.bar.winfo_width()
        h = int(self.bar.cget("height"))
        done = 1 - remaining / self.total_seconds
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, w * done, h, fill=ACCENT, width=0)
        self.after(200, self.tick)

    def keep_focus(self):
        """Full lock: stay on top of everything, except Task Manager."""
        if not self.locked:
            return
        windows = [self] + self.covers
        if foreground_exe() == "taskmgr.exe":
            for w in windows:                      # step aside so you can end us
                w.attributes("-topmost", False)
        else:
            for w in windows:
                w.attributes("-topmost", True)
                w.lift()
            if self.focus_displayof() is None:
                self.focus_force()
                if self.quit_entry is not None and self.quit_entry.winfo_exists():
                    self.quit_entry.focus_force()
        self.after(250, self.keep_focus)

    def guard_tick(self):
        """Study mode: minimise anything that isn't allowed."""
        if not self.locked:
            return
        chrome_visible = False
        for hwnd, _pid, exe, _title in list_app_windows(exclude_pids={os.getpid()}):
            if exe == "chrome.exe":
                chrome_visible = True
            if not exe or exe in self.allowed or exe in ALWAYS_ALLOWED:
                continue
            minimize_window(hwnd)
        self.revive_chrome(chrome_visible)
        self.attributes("-topmost", True)
        self.lift()
        self.after(300, self.guard_tick)

    def revive_chrome(self, chrome_visible):
        """If Chrome was closed entirely (so the extension can't react), reopen the page."""
        if not (self.tab_locked and self.tabsrv.locked_url) or chrome_visible:
            return
        now = time.monotonic()
        if time.time() - self.tabsrv.status_at < 8 or now - self._last_relaunch < 10:
            return
        self._last_relaunch = now
        path = find_chrome()
        if path:
            try:
                subprocess.Popen([path, self.tabsrv.locked_url])
            except OSError:
                pass

    # ---- ending ------------------------------------------------------------
    def unlock(self):
        self.locked = False
        self.quit_entry = None
        self.blocker.stop()
        self.blocker.lock_chrome = False
        self.tabsrv.end()                 # extension unlocks Chrome within ~1 second
        self.tab_locked = False
        for cv in self.covers:
            cv.destroy()
        self.covers = []
        self.unbind("<Alt-F4>")
        self.unbind("<Escape>")
        self.attributes("-topmost", False)
        self.attributes("-fullscreen", False)
        self.attributes("-toolwindow", False)
        self.container.configure(padx=0, pady=0)

    def beep(self):
        try:
            import winsound
            for freq in (660, 880, 1100):
                winsound.Beep(freq, 180)
        except Exception:
            self.bell()

    def show_finished(self, completed):
        self.clear()
        self.resizable(False, False)
        self.geometry("")
        c = self.container
        tk.Frame(c, width=460, height=1, bg=BG).pack()
        if completed:
            lbl(c, "Session complete", 24, GOOD, bold=True).pack(pady=(60, 8))
            lbl(c, "Nice work. Take a short break.", 12, MUTED).pack(pady=(0, 30))
        else:
            lbl(c, "Session ended early", 22, DANGER, bold=True).pack(pady=(60, 8))
            lbl(c, "No worries. Try again when you're ready.", 12, MUTED).pack(pady=(0, 30))
        btn(c, "Another session", self.show_setup, size=12).pack(pady=6)
        btn(c, "Close", self.on_close, color="#475569", fg=FG, size=12).pack(pady=(6, 50))
        self.center_on_primary()


if __name__ == "__main__":
    if not IS_WINDOWS:
        print("Focus Lock only runs on Windows.")
        sys.exit(1)
    try:  # keep Tk and Win32 coordinates in the same pixel space
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    app = FocusLock()
    atexit.register(app.shutdown)
    app.mainloop()
