"""
Focus Lock v4 - a Windows study timer that is hard to escape.

Run:   python focus_lock.py          (Windows, Python 3.8+, no extra installs)

TWO MODES
  Full lock   Black screen + clock-style timer on EVERY monitor. Nothing else
              usable. Blocks Win key, Alt+Tab, Alt+Esc, Ctrl+Esc.
  Study mode  Only the apps you allow stay usable; everything else is
              minimised the moment it appears. Blocks the Win key (Alt+Tab
              still works so you can hop between allowed apps).

BREAKS
  Before starting, choose 0-3 breaks and how long each one is. During a
  session, "Take a break" freezes the timer and unlocks everything (Chrome
  included). When the break runs out, the lock comes back on by itself and
  Chrome jumps back to your study tab.

SPOTIFY
  With "Spotify controls" on, a card next to the timer shows what the Spotify
  desktop app is playing, with back / play-pause / skip buttons. It reads the
  song from Spotify's window and sends it commands directly, so there is no
  login. It needs the desktop app (not the web player) and doesn't see songs
  playing on another device. "Back" goes to the previous song, not just the
  start of the current one.

CHROME TAB LOCK (Study mode)
  1. Open the page you want to study in your normal Chrome.
  2. Press Start. Focus Lock locks THAT tab:
       - no new tabs or windows (they are closed instantly),
       - the tab's address can't be changed (you get bounced straight back),
       - you can't switch to any other tab or Chrome window,
       - closing the tab (or Chrome's last window) just reopens it.
  This needs a small companion extension. Click "Install extension" once in
  the app - it writes the extension files and walks you through it. The
  extension must be allowed in Incognito, otherwise a private window would be
  a way around the lock. The start screen shows whether it is connected.

EMERGENCY EXITS (deliberately left open so you can never be trapped)
  - Click "End early" and type the phrase.
  - Ctrl+Alt+Del -> Task Manager. While Task Manager is in front, Full lock
    steps aside so you can end it.
  - If this program dies, Chrome unlocks itself ~10 seconds later and the
    keyboard hook disappears with the process.
"""

import atexit
import datetime
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IS_WINDOWS = sys.platform == "win32"
APP_TITLE = "Focus Lock"

# ---- Look & feel -----------------------------------------------------------
BG = "#0b1120"
SURFACE = "#111a2d"
PANEL = "#1e293b"
PANEL_HI = "#283548"
BORDER = "#24324a"
FG = "#e2e8f0"
MUTED = "#94a3b8"
SUBTLE = "#64748b"
ACCENT = "#38bdf8"
ACCENT_HI = "#7dd3fc"
GOOD = "#4ade80"
WARN = "#fbbf24"
WARN_HI = "#fcd34d"
DANGER = "#f87171"
INK = "#0b1120"          # text on bright fills
FONT = "Segoe UI"

QUIT_PHRASE = "i am choosing to be distracted"
PRESETS = [15, 25, 45, 60]
BREAK_COUNTS = [0, 1, 2, 3]
BREAK_MINUTES = [2, 3, 5, 10, 15, 20, 30]
STREAK_MIN_SECONDS = 5 * 60      # a day counts towards the streak after 5 focused minutes

_SCALE = 1.0                     # set from the screen DPI when the app starts


def px(n):
    """Scale a design-pixel value to the current DPI."""
    return int(round(n * _SCALE))


# ---- Paths / settings ------------------------------------------------------
TAB_PORT = 9334
_APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
_LOCAL = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
SETTINGS_FILE = os.path.join(_APPDATA, "FocusLock", "settings.json")
STATS_FILE = os.path.join(_APPDATA, "FocusLock", "stats.json")
EXT_DIR = os.path.join(_LOCAL, "FocusLock", "extension")
SOUND_DIR = os.path.join(_LOCAL, "FocusLock", "sounds")

DEFAULTS = {"minutes": "25", "mode": "full", "locktab": True, "exact": True,
            "apps": [], "extra": "", "breaks": 1, "break_minutes": 5, "spotify": True}


def parse_exes(text):
    out = set()
    for tok in text.replace(";", ",").replace("\n", ",").split(","):
        tok = tok.strip().lower()
        if tok:
            out.add(tok if "." in tok else tok + ".exe")
    return out


def _nearest(value, choices):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return choices[0]
    return min(choices, key=lambda c: abs(c - value))


def load_settings():
    s = dict(DEFAULTS)
    raw = {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        pass
    if isinstance(raw, dict):
        s.update(raw)
        if raw and "apps" not in raw:
            # v3 saved the ticked apps into the "extra" text box, so un-ticking
            # them never took effect. Move them back into the list.
            s["apps"] = sorted(parse_exes(str(raw.get("extra", ""))))
            s["extra"] = ""
    if not isinstance(s["apps"], list):
        s["apps"] = []
    s["apps"] = [str(a).lower() for a in s["apps"] if a]
    s["breaks"] = _nearest(s.get("breaks"), BREAK_COUNTS)
    s["break_minutes"] = _nearest(s.get("break_minutes"), BREAK_MINUTES)
    s["spotify"] = bool(s.get("spotify", True))
    return s


def save_settings(s):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except OSError:
        pass


# ---- Focus history (today / week / streak) ---------------------------------
class Stats:
    def __init__(self):
        self.days = {}
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.get("days", {}).items():
                self.days[str(k)] = float(v)
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def add(self, seconds):
        key = datetime.date.today().isoformat()
        self.days[key] = self.days.get(key, 0.0) + max(0.0, seconds)
        try:
            os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
            tmp = STATS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"days": self.days}, f)
            os.replace(tmp, STATS_FILE)
        except OSError:
            pass

    def on(self, day):
        return self.days.get(day.isoformat(), 0.0)

    def today(self):
        return self.on(datetime.date.today())

    def week(self):
        d = datetime.date.today()
        return sum(self.on(d - datetime.timedelta(days=i)) for i in range(7))

    def last7(self):
        """Booleans for the last 7 days (oldest first): did the day count?"""
        d = datetime.date.today()
        return [self.on(d - datetime.timedelta(days=i)) >= STREAK_MIN_SECONDS
                for i in range(6, -1, -1)]

    def streak(self):
        d = datetime.date.today()
        if self.on(d) < STREAK_MIN_SECONDS:      # today not done yet - keep yesterday's streak
            d -= datetime.timedelta(days=1)
        n = 0
        while self.on(d) >= STREAK_MIN_SECONDS:
            n += 1
            d -= datetime.timedelta(days=1)
        return n

    def best_streak(self):
        good = sorted(datetime.date.fromisoformat(k) for k, v in self.days.items()
                      if v >= STREAK_MIN_SECONDS)
        best = run = 0
        prev = None
        for d in good:
            run = run + 1 if prev and (d - prev).days == 1 else 1
            best = max(best, run)
            prev = d
        return best


def fmt_duration(secs):
    m = int(secs // 60)
    if m < 60:
        return "%dm" % m
    return "%dh %02dm" % (m // 60, m % 60)


def fmt_clock(secs):
    secs = int(math.ceil(max(0.0, secs) - 1e-6))
    h, rest = divmod(secs, 3600)
    if h:
        return "%d:%02d:%02d" % (h, rest // 60, rest % 60)
    return "%02d:%02d" % (rest // 60, rest % 60)


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


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
SW_SHOW, SW_MINIMIZE, SW_RESTORE = 5, 6, 9
DWMWA_CLOAKED = 14
ERROR_ALREADY_EXISTS = 183
WM_APPCOMMAND = 0x319
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
# action -> (APPCOMMAND_* sent to Spotify's window, media key used as a fallback)
SPOTIFY_ACTIONS = {"next": (11, 0xB0), "prev": (12, 0xB1), "toggle": (14, 0xB3)}

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
    _sig(user32.ShowWindow, [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
    _sig(user32.PostMessageW,
         [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL)
    _sig(user32.keybd_event, [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t], None)
    _sig(user32.SetForegroundWindow, [wintypes.HWND], wintypes.BOOL)
    _sig(user32.FindWindowW, [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND)
    _sig(user32.MessageBoxW,
         [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT], ctypes.c_int)
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
    _sig(kernel32.CreateMutexW, [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR],
         wintypes.HANDLE)
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

    def find_spotify_window():
        """(hwnd, title) of the Spotify desktop app's main window, or (None, '').
        Hidden windows count too, so it still works when Spotify sits in the tray."""
        found = []

        def cb(hwnd, _lparam):
            try:
                cbuf = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, cbuf, 64)
                if not cbuf.value.startswith("Chrome_WidgetWin"):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if exe_name_for_pid(pid.value) != "spotify.exe":
                    return True
                tbuf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, tbuf, n + 1)
                found.append((not user32.IsWindowVisible(hwnd), hwnd, tbuf.value))
            except Exception:
                pass
            return True

        proc = WNDENUMPROC(cb)
        user32.EnumWindows(proc, 0)
        if not found:
            return None, ""
        found.sort(key=lambda f: f[0])          # visible windows first
        return found[0][1], found[0][2]

    def spotify_send(hwnd, action, use_keys=False):
        command, vk = SPOTIFY_ACTIONS[action]
        if use_keys:
            user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
            user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        else:
            user32.PostMessageW(hwnd, WM_APPCOMMAND, 0, command << 16)

    def launch_spotify():
        for target in ("spotify:", os.path.join(_APPDATA, "Spotify", "Spotify.exe")):
            try:
                os.startfile(target)
                return True
            except OSError:
                continue
        return False

else:  # lets the logic be imported/tested off-Windows
    def exe_name_for_pid(pid): return ""
    def foreground_exe(): return ""
    def get_monitors(): return []
    def list_app_windows(exclude_pids=(), include_minimized=False): return []
    def minimize_window(hwnd): pass
    def find_spotify_window(): return None, ""
    def spotify_send(hwnd, action, use_keys=False): pass
    def launch_spotify(): return False


# ---- Only one copy at a time -----------------------------------------------
_instance_mutex = None


def acquire_single_instance():
    """True if we are the only Focus Lock running (keeps a named mutex open)."""
    global _instance_mutex
    if not IS_WINDOWS:
        return True
    h = kernel32.CreateMutexW(None, False, "Local\\FocusLock.SingleInstance")
    err = ctypes.get_last_error()
    if not h:
        return True                     # can't tell - don't lock the user out
    if err == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(h)
        return False
    _instance_mutex = h
    return True


def activate_existing_instance():
    hwnd = user32.FindWindowW("TkTopLevel", APP_TITLE) or user32.FindWindowW(None, APP_TITLE)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW)
    user32.SetForegroundWindow(hwnd)
    return True


# ============================================================================
#  Spotify (desktop app): song from its window title, controls via messages
# ============================================================================
SPOTIFY_GREEN = "#1db954"
SPOTIFY_IDLE_TITLES = {"spotify", "spotify premium", "spotify free"}


def parse_spotify_title(title):
    """Spotify's window title is 'Artist - Song' while playing, 'Spotify Premium' when paused.
    Returns (playing, song, artist); song is None when nothing is playing."""
    t = (title or "").strip()
    if not t or t.lower() in SPOTIFY_IDLE_TITLES:
        return False, None, None
    if " - " in t:
        artist, song = t.split(" - ", 1)
        return True, song.strip(), artist.strip()
    return True, t, ""                       # e.g. "Advertisement"


class SpotifyWatcher:
    def __init__(self):
        self.hwnd = None
        self.title = ""
        self.playing = False
        self.song = ""
        self.artist = ""
        self.played = 999.0          # seconds of the current song seen playing (999 = unknown)
        self.use_keys = False        # Spotify ignored our window message: use media keys
        self._last = 0.0
        self._check = None           # (due, title before, action) to confirm a command worked

    @property
    def running(self):
        return self.hwnd is not None

    def poll(self):
        now = time.monotonic()
        hwnd, title = find_spotify_window()
        self.hwnd, self.title = hwnd, title
        playing, song, artist = parse_spotify_title(title) if hwnd else (False, None, None)
        if song is not None and (song, artist) != (self.song, self.artist):
            # Only trust "it just started" if we were watching when it changed.
            self.played = 0.0 if now - self._last < 2.5 else 999.0
            self.song, self.artist = song, artist
        elif self.playing and playing:
            self.played += now - self._last
        self.playing = playing
        self._last = now
        if self._check and now >= self._check[0]:
            _due, before, action = self._check
            self._check = None
            if hwnd and title == before and not self.use_keys:
                self.use_keys = True
                self._send(action, 1)

    def command(self, action):
        """action: 'next' | 'prev' | 'toggle'."""
        if not self.hwnd:
            return
        presses = 1
        if action == "prev":
            # Spotify's "previous" restarts the song once it's a few seconds in;
            # a second press then goes back to the song played before.
            presses = 2 if self.played > 2.5 else 1
            self.played = 0.0
        else:
            self._check = (time.monotonic() + 1.5, self.title, action)
            if action == "toggle":
                self.playing = not self.playing     # show it straight away
            else:
                self.played = 0.0
        self._send(action, presses)

    def _send(self, action, presses):
        hwnd, keys = self.hwnd, self.use_keys

        def run():
            for i in range(presses):
                if i:
                    time.sleep(0.15)
                try:
                    spotify_send(hwnd, action, keys)
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True).start()


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
#  Chime (played on a worker thread so the UI never stalls)
# ============================================================================
CHIMES = {
    "done": [(659.25, 0.00), (880.00, 0.16), (1108.73, 0.32)],    # E5 A5 C#6, rising
    "resume": [(880.00, 0.00), (659.25, 0.18)],                    # A5 E5, back to work
}


def _chime_file(kind):
    path = os.path.join(SOUND_DIR, "chime-%s.wav" % kind)
    if os.path.isfile(path):
        return path
    rate, ring = 22050, 1.1
    notes = CHIMES[kind]
    n = int(rate * (notes[-1][1] + ring))
    buf = [0.0] * n
    for freq, start in notes:
        s0 = int(start * rate)
        w = 2 * math.pi * freq / rate
        for i in range(min(int(rate * ring), n - s0)):
            t = i / rate
            env = min(1.0, t / 0.006) * math.exp(-t * 4.2)
            buf[s0 + i] += env * (math.sin(w * i) + 0.3 * math.sin(2 * w * i))
    peak = max(abs(x) for x in buf) or 1.0
    data = struct.pack("<%dh" % n, *(int(x / peak * 0.55 * 32767) for x in buf))
    os.makedirs(SOUND_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(data)
    os.replace(tmp, path)
    return path


def play_chime(kind):
    def run():
        try:
            import winsound
        except ImportError:
            return
        try:
            winsound.PlaySound(_chime_file(kind),
                               winsound.SND_FILENAME | winsound.SND_NODEFAULT)
        except Exception:
            try:
                winsound.MessageBeep()
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()


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


def version_tuple(v):
    out = []
    for part in str(v or "0").split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


class TabServer:
    """
    Tiny HTTP server on 127.0.0.1, started with the app. The extension polls
    GET /state once a second and POSTs /status (heartbeat, "I locked the tab",
    errors). Between sessions active=False, so Chrome unlocks within a second.
    """

    def __init__(self):
        self.active = False
        self.paused = False
        self.exact = True
        self.session = ""
        self.locked_url = ""
        self.status = {}
        self.status_at = 0.0
        self.ext = {}               # what the extension last told us about itself
        self.ext_at = 0.0           # last /status from the extension
        self.poll_at = 0.0          # last /state poll (older extensions only poll)
        self.ext_version = ""       # extension version written to disk, if any
        self.port = TAB_PORT
        self._httpd = None

    def start(self):
        if self._httpd is not None:
            return True
        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler())
        except OSError:
            return False
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return True

    def begin(self, exact):
        """Start a new locked session."""
        self.session = "%d-%d" % (os.getpid(), int(time.time() * 1000))
        self.exact = bool(exact)
        self.locked_url = ""
        self.status = {}
        self.status_at = 0.0
        self.paused = False
        self.active = True

    def end(self):
        self.active = False
        self.paused = False

    def connected(self):
        return time.time() - max(self.ext_at, self.poll_at) < 5

    def snapshot(self):
        # During a break, report active=False so that even an extension from
        # before breaks existed stops enforcing; "paused" tells newer versions
        # to keep the session and return to the study tab afterwards.
        on_break = self.active and self.paused
        return {"active": self.active and not on_break, "paused": on_break, "exact": self.exact,
                "session": self.session, "locked_url": self.locked_url,
                "ext_version": self.ext_version}

    def set_status(self, data):
        if not isinstance(data, dict):
            return
        self.ext = {"id": str(data.get("id") or ""),
                    "version": str(data.get("version") or ""),
                    "incognito": data.get("incognito")}
        self.ext_at = time.time()
        if not self.session or data.get("session") != self.session:
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
                    srv.poll_at = time.time()
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
EXT_VERSION = "1.2"

EXT_MANIFEST = r"""{
  "manifest_version": 3,
  "name": "Focus Lock (companion)",
  "version": "__VERSION__",
  "description": "Locks Chrome to a single tab while the Focus Lock app is running a session.",
  "permissions": ["tabs", "webNavigation", "storage", "alarms"],
  "host_permissions": ["http://127.0.0.1/*"],
  "background": { "service_worker": "background.js" },
  "incognito": "spanning",
  "action": { "default_title": "Focus Lock" }
}
""".replace("__VERSION__", EXT_VERSION)

EXT_JS = r"""// Focus Lock companion extension (Manifest V3).
// The Focus Lock app serves /state and /status on 127.0.0.1; this script polls it.
const API = "http://127.0.0.1:9334";
const VERSION = chrome.runtime.getManifest().version;
const POLL_MS = 1000;
const HELLO_MS = 2000;      // heartbeat so the app can show "connected"
const GIVE_UP_MS = 10000;   // app stopped answering -> unlock (never trap the user)
const BOUNCE_LIMIT = 8;     // 8 bounces in 10s on the same site = page rewrites its own URL

let state = null;           // null when not locked; state.paused is true during a break
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

function enforcing() {
  return !!state && !state.paused;
}

function badge() {
  try {
    chrome.action.setBadgeText({ text: !state ? "" : state.paused ? "BRK" : "ON" });
    chrome.action.setBadgeBackgroundColor({ color: state && state.paused ? "#fbbf24" : "#38bdf8" });
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
async function incognitoAllowed() {
  try { return await chrome.extension.isAllowedIncognitoAccess(); } catch (e) { return null; }
}

async function report(session, locked, error, code) {
  lastReport = Date.now();
  const body = {
    session: session || "", locked: !!locked, paused: !!(state && state.paused),
    error: error || "", code: code || "", url: state ? state.good : "",
    incognito: await incognitoAllowed(), id: chrome.runtime.id, version: VERSION
  };
  try {
    await fetch(API + "/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  } catch (e) {}
}

function newer(a, b) {      // is version a newer than version b?
  const pa = String(a).split(".").map(Number), pb = String(b).split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0, y = pb[i] || 0;
    if (x !== y) return x > y;
  }
  return false;
}

async function maybeReload(want) {
  // The app rewrote our files with a newer version: pick them up (idle only).
  if (state || !want || !newer(want, VERSION)) return;
  try {
    const r = await chrome.storage.local.get("reloadAt");
    if (r.reloadAt && Date.now() - r.reloadAt < 60000) return;
    await chrome.storage.local.set({ reloadAt: Date.now() });
  } catch (e) {
    return;
  }
  chrome.runtime.reload();
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
  if (s.paused && s.session) {          // on a break: stop enforcing, keep the session
    if (state && state.session !== s.session) await unlock();
    if (state && !state.paused) await pause();
  } else if (s.active) {
    if (state && state.session !== s.session) await unlock();   // left over from an old session
    if (!state) {
      await lock(s);
    } else if (state.paused) {
      await resume();
    } else {
      await verify();
    }
  } else if (state) {
    await unlock();
  } else {
    await maybeReload(s.ext_version);
  }
  if (Date.now() - lastReport > HELLO_MS) await report(state ? state.session : "", !!state, "");
}

// ---- lock / unlock / break -------------------------------------------------
async function lock(s) {
  if (await incognitoAllowed() === false) {
    return report(s.session, false,
      "Focus Lock isn't allowed in Incognito, so a private window would get around the tab lock. " +
      "Open the extension's settings, switch on 'Allow in Incognito', then press Start again.",
      "incognito");
  }
  const recovering = !!s.locked_url;   // Chrome was restarted mid-session
  let tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true, windowType: "normal" });
  if (!tabs.length) tabs = await chrome.tabs.query({ active: true, windowType: "normal" });
  const tab = tabs[0];
  if (!tab && recovering) {
    state = { session: s.session, tabId: -1, windowId: -1, base: s.locked_url, good: s.locked_url,
              exact: !!s.exact, known: [], paused: false };
    await save();
    badge();
    return reopen(true);
  }
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
    base: base, good: base, exact: !!s.exact, paused: false,
    known: all.map(function (t) { return t.id; })
  };
  await save();
  badge();
  await focusStudyTab();
  await report(s.session, true, "");
}

async function unlock() {
  state = null;
  bounces = [];
  await save();
  badge();
}

async function pause() {
  state.paused = true;
  bounces = [];
  await save();
  badge();
  await report(state.session, true, "");
}

async function resume() {
  // Break is over: keep tabs opened during the break, but go back to the study tab.
  state.paused = false;
  try {
    const all = await chrome.tabs.query({});
    state.known = all.map(function (t) { return t.id; });
  } catch (e) {}
  await save();
  badge();
  let tab = null;
  try { tab = await chrome.tabs.get(state.tabId); } catch (e) {}
  if (!tab) {
    await reopen(false);
  } else {
    if (tab.url && !allowed(tab.url)) {
      try { await chrome.tabs.update(tab.id, { url: state.good }); } catch (e) {}
    }
    await focusStudyTab();
  }
  await report(state.session, true, "");
}

async function focusStudyTab() {
  if (!state) return;
  try {
    const t = await chrome.tabs.update(state.tabId, { active: true });
    state.windowId = t.windowId;
    const w = await chrome.windows.get(t.windowId);
    await chrome.windows.update(t.windowId,
      w.state === "minimized" ? { state: "normal", focused: true } : { focused: true });
  } catch (e) {}
}

// ---- enforcement -----------------------------------------------------------
async function bounce(url, preCommit) {
  if (!enforcing()) return;
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
  if (!enforcing()) return;
  if (allowed(url)) {
    if (committed && state.good !== url) { state.good = url; save(); }
  } else {
    bounce(url, !committed);
  }
}

async function reopen(newWindow) {
  // Bring the study tab back. If no normal window is left (e.g. on a Mac after
  // closing Chrome's last window, Chrome keeps running), open a fresh window.
  if (!state || reopening) return;
  reopening = true;
  try {
    let t = null;
    if (!newWindow) {
      const wins = await chrome.windows.getAll({ windowTypes: ["normal"] });
      const target = wins.find(function (w) { return w.id === state.windowId; }) || wins[0];
      if (target) {
        try {
          t = await chrome.tabs.create({ windowId: target.id, url: state.good, active: true });
        } catch (e) {
          t = null;
        }
      }
    }
    if (!t) {
      const w = await chrome.windows.create({ url: state.good, focused: true, type: "normal" });
      t = w && w.tabs && w.tabs[0];
    }
    if (t) {
      state.tabId = t.id;
      state.windowId = t.windowId;
      if (state.known.indexOf(t.id) === -1) state.known.push(t.id);
      await save();
      await focusStudyTab();
      await report(state.session, true, "");
    }
  } catch (e) {
  } finally {
    setTimeout(function () { reopening = false; }, 300);
  }
}

async function verify() {
  if (!enforcing() || reopening) return;
  let tab;
  try { tab = await chrome.tabs.get(state.tabId); } catch (e) { return reopen(false); }
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
  if (!enforcing() || reopening) return;     // the tab we create ourselves is adopted in reopen()
  if (tab.id !== state.tabId) chrome.tabs.remove(tab.id).catch(function () {});
});

chrome.tabs.onRemoved.addListener(async function (tabId, info) {
  await ready;
  if (enforcing() && tabId === state.tabId) await reopen(!!(info && info.isWindowClosing));
});

chrome.tabs.onActivated.addListener(async function (info) {
  await ready;
  if (!enforcing() || reopening || info.tabId === state.tabId) return;
  try {
    await chrome.tabs.update(state.tabId, { active: true });
    if (info.windowId !== state.windowId) {
      await chrome.windows.update(state.windowId, { focused: true });
    }
  } catch (e) {}
});

chrome.windows.onFocusChanged.addListener(async function (windowId) {
  await ready;
  if (!enforcing() || reopening) return;
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
  if (enforcing() && d.frameId === 0 && d.tabId === state.tabId) check(d.url, false);
});

chrome.tabs.onUpdated.addListener(async function (tabId, change) {
  await ready;
  if (enforcing() && tabId === state.tabId && change.url) check(change.url, true);
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
    """Write the extension files if they changed. Returns the folder."""
    os.makedirs(EXT_DIR, exist_ok=True)
    for name, text in (("manifest.json", EXT_MANIFEST), ("background.js", EXT_JS)):
        path = os.path.join(EXT_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                if f.read() == text:
                    continue
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return EXT_DIR


def refresh_installed_extension():
    """If the extension was installed before, keep its files current. True if up to date."""
    if not os.path.isfile(os.path.join(EXT_DIR, "manifest.json")):
        return False
    try:
        write_extension()
        return True
    except OSError:
        return False


# ============================================================================
#  Custom controls (drawn on canvases so everything can be rounded)
# ============================================================================
def mix(a, b, t):
    """Blend two #rrggbb colours: t=0 -> a, t=1 -> b."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * t)) for x, y in zip(ca, cb))


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts = (x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
           x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1)
    return cv.create_polygon(pts, smooth=True, **kw)


def watch_var(widget, var, callback):
    """var.trace that is removed again when the widget is destroyed."""
    def fire(*_a):
        try:
            if widget.winfo_exists():
                callback()
        except tk.TclError:
            pass
    tid = var.trace_add("write", fire)

    def gone(e):
        if e.widget is widget:
            try:
                var.trace_remove("write", tid)
            except (tk.TclError, ValueError):
                pass
    widget.bind("<Destroy>", gone, add="+")


def bold(size):
    return tkfont.Font(family=FONT, size=size, weight="bold")


class RoundButton(tk.Canvas):
    """Pill-shaped button. kind: primary | secondary | warn | ghost."""

    def __init__(self, parent, text, command, kind="primary", size=11,
                 padx=18, pady=9, hover_fg=None, width=None):
        self.bg = parent["bg"]
        self.text, self.command, self.kind = text, command, kind
        self.hover_fg = hover_fg
        self.font = bold(size)
        self.padx, self.fixed_w = px(padx), width
        self.h = self.font.metrics("linespace") + 2 * px(pady)
        self.w = width or self.font.measure(text) + 2 * self.padx
        super().__init__(parent, width=self.w, height=self.h, bg=self.bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.enabled, self.hover, self.pressed = True, False, False
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", self._resized)
        self._draw()

    def _colors(self):
        k = self.kind
        if not self.enabled:
            return (PANEL if k != "ghost" else None), SUBTLE
        if k == "primary":
            fill, fg = (ACCENT_HI if self.hover else ACCENT), INK
        elif k == "warn":
            fill, fg = (WARN_HI if self.hover else WARN), INK
        elif k == "secondary":
            fill, fg = (PANEL_HI if self.hover else PANEL), FG
        else:  # ghost
            fill = PANEL if self.hover else None
            fg = (self.hover_fg or FG) if self.hover else MUTED
        if self.pressed and fill:
            fill = mix(fill, BG, 0.18)
        return fill, fg

    def _draw(self):
        self.delete("all")
        fill, fg = self._colors()
        if fill:
            round_rect(self, 0, 0, self.w - 1, self.h - 1, self.h / 2.0, fill=fill, outline=fill)
        self.create_text(self.w / 2.0, self.h / 2.0, text=self.text, font=self.font, fill=fg)

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self._draw()

    def _resized(self, e):
        if (e.width, e.height) != (self.w, self.h):
            self.w, self.h = e.width, e.height
            self._draw()

    def _release(self, e):
        was = self.pressed
        self._set(pressed=False)
        if was and self.enabled and 0 <= e.x <= self.w and 0 <= e.y <= self.h and self.command:
            self.command()

    def _stretched(self):
        try:
            return self.winfo_manager() == "pack" and self.pack_info().get("fill") in ("x", "both")
        except tk.TclError:
            return False

    def set_text(self, text):
        self.text = text
        if not self.fixed_w and not self._stretched():
            self.w = self.font.measure(text) + 2 * self.padx
            self.configure(width=self.w)
        self._draw()

    def set_enabled(self, on):
        self.enabled = bool(on)
        self.configure(cursor="hand2" if on else "arrow")
        self._draw()


class Toggle(tk.Frame):
    """Switch + label (+ optional muted line underneath)."""

    def __init__(self, parent, text, variable, sub=None, command=None):
        bg = parent["bg"]
        super().__init__(parent, bg=bg, cursor="hand2")
        self.var, self.command = variable, command
        self.sw = tk.Canvas(self, width=px(38), height=px(22), bg=bg,
                            highlightthickness=0, cursor="hand2")
        self.sw.pack(side="left", anchor="n", pady=(px(1), 0))
        col = tk.Frame(self, bg=bg, cursor="hand2")
        col.pack(side="left", padx=(px(10), 0), fill="x")
        parts = [self, self.sw, col,
                 tk.Label(col, text=text, bg=bg, fg=FG, font=(FONT, 10), cursor="hand2")]
        parts[-1].pack(anchor="w")
        if sub:
            parts.append(tk.Label(col, text=sub, bg=bg, fg=SUBTLE, font=(FONT, 9),
                                  cursor="hand2", justify="left"))
            parts[-1].pack(anchor="w")
        for w in parts:
            w.bind("<Button-1>", self.toggle)
        watch_var(self, variable, self._draw)
        self._draw()

    def toggle(self, _e=None):
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _draw(self):
        c = self.sw
        c.delete("all")
        w, h = px(38), px(22)
        on = bool(self.var.get())
        round_rect(c, 0, 0, w - 1, h - 1, h / 2.0,
                   fill=ACCENT if on else PANEL, outline=ACCENT if on else BORDER)
        d = h - px(8)
        x = w - px(4) - d if on else px(4)
        c.create_oval(x, px(4), x + d, px(4) + d, fill="#f8fafc" if on else MUTED, outline="")


class Segmented(tk.Canvas):
    """Row of options, one of them selected (bound to a StringVar)."""

    def __init__(self, parent, options, variable, command=None, size=10,
                 padx=14, pady=8, equal=False):
        self.font = bold(size)
        self.options, self.var, self.command = options, variable, command
        self.inset = px(3)
        widths = [self.font.measure(label) + 2 * px(padx) for _v, label in options]
        if equal:
            widths = [max(widths)] * len(widths)
        self.h = self.font.metrics("linespace") + 2 * px(pady)
        self.w = sum(widths) + 2 * self.inset
        super().__init__(parent, width=self.w, height=self.h, bg=parent["bg"],
                         highlightthickness=0, bd=0, cursor="hand2")
        self.spans, x = [], self.inset
        for wd in widths:
            self.spans.append((x, x + wd))
            x += wd
        self.hover = None
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", lambda e: self._hover(self._index(e.x)))
        self.bind("<Leave>", lambda e: self._hover(None))
        watch_var(self, variable, self._draw)
        self._draw()

    def _index(self, x):
        for i, (a, b) in enumerate(self.spans):
            if a <= x < b:
                return i
        return None

    def _hover(self, i):
        if i != self.hover:
            self.hover = i
            self._draw()

    def _click(self, e):
        i = self._index(e.x)
        if i is not None:
            self.var.set(self.options[i][0])
            if self.command:
                self.command()

    def _draw(self):
        self.delete("all")
        r = self.h / 2.0
        round_rect(self, 0, 0, self.w - 1, self.h - 1, r, fill=PANEL, outline=PANEL)
        cur = str(self.var.get()).strip()
        ins = self.inset
        for i, ((value, label), (a, b)) in enumerate(zip(self.options, self.spans)):
            sel = str(value) == cur
            if sel:
                round_rect(self, a, ins, b, self.h - 1 - ins, r - ins, fill=ACCENT, outline=ACCENT)
            elif i == self.hover:
                round_rect(self, a, ins, b, self.h - 1 - ins, r - ins, fill=PANEL_HI, outline=PANEL_HI)
            fg = INK if sel else (FG if i == self.hover else MUTED)
            self.create_text((a + b) / 2.0, self.h / 2.0, text=label, font=self.font, fill=fg)


class Stepper(tk.Canvas):
    """  -  value  +   pill, stepping through a fixed list of values."""

    def __init__(self, parent, values, variable, fmt, command=None, size=10):
        self.values, self.var, self.fmt, self.command = values, variable, fmt, command
        self.font = bold(size)
        self.h = self.font.metrics("linespace") + 2 * px(8)
        self.bw = self.h
        vw = max(self.font.measure(fmt(v)) for v in values) + px(14)
        self.w = 2 * self.bw + vw
        super().__init__(parent, width=self.w, height=self.h, bg=parent["bg"],
                         highlightthickness=0, bd=0, cursor="hand2")
        self.enabled, self.hover = True, 0
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._sethover(0))
        watch_var(self, variable, self._draw)
        self._draw()

    def _pos(self):
        v = _nearest(self.var.get(), self.values)
        return self.values.index(v)

    def _zone(self, x):
        if x < self.bw:
            return -1
        if x > self.w - self.bw:
            return 1
        return 0

    def _motion(self, e):
        self._sethover(self._zone(e.x))

    def _sethover(self, z):
        if z != self.hover:
            self.hover = z
            self._draw()

    def _click(self, e):
        z = self._zone(e.x)
        if not z or not self.enabled:
            return
        i = self._pos() + z
        if 0 <= i < len(self.values):
            self.var.set(self.values[i])
            if self.command:
                self.command()

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._draw()

    def _draw(self):
        self.delete("all")
        h, w, bw = self.h, self.w, self.bw
        round_rect(self, 0, 0, w - 1, h - 1, h / 2.0, fill=PANEL, outline=PANEL)
        i = self._pos()
        for z, cx, can in ((-1, bw / 2.0, i > 0), (1, w - bw / 2.0, i < len(self.values) - 1)):
            can = can and self.enabled
            if can and self.hover == z:
                d = h - px(8)
                self.create_oval(cx - d / 2, h / 2 - d / 2, cx + d / 2, h / 2 + d / 2,
                                 fill=PANEL_HI, outline="")
            col = FG if can else SUBTLE
            L = px(4.5)
            self.create_line(cx - L, h / 2, cx + L, h / 2, fill=col, width=px(2), capstyle="round")
            if z > 0:
                self.create_line(cx, h / 2 - L, cx, h / 2 + L, fill=col, width=px(2), capstyle="round")
        self.create_text(w / 2.0, h / 2.0, text=self.fmt(self.values[i]), font=self.font,
                         fill=FG if self.enabled else SUBTLE)


class RoundEntry(tk.Canvas):
    """Text entry inside a rounded box that lights up when focused."""

    def __init__(self, parent, variable, width, size=11, justify="left"):
        font = tkfont.Font(family=FONT, size=size)
        self.h = font.metrics("linespace") + 2 * px(8)
        super().__init__(parent, width=width, height=self.h, bg=parent["bg"],
                         highlightthickness=0, bd=0, cursor="xterm")
        self.entry = tk.Entry(self, textvariable=variable, font=font, bg=PANEL, fg=FG,
                              insertbackground=FG, relief="flat", bd=0,
                              highlightthickness=0, justify=justify,
                              disabledbackground=PANEL, selectbackground=ACCENT,
                              selectforeground=INK)
        self._win = self.create_window(0, 0, window=self.entry, anchor="w")
        self.focused, self.error = False, False
        self.entry.bind("<FocusIn>", lambda e: self._set(focused=True), add="+")
        self.entry.bind("<FocusOut>", lambda e: self._set(focused=False), add="+")
        self.entry.bind("<Key>", lambda e: self.error and self._set(error=False), add="+")
        self.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self._draw()

    def _draw(self):
        w = self.winfo_width() if self.winfo_ismapped() else int(self.cget("width"))
        h = self.h
        self.delete("box")
        outline = DANGER if self.error else (ACCENT if self.focused else BORDER)
        round_rect(self, 1, 1, w - 2, h - 2, px(10), fill=PANEL, outline=outline, width=px(1.5),
                   tags="box")
        self.tag_lower("box")
        pad = px(12)
        self.coords(self._win, pad, h / 2.0)
        self.itemconfig(self._win, width=max(10, w - 2 * pad))

    def focus_entry(self):
        self.entry.focus_force()


class StatCard(tk.Canvas):
    def __init__(self, parent, width, height, title, value, sub, dots=None, color=ACCENT):
        super().__init__(parent, width=width, height=height, bg=parent["bg"],
                         highlightthickness=0, bd=0)
        w, h, p = width, height, px(16)
        round_rect(self, 1, 1, w - 2, h - 2, px(16), fill=SURFACE, outline=BORDER)
        self.create_text(p, p, anchor="nw", text=title, fill=SUBTLE, font=(FONT, 8, "bold"))
        self.create_text(p, p + px(17), anchor="nw", text=value, fill=FG, font=(FONT, 19, "bold"))
        self.create_text(p, h - p + px(2), anchor="sw", text=sub, fill=MUTED, font=(FONT, 9))
        if dots:
            d, gap = px(8), px(4)
            x = w - p - len(dots) * d - (len(dots) - 1) * gap
            y = p + px(2)
            for i, on in enumerate(dots):
                last = i == len(dots) - 1
                self.create_oval(x, y, x + d, y + d, fill=color if on else "#334155",
                                 outline=FG if last and not on else (color if on else "#334155"))
                x += d + gap


class CheckList(tk.Frame):
    """Scrollable list of apps with rounded check boxes."""

    def __init__(self, parent, width, height):
        bg = parent["bg"]
        super().__init__(parent, bg=bg)
        self.box = tk.Canvas(self, width=width, height=height, bg=bg, highlightthickness=0, bd=0)
        self.box.pack(fill="x")
        self.view = tk.Canvas(self.box, bg=SURFACE, highlightthickness=0, bd=0,
                              yscrollincrement=px(16))
        self.rows = tk.Frame(self.view, bg=SURFACE)
        self._rows_win = self.view.create_window(0, 0, window=self.rows, anchor="nw")
        self._view_win = self.box.create_window(px(5), px(6), window=self.view, anchor="nw")
        self.box.bind("<Configure>", lambda e: self._layout())
        self.rows.bind("<Configure>", lambda e: self._scrolled())
        self.vars = {}
        self.bind_all("<MouseWheel>", self._wheel, add="+")
        self.bind_all("<Button-4>", self._wheel, add="+")
        self.bind_all("<Button-5>", self._wheel, add="+")

    def _layout(self):
        w, h = self.box.winfo_width(), self.box.winfo_height()
        self.box.delete("frame")
        round_rect(self.box, 1, 1, w - 2, h - 2, px(14), fill=SURFACE, outline=BORDER, tags="frame")
        self.box.tag_lower("frame")
        self.vw, self.vh = max(10, w - px(5) - px(14)), max(10, h - px(12))
        self.box.itemconfig(self._view_win, width=self.vw, height=self.vh)
        self.view.itemconfig(self._rows_win, width=self.vw)
        self._scrolled()

    def _scrolled(self):
        if not self.winfo_exists():
            return
        rh = self.rows.winfo_reqheight()
        self.view.configure(scrollregion=(0, 0, getattr(self, "vw", 1), rh))
        self.box.delete("thumb")
        top, bottom = self.view.yview()
        if bottom - top < 0.999:
            h = self.box.winfo_height()
            x = self.box.winfo_width() - px(8)
            y1 = px(10) + (h - px(20)) * top
            y2 = px(10) + (h - px(20)) * bottom
            self.box.create_line(x, y1, x, y2, fill=PANEL_HI, width=px(4),
                                 capstyle="round", tags="thumb")

    def _wheel(self, e):
        try:
            if not self.winfo_exists():
                return
            w = self.winfo_containing(e.x_root, e.y_root)
        except (tk.TclError, KeyError):
            return
        if w is None or not str(w).startswith(str(self)):
            return
        if getattr(e, "num", None) == 4:
            step = -1
        elif getattr(e, "num", None) == 5:
            step = 1
        else:
            step = -1 if e.delta > 0 else 1
        self.view.yview_scroll(step * 2, "units")
        self._scrolled()

    def set_items(self, items):
        """items: [(exe, title, checked)]"""
        for w in self.rows.winfo_children():
            w.destroy()
        self.vars = {}
        if not items:
            tk.Label(self.rows, text="No other apps are open right now.", bg=SURFACE,
                     fg=SUBTLE, font=(FONT, 9)).pack(anchor="w", padx=px(10), pady=px(10))
        for exe, title, checked in items:
            var = tk.BooleanVar(value=checked)
            self.vars[exe] = var
            self._row(exe, title, var)
        self.view.yview_moveto(0)
        self.after_idle(self._scrolled)

    def checked(self):
        return sorted(exe for exe, v in self.vars.items() if v.get())

    def _row(self, exe, title, var):
        row = tk.Frame(self.rows, bg=SURFACE, cursor="hand2")
        row.pack(fill="x")
        s = px(18)
        chk = tk.Canvas(row, width=s, height=s, bg=SURFACE, highlightthickness=0, cursor="hand2")
        chk.pack(side="left", padx=(px(10), px(10)), pady=px(6))
        name = tk.Label(row, text=exe, bg=SURFACE, fg=FG, font=(FONT, 10), cursor="hand2")
        name.pack(side="left")
        sub = tk.Label(row, text=title[:38], bg=SURFACE, fg=SUBTLE, font=(FONT, 9),
                       anchor="w", cursor="hand2")
        sub.pack(side="left", padx=(px(8), px(4)), fill="x", expand=True)
        parts = (row, chk, name, sub)

        def draw():
            chk.delete("all")
            on = var.get()
            round_rect(chk, 1, 1, s - 2, s - 2, px(5), fill=ACCENT if on else PANEL,
                       outline=ACCENT if on else MUTED)
            if on:
                chk.create_line(s * 0.27, s * 0.52, s * 0.44, s * 0.69, s * 0.74, s * 0.33,
                                fill=INK, width=max(2, px(2)), capstyle="round", joinstyle="round")

        def toggle(_e=None):
            var.set(not var.get())
            draw()

        def hover(on):
            for w in parts:
                w.configure(bg=PANEL if on else SURFACE)

        for w in parts:
            w.bind("<Button-1>", toggle)
            w.bind("<Enter>", lambda e: hover(True))
            w.bind("<Leave>", lambda e: hover(False))
        draw()


class Ring(tk.Canvas):
    """Clock-style progress ring: tick marks, a sweeping arc, text in the middle."""

    def __init__(self, parent, size, thickness, color=ACCENT, ticks=60,
                 time_px=None, sub_px=None):
        super().__init__(parent, width=size, height=size, bg=parent["bg"],
                         highlightthickness=0, bd=0)
        self.size, self.th, self.color, self.nticks = size, thickness, color, ticks
        c = self.c = size / 2.0
        outer = c - 2
        major = max(4.0, size * 0.04)
        minor = major * 0.5
        if ticks:
            self.rr = outer - major - max(5.0, size * 0.035) - thickness / 2.0
        else:
            self.rr = outer - thickness / 2.0
        rr = self.rr
        self.ticks = []
        for i in range(ticks):
            a = math.radians(90 - 360.0 * i / ticks)
            is_major = ticks <= 12 or i % 5 == 0
            r2 = outer - (major if is_major else minor)
            tid = self.create_line(c + outer * math.cos(a), c - outer * math.sin(a),
                                   c + r2 * math.cos(a), c - r2 * math.sin(a),
                                   width=max(1, px(2) if is_major else px(1)),
                                   capstyle="round", fill=BORDER)
            self.ticks.append((tid, is_major))
        self.create_oval(c - rr, c - rr, c + rr, c + rr, outline=PANEL, width=thickness)
        self.arc = self.create_arc(c - rr, c - rr, c + rr, c + rr, start=90, extent=-1,
                                   style="arc", outline=color, width=thickness)
        d = thickness / 2.0
        self.cap_a = self.create_oval(c - d, c - rr - d, c + d, c - rr + d, fill=color, outline="")
        self.cap_b = self.create_oval(0, 0, 0, 0, fill=color, outline="")
        self.tip = self.create_oval(0, 0, 0, 0, fill="#f8fafc", outline="")
        self.time_px = time_px or int(size * 0.17)
        self.sub_px = sub_px or max(px(9), int(size * 0.04))
        self.top_id = self.create_text(c, c - self.time_px * 0.92, text="", fill=SUBTLE,
                                       font=(FONT, -int(self.sub_px * 0.8), "bold"))
        self.time_id = self.create_text(c, c, text="", fill=FG,
                                        font=(FONT, -self.time_px, "bold"))
        self.sub_id = self.create_text(c, c + self.time_px * 0.88, text="", fill=MUTED,
                                       font=(FONT, -self.sub_px))
        self._lit = -1
        self._long = None

    def set_color(self, color):
        self.color = color
        for item in (self.cap_a, self.cap_b):
            self.itemconfig(item, fill=color)
        self.itemconfig(self.arc, outline=color)
        self._lit = -1

    def set(self, frac, text=None, sub=None, top=None, sub_color=None):
        frac = max(0.0, min(1.0, frac))
        c, rr = self.c, self.rr
        moving = (self.arc, self.cap_a, self.cap_b, self.tip)
        if frac < 0.001:
            for item in moving:
                self.itemconfig(item, state="hidden")
        else:
            self.itemconfig(self.arc, extent=-min(359.99, 360.0 * frac))
            a = math.radians(90 - 360.0 * frac)
            x, y = c + rr * math.cos(a), c - rr * math.sin(a)
            d, t = self.th / 2.0, self.th * 0.22
            self.coords(self.cap_b, x - d, y - d, x + d, y + d)
            self.coords(self.tip, x - t, y - t, x + t, y + t)
            for item in moving:
                self.itemconfig(item, state="normal")
        if self.nticks:
            lit = int(math.ceil(frac * self.nticks - 1e-9))
            if lit != self._lit:
                self._lit = lit
                for i, (tid, major) in enumerate(self.ticks):
                    if i < lit:
                        col = self.color if major else mix(self.color, self["bg"], 0.45)
                    else:
                        col = "#334155" if major else BORDER
                    self.itemconfig(tid, fill=col)
        if text is not None:
            long_text = len(text) > 5
            if long_text != self._long:
                self._long = long_text
                size = int(self.time_px * (0.74 if long_text else 1.0))
                self.itemconfig(self.time_id, font=(FONT, -size, "bold"))
            self.itemconfig(self.time_id, text=text)
        if sub is not None:
            self.itemconfig(self.sub_id, text=sub, fill=sub_color or MUTED)
        if top is not None:
            self.itemconfig(self.top_id, text=top)


class SpotifyCard(tk.Canvas):
    """Now playing + back / play-pause / skip for the Spotify desktop app."""

    def __init__(self, parent, watcher, width, height):
        super().__init__(parent, width=width, height=height, bg=parent["bg"],
                         highlightthickness=0, bd=0)
        self.watcher, self.w, self.h = watcher, width, height
        self.f_song = bold(10)
        self.f_sub = tkfont.Font(family=FONT, size=9)
        self.f_tag = bold(7)
        self.hover = None
        self.hits = {}               # name -> (x1, y1, x2, y2)
        self._sig = None
        self.bind("<Motion>", lambda e: self._set_hover(self._hit(e.x, e.y)))
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.bind("<Button-1>", self._click)
        self.refresh()

    def _hit(self, x, y):
        for name, (x1, y1, x2, y2) in self.hits.items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                return name
        return None

    def _set_hover(self, name):
        if name != self.hover:
            self.hover = name
            self.configure(cursor="hand2" if name else "arrow")
            self.refresh()

    def _click(self, e):
        name = self._hit(e.x, e.y)
        if name == "open":
            launch_spotify()
        elif name:
            self.watcher.command(name)
        else:
            return
        self.refresh()
        self.after(450, self._repoll)

    def _repoll(self):
        try:
            if self.winfo_exists():
                self.watcher.poll()
                self.refresh()
        except tk.TclError:
            pass

    def _fit(self, text, font, width):
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text.rstrip() + "…"

    def refresh(self):
        wt = self.watcher
        sig = (wt.running, wt.playing, wt.song, wt.artist, self.hover)
        if sig != self._sig:
            self._sig = sig
            self._draw()

    def _draw(self):
        self.delete("all")
        self.hits = {}
        w, h, p, wt = self.w, self.h, px(12), self.watcher
        round_rect(self, 1, 1, w - 2, h - 2, px(16), fill=SURFACE, outline=BORDER)

        # header: logo, label, state
        r = px(6)
        lx, ly = p + r, p + r
        self.create_oval(lx - r, ly - r, lx + r, ly + r, fill=SPOTIFY_GREEN, outline="")
        for k, (half, dy) in enumerate(((0.62, -0.32), (0.5, 0.02), (0.38, 0.33))):
            a = r * half
            y = ly + r * dy
            self.create_line(lx - a, y, lx, y - r * 0.12, lx + a, y + r * 0.04, smooth=True,
                             fill=INK, width=max(1, px(1.6) - k * 0.2), capstyle="round")
        self.create_text(lx + r + px(6), ly, anchor="w", text="SPOTIFY", font=self.f_tag, fill=SUBTLE)
        if not wt.running:
            state, color = "Not open", SUBTLE
        elif wt.playing:
            state, color = "Playing", SPOTIFY_GREEN
        else:
            state, color = "Paused", MUTED
        self.create_text(w - p, ly, anchor="e", text=state, font=self.f_tag, fill=color)

        # song + artist
        if not wt.running:
            song, sub = "Spotify isn't open", "Open it to control music here"
        elif wt.song:
            song, sub = wt.song, wt.artist
        else:
            song, sub = "Nothing playing", "Press play to start"
        tw = w - 2 * p
        y = ly + r + px(6)
        self.create_text(p, y, anchor="nw", text=self._fit(song, self.f_song, tw),
                         font=self.f_song, fill=FG)
        y += self.f_song.metrics("linespace")
        self.create_text(p, y, anchor="nw", text=self._fit(sub or "", self.f_sub, tw),
                         font=self.f_sub, fill=MUTED)

        # controls
        cy = h - p - px(14)
        if not wt.running:
            bw, bh = px(112), px(26)
            x1, y1 = (w - bw) / 2.0, cy - bh / 2.0
            fill = PANEL_HI if self.hover == "open" else PANEL
            round_rect(self, x1, y1, x1 + bw, y1 + bh, bh / 2.0, fill=fill, outline=fill)
            self.create_text(w / 2.0, cy, text="Open Spotify", font=self.f_tag, fill=FG)
            self.hits["open"] = (x1, y1, x1 + bw, y1 + bh)
            return
        gap = px(40)
        for name, cx in (("prev", w / 2.0 - gap), ("toggle", w / 2.0), ("next", w / 2.0 + gap)):
            big = name == "toggle"
            d = px(30) if big else px(26)
            hov = self.hover == name
            if big:
                fill = "#1ed760" if hov else SPOTIFY_GREEN
            else:
                fill = PANEL_HI if hov else None
            if fill:
                self.create_oval(cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2, fill=fill, outline="")
            self.hits[name] = (cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2)
            ink = INK if big else FG
            s = px(5)
            if name == "toggle" and wt.playing:          # pause
                bwid = px(3)
                for x in (cx - px(4.5), cx + px(1.5)):
                    self.create_rectangle(x, cy - s, x + bwid, cy + s, fill=ink, outline="")
            elif name == "toggle":                        # play
                self.create_polygon(cx - s * 0.6, cy - s * 1.05, cx - s * 0.6, cy + s * 1.05,
                                    cx + s * 1.05, cy, fill=ink, outline="")
            else:
                sign = 1 if name == "next" else -1
                tip, base = cx + sign * s * 0.55, cx - sign * s * 0.85
                self.create_polygon(base, cy - s, base, cy + s, tip, cy, fill=ink, outline="")
                bx = tip if sign > 0 else tip - px(2)
                self.create_rectangle(bx, cy - s, bx + px(2), cy + s, fill=ink, outline="")


def section(parent, text, pady=(16, 7)):
    lab = tk.Label(parent, text=text.upper(), bg=parent["bg"], fg=SUBTLE, font=(FONT, 8, "bold"))
    lab.pack(anchor="w", pady=(px(pady[0]), px(pady[1])))
    return lab


def label(parent, text, size=10, color=FG, weight="normal", **kw):
    return tk.Label(parent, text=text, bg=parent["bg"], fg=color,
                    font=(FONT, size, weight), **kw)


# ============================================================================
#  Main app
# ============================================================================
class FocusLock(tk.Tk):
    def __init__(self):
        super().__init__()
        global _SCALE
        try:
            _SCALE = max(1.0, self.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            _SCALE = 1.0
        self.title(APP_TITLE)
        self.configure(bg=BG)
        self.settings = load_settings()
        self.stats = Stats()

        self.phase = "setup"            # setup | starting | focus | break | done
        self.gen = 0                    # bumps on every phase change; stops stale loops
        self.full_lock = True
        self.tab_locked = False
        self.total_seconds = 0
        self.remaining = 0.0            # focus seconds left (frozen during a break)
        self.end_time = 0.0
        self.breaks_total = 0
        self.breaks_left = 0
        self.break_secs = 0
        self.break_end = 0.0
        self.seg_start = None           # monotonic start of focus time not yet saved
        self.session_focus = 0.0
        self.covers = []
        self.cover_labels = []
        self.quit_entry = None
        self.ring = None
        self.allowed = set()
        self.blocker = KeyBlocker()
        self.tabsrv = TabServer()
        self.port_ok = self.tabsrv.start()
        if refresh_installed_extension():
            self.tabsrv.ext_version = EXT_VERSION
        self._last_relaunch = 0.0
        self._status_token = None
        self._break_armed_until = 0.0
        self.spotify = SpotifyWatcher()
        self.spotify_on = False
        self.spotify_card = None

        self.container = tk.Frame(self, bg=BG)
        self.container.pack(expand=True, fill="both")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.show_setup()

    # ---- housekeeping ------------------------------------------------------
    def new_phase(self, phase):
        self.phase = phase
        self.gen += 1
        return self.gen

    def clear(self):
        self.unbind("<Return>")
        self.ring = None
        self.spotify_card = None
        for w in self.container.winfo_children():
            w.destroy()

    def wm_attr(self, win, name, value):
        try:
            win.attributes(name, value)
        except tk.TclError:
            pass                        # e.g. -toolwindow off Windows

    def on_close(self):
        if self.phase in ("focus", "break"):
            return  # ignore the X button / Alt+F4 during a session
        if self.phase == "starting":
            self.cancel_start()
        self.shutdown()
        self.destroy()

    def shutdown(self):
        try:
            self.credit_focus(stop=True)
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
        px_, py_, pw, ph = self.primary_monitor()
        x = px_ + (pw - self.winfo_reqwidth()) // 2
        y = py_ + max(0, (ph - self.winfo_reqheight()) // 3)
        self.geometry("+%d+%d" % (x, y))

    def place_top_center(self):
        self.update_idletasks()
        px_, py_, pw, _ph = self.primary_monitor()
        self.geometry("+%d+%d" % (px_ + (pw - self.winfo_reqwidth()) // 2, py_ + px(6)))

    def release_window(self):
        """Undo everything a lock screen did to the window."""
        self.quit_entry = None
        for cv in self.covers:
            try:
                cv.destroy()
            except tk.TclError:
                pass
        self.covers = []
        self.cover_labels = []
        self.unbind("<Alt-F4>")
        self.unbind("<Escape>")
        self.wm_attr(self, "-fullscreen", False)
        self.wm_attr(self, "-topmost", False)
        self.wm_attr(self, "-toolwindow", False)
        self.container.configure(padx=0, pady=0)
        self.resizable(False, False)
        self.geometry("")

    def credit_focus(self, stop=False):
        """Add focus time since the last save to today's total."""
        if self.seg_start is None:
            return
        now = time.monotonic()
        secs = now - self.seg_start
        self.seg_start = None if stop else now
        if secs > 0:
            self.session_focus += secs
            self.stats.add(secs)

    def chime(self, kind):
        if IS_WINDOWS:
            play_chime(kind)
        else:
            self.bell()

    # ---- setup screen ------------------------------------------------------
    def show_setup(self):
        self.new_phase("setup")
        self.release_window()
        self.clear()
        s = self.settings
        W, gap = px(620), px(18)
        colw = (W - gap) // 2

        outer = tk.Frame(self.container, bg=BG, padx=px(30), pady=px(24))
        outer.pack()
        tk.Frame(outer, width=W, height=1, bg=BG).pack()

        # Header
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        logo = Ring(head, px(42), px(5), ticks=0)
        logo.set(0.7)
        logo.pack(side="left")
        tf = tk.Frame(head, bg=BG)
        tf.pack(side="left", padx=(px(12), 0))
        label(tf, APP_TITLE, 18, weight="bold").pack(anchor="w")
        label(tf, "Pick a time. Once it starts, the only way out is the timer.",
              9, MUTED).pack(anchor="w")

        # Today / week / streak
        today, streak = self.stats.today(), self.stats.streak()
        cards = tk.Frame(outer, bg=BG)
        cards.pack(fill="x", pady=(px(18), 0))
        cw, ch = (W - 2 * px(12)) // 3, px(100)
        if today >= STREAK_MIN_SECONDS:
            today_sub = "Streak kept for today"
        else:
            today_sub = "%s more for the streak" % fmt_duration(STREAK_MIN_SECONDS - today + 59)
        StatCard(cards, cw, ch, "TODAY'S FOCUS", fmt_duration(today), today_sub).pack(side="left")
        StatCard(cards, cw, ch, "THIS WEEK", fmt_duration(self.stats.week()),
                 "Last 7 days").pack(side="left", padx=px(12))
        best = self.stats.best_streak()
        StatCard(cards, cw, ch, "STREAK", plural(streak, "day"),
                 "Best: %s" % plural(best, "day") if best else "Focus 5 min a day",
                 dots=self.stats.last7(), color=GOOD).pack(side="left")

        # Duration + breaks side by side
        grid = tk.Frame(outer, bg=BG)
        grid.pack(fill="x")
        grid.columnconfigure(0, minsize=colw)
        grid.columnconfigure(1, minsize=colw)
        left = tk.Frame(grid, bg=BG)
        left.grid(row=0, column=0, sticky="nw", padx=(0, gap))
        right = tk.Frame(grid, bg=BG)
        right.grid(row=0, column=1, sticky="nw")

        section(left, "Duration")
        r = tk.Frame(left, bg=BG)
        r.pack(anchor="w")
        self.minutes_var = tk.StringVar(value=str(s["minutes"]))
        Segmented(r, [(str(m), str(m)) for m in PRESETS], self.minutes_var,
                  padx=11).pack(side="left")
        RoundEntry(r, self.minutes_var, px(62), justify="center").pack(side="left", padx=(px(8), 0))
        label(r, "min", 10, MUTED).pack(side="left", padx=(px(6), 0))
        self.spotify_var = tk.BooleanVar(value=bool(s["spotify"]))
        Toggle(left, "Spotify controls", self.spotify_var,
               sub="Song, skip and back next to the timer").pack(anchor="w", pady=(px(14), 0))

        section(right, "Breaks")
        r = tk.Frame(right, bg=BG)
        r.pack(anchor="w")
        self.breaks_var = tk.IntVar(value=s["breaks"])
        self.break_min_var = tk.IntVar(value=s["break_minutes"])
        Stepper(r, BREAK_COUNTS, self.breaks_var,
                lambda n: "No breaks" if n == 0 else plural(n, "break"),
                command=self._breaks_changed).pack(side="left")
        self.break_of = label(r, "of", 10, MUTED)
        self.break_of.pack(side="left", padx=px(8))
        self.break_len = Stepper(r, BREAK_MINUTES, self.break_min_var, lambda m: "%d min" % m)
        self.break_len.pack(side="left")
        label(right, "A break freezes the timer and unlocks everything. "
                     "It re-locks by itself when the break ends.",
              9, SUBTLE, wraplength=colw, justify="left").pack(anchor="w", pady=(px(6), 0))
        self._breaks_changed()

        # Mode
        section(outer, "Mode")
        mr = tk.Frame(outer, bg=BG)
        mr.pack(fill="x")
        self.mode_var = tk.StringVar(value=s["mode"] if s["mode"] in ("full", "study") else "full")
        seg = Segmented(mr, [("full", "Full lock"), ("study", "Study mode")], self.mode_var,
                        command=self.on_mode_change, equal=True, padx=18)
        seg.pack(side="left")
        self.mode_desc = label(mr, "", 9, MUTED, justify="left",
                               wraplength=W - seg.w - px(18))
        self.mode_desc.pack(side="left", padx=(px(16), 0))

        # Study-mode options (shown only in study mode)
        self.study_slot = tk.Frame(outer, bg=BG)
        self.study_slot.pack(fill="x")
        sf = self.study_frame = tk.Frame(self.study_slot, bg=BG)
        sg = tk.Frame(sf, bg=BG)
        sg.pack(fill="x")
        sg.columnconfigure(0, minsize=colw)
        sg.columnconfigure(1, minsize=colw)
        cl = tk.Frame(sg, bg=BG)
        cl.grid(row=0, column=0, sticky="nw", padx=(0, gap))
        cr = tk.Frame(sg, bg=BG)
        cr.grid(row=0, column=1, sticky="nw")

        section(cl, "Chrome")
        st = tk.Frame(cl, bg=BG)
        st.pack(anchor="w", fill="x")
        self.ext_dot = tk.Canvas(st, width=px(10), height=px(10), bg=BG, highlightthickness=0)
        self.ext_dot.pack(side="left")
        self.ext_lbl = label(st, "Checking for the extension...", 9, MUTED,
                             wraplength=colw - px(20), justify="left")
        self.ext_lbl.pack(side="left", padx=(px(8), 0))
        self.ext_btns = tk.Frame(cl, bg=BG)
        self.ext_btns.pack(anchor="w", pady=(px(8), px(4)))
        self.ext_fix = RoundButton(self.ext_btns, "Open extension settings",
                                   self.open_extension_page, kind="warn", size=9, padx=12, pady=5)
        self.ext_install = RoundButton(self.ext_btns, "Install extension", self.install_extension,
                                       kind="secondary", size=9, padx=12, pady=5)
        self.ext_install.pack(side="left")

        self.locktab_var = tk.BooleanVar(value=bool(s["locktab"]))
        self.exact_var = tk.BooleanVar(value=bool(s["exact"]))
        Toggle(cl, "Lock my current Chrome tab", self.locktab_var,
               sub="Open your page in Chrome first").pack(anchor="w", pady=(px(8), 0))
        Toggle(cl, "Lock its exact address", self.exact_var,
               sub="Off: links within the same site are OK").pack(anchor="w", pady=(px(10), 0))

        hr = tk.Frame(cr, bg=BG)
        hr.pack(fill="x")
        section(hr, "Allowed apps").pack(side="left")
        RoundButton(hr, "Refresh", self.refresh_apps, kind="ghost", size=8,
                    padx=10, pady=3).pack(side="right", pady=(px(10), 0))
        self.app_list = CheckList(cr, colw, px(150))
        self.app_list.pack(fill="x")
        label(cr, "Other apps (exe names, comma separated)", 9, SUBTLE).pack(
            anchor="w", pady=(px(8), px(4)))
        self.extra_var = tk.StringVar(value=s["extra"])
        RoundEntry(cr, self.extra_var, colw, size=10).pack(fill="x")

        # Start
        bottom = tk.Frame(outer, bg=BG)
        bottom.pack(fill="x", pady=(px(22), 0))
        self.start_btn = RoundButton(bottom, "Start focus", self.start, size=12, pady=11)
        self.start_btn.pack(fill="x")
        msg = tk.Frame(outer, bg=BG)
        msg.pack(fill="x", pady=(px(10), 0))
        self.msg_lbl = label(msg, "", 10, DANGER, wraplength=W - px(150), justify="left")
        self.msg_lbl.pack(side="left", anchor="n")
        self.side_btns = tk.Frame(msg, bg=BG)
        self.side_btns.pack(side="right", anchor="n")
        self.cancel_btn = RoundButton(self.side_btns, "Cancel", self.cancel_start,
                                      kind="ghost", size=9, padx=12, pady=5)

        self.bind("<Return>", lambda e: self.start() if self.phase == "setup" else None)
        self.on_mode_change(resize=False)
        self.refresh_apps()
        self._status_token = object()
        self.update_ext_status(self._status_token)
        self.center_on_primary()

    def _breaks_changed(self):
        on = self.breaks_var.get() > 0
        self.break_len.set_enabled(on)
        self.break_of.configure(fg=MUTED if on else SUBTLE)

    def on_mode_change(self, resize=True):
        study = self.mode_var.get() == "study"
        if study:
            self.study_frame.pack(fill="x")
            self.mode_desc.configure(text="Only the apps you allow stay usable, and Chrome can "
                                          "be locked to one tab. Alt+Tab still works.")
        else:
            self.study_frame.pack_forget()
            self.mode_desc.configure(text="Black screen with the timer on every monitor. "
                                          "Nothing else is usable until it ends.")
        if resize:
            self.geometry("")
            self.update_idletasks()
            _x, py_, _w, ph = self.primary_monitor()
            if self.winfo_rooty() + self.winfo_reqheight() > py_ + ph - px(40):
                self.center_on_primary()

    def set_message(self, text, color=DANGER):
        self.msg_lbl.configure(text=text, fg=color)

    def selected_exes(self):
        return self.app_list.checked()

    def refresh_apps(self):
        ticked = set(self.app_list.checked()) if self.app_list.vars else set(self.settings["apps"])
        seen = {}
        for _h, _pid, exe, title in list_app_windows(
                exclude_pids={os.getpid()}, include_minimized=True):
            if exe and exe not in ALWAYS_ALLOWED:
                seen.setdefault(exe, title)
        for exe in ticked:                      # keep ticked apps visible so they can be un-ticked
            seen.setdefault(exe, "not running")
        items = [(exe, title, exe in ticked) for exe, title in sorted(seen.items())]
        self.app_list.set_items(items)

    # ---- extension status --------------------------------------------------
    def ext_state(self):
        """(colour, text, fixable) describing the Chrome extension connection."""
        if not self.port_ok and not self.tabsrv.start():
            return DANGER, "Port %d is in use by another program." % TAB_PORT, False
        self.port_ok = True
        if not self.tabsrv.connected():
            return SUBTLE, "Extension not connected. Open Chrome, or install it below.", False
        info = self.tabsrv.ext
        if self.ext_outdated():
            return WARN, ("Extension needs a reload: open its settings and click the "
                          "reload arrow on Focus Lock."), True
        if info.get("incognito") is False:
            return WARN, "Connected, but not allowed in Incognito.", True
        return GOOD, "Extension connected", False

    def ext_outdated(self):
        # Chrome keeps running the old code until the extension is reloaded,
        # even though the app has already rewritten its files.
        return (self.tabsrv.connected() and version_tuple(self.tabsrv.ext.get("version") or "1.0")
                < version_tuple(EXT_VERSION))

    def update_ext_status(self, token):
        if token is not self._status_token or self.phase not in ("setup", "starting"):
            return
        try:
            color, text, fixable = self.ext_state()
            d = px(10)
            self.ext_dot.delete("all")
            self.ext_dot.create_oval(1, 1, d - 1, d - 1, fill=color, outline="")
            self.ext_lbl.configure(text=text, fg=FG if color == GOOD else MUTED)
            if color == GOOD and "Incognito" in self.msg_lbl.cget("text"):
                self.set_message("")                # the user fixed it - drop the stale error
            if fixable and not self.ext_fix.winfo_manager():
                self.ext_fix.pack(side="left", padx=(0, px(8)), before=self.ext_install)
            elif not fixable and self.ext_fix.winfo_manager():
                self.ext_fix.pack_forget()
        except tk.TclError:
            return
        self.after(1000, self.update_ext_status, token)

    def open_extension_page(self):
        ext_id = self.tabsrv.ext.get("id") or ""
        url = "chrome://extensions/?id=" + ext_id if ext_id else "chrome://extensions"
        chrome = find_chrome()
        if chrome:
            try:
                subprocess.Popen([chrome, url])
                return
            except OSError:
                pass
        from tkinter import messagebox
        messagebox.showinfo(APP_TITLE, "Open %s in Chrome, click Details on Focus Lock and "
                                       "switch on 'Allow in Incognito'." % url)

    def install_extension(self):
        from tkinter import messagebox
        try:
            folder = write_extension()
        except OSError as e:
            messagebox.showerror(APP_TITLE, "Couldn't write the extension files:\n%s" % e)
            return
        self.tabsrv.ext_version = EXT_VERSION
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
            "   (Already installed? Just click the reload arrow on Focus Lock.)\n"
            "3. Click 'Details' on Focus Lock and switch ON 'Allow in Incognito'. "
            "This is required - otherwise a private window would get around the lock.\n"
            "4. (Optional) Pin it from the puzzle-piece menu - it shows 'ON' "
            "while a session is running and 'BRK' during a break.\n\n"
            "If Chrome ever asks about 'local network access' for Focus Lock, "
            "click Allow.")

    # ---- starting a session ------------------------------------------------
    def start(self):
        if self.phase != "setup":
            return
        try:
            minutes = float(self.minutes_var.get())
            if not 0.1 <= minutes <= 600:
                raise ValueError
        except ValueError:
            self.set_message("Enter a number of minutes between 1 and 600.")
            return

        mode = self.mode_var.get()
        apps = self.selected_exes()
        extra = self.extra_var.get().strip()
        allowed, lock_tab = set(), False
        if mode == "study":
            allowed = set(apps) | parse_exes(extra)
            lock_tab = bool(self.locktab_var.get())
            if not allowed and not lock_tab:
                self.set_message("Study mode needs at least one allowed app, or a locked Chrome tab.")
                return

        self.settings = {"minutes": self.minutes_var.get().strip(), "mode": mode,
                         "locktab": bool(self.locktab_var.get()),
                         "exact": bool(self.exact_var.get()),
                         "apps": apps, "extra": extra,
                         "breaks": self.breaks_var.get(),
                         "break_minutes": self.break_min_var.get(),
                         "spotify": bool(self.spotify_var.get())}
        save_settings(self.settings)

        params = {"minutes": minutes, "mode": mode, "allowed": allowed, "lock_tab": lock_tab,
                  "breaks": self.breaks_var.get(), "break_minutes": self.break_min_var.get(),
                  "spotify": bool(self.spotify_var.get())}
        if not lock_tab:
            self.begin_session(params)
            return

        if not self.port_ok and not self.tabsrv.start():
            self.set_message("Couldn't open local port %d, so Chrome can't be reached. Is "
                             "another program using it?" % TAB_PORT)
            return
        self.port_ok = True
        if self.tabsrv.connected() and self.tabsrv.ext.get("incognito") is False:
            self.set_message("Allow Focus Lock in Incognito first, otherwise a private window "
                             "gets around the tab lock. Use 'Open extension settings' above, "
                             "switch on 'Allow in Incognito', then press Start again.")
            return
        gen = self.new_phase("starting")
        self.tabsrv.begin(self.exact_var.get())
        self.start_btn.set_enabled(False)
        self.start_btn.set_text("Locking your Chrome tab")
        self.set_message("Waiting for Chrome...", MUTED)
        self.cancel_btn.pack(side="left", padx=(px(8), 0))
        self.after(100, self._await_tab_lock, gen, time.monotonic(), params, 0)

    def _await_tab_lock(self, gen, t0, params, n):
        """Polls (without blocking the UI) until the extension confirms the lock."""
        if gen != self.gen:
            return
        st = self.tabsrv.status
        if st.get("locked"):
            params["allowed"].add("chrome.exe")
            self.begin_session(params)
            return
        if st.get("error"):
            self._start_failed(st["error"])
            return
        if time.monotonic() - t0 > 10:
            if self.tabsrv.connected():
                why = ("Chrome didn't confirm the lock. Click the Focus Lock icon in Chrome "
                       "and press Start again.")
            else:
                why = ("Chrome didn't respond. Make sure Chrome is open and the Focus Lock "
                       "extension is installed (use 'Install extension').")
            self._start_failed(why)
            return
        self.start_btn.set_text("Locking your Chrome tab" + "." * (n // 4 % 4))
        self.after(100, self._await_tab_lock, gen, t0, params, n + 1)

    def _start_failed(self, text, color=DANGER):
        self.tabsrv.end()
        self.new_phase("setup")
        self.start_btn.set_text("Start focus")
        self.start_btn.set_enabled(True)
        self.cancel_btn.pack_forget()
        self.set_message(text, color)

    def cancel_start(self):
        if self.phase == "starting":
            self._start_failed("Cancelled.", color=MUTED)

    def begin_session(self, p):
        self.allowed = p["allowed"]
        self.tab_locked = p["lock_tab"]
        self.full_lock = p["mode"] == "full"
        self.total_seconds = max(1, int(p["minutes"] * 60))
        self.remaining = float(self.total_seconds)
        self.breaks_total = self.breaks_left = int(p["breaks"])
        self.break_secs = int(p["break_minutes"]) * 60
        self.session_focus = 0.0
        self.spotify_on = bool(p.get("spotify"))
        self.enter_focus()

    # ---- focus -------------------------------------------------------------
    def enter_focus(self):
        gen = self.new_phase("focus")
        self.release_window()
        self.clear()
        self.end_time = time.monotonic() + self.remaining
        self.seg_start = time.monotonic()
        self.tabsrv.paused = False      # the extension re-locks and returns to the study tab
        self._break_armed_until = 0.0

        if self.full_lock:
            self.enter_full_lock()
        else:
            self.enter_study_bar()

        self.blocker.block_alt_tab = self.full_lock
        self.blocker.lock_chrome = self.tab_locked
        self.blocker.start()

        self.tick(gen)
        if self.full_lock:
            self.keep_focus(gen)
        else:
            self.guard_tick(gen)
        if self.spotify_card is not None:
            self.spotify_tick(gen)

    def _break_button(self, parent, size, small):
        if not self.breaks_total:
            return None
        b = RoundButton(parent, "", self.on_break_click, kind="secondary", size=size,
                        padx=12 if small else 20, pady=5 if small else 9)
        self._reset_break_button(b, small)
        return b

    def _reset_break_button(self, b, small):
        if not b or not b.winfo_exists():
            return
        if self.breaks_left:
            text = ("Break (%d left)" if small else "Take a break  ·  %d left") % self.breaks_left
            b.set_text(text)
            b.set_enabled(True)
        else:
            b.set_text("No breaks left")
            b.set_enabled(False)

    def on_break_click(self):
        """First click arms, second click (within 4 s) starts the break."""
        if self.phase != "focus" or not self.breaks_left:
            return
        now = time.monotonic()
        if now < self._break_armed_until:
            self.take_break()
            return
        self._break_armed_until = now + 4
        mins = self.break_secs // 60
        self.break_btn.set_text("Click again for a %d-min break" % mins)
        gen, small = self.gen, not self.full_lock
        self.after(4000, lambda: gen == self.gen and self._reset_break_button(self.break_btn, small))

    def enter_full_lock(self):
        px_, py_, pw, ph = self.primary_monitor()
        self.resizable(True, True)
        self.geometry("+%d+%d" % (px_, py_))
        self.update_idletasks()
        self.wm_attr(self, "-fullscreen", True)
        self.wm_attr(self, "-topmost", True)
        self.bind("<Alt-F4>", lambda e: "break")
        self.bind("<Escape>", lambda e: "break")

        c = self.container
        center = tk.Frame(c, bg=BG)
        center.place(relx=0.5, rely=0.48, anchor="center")
        size = int(min(ph * (0.5 if self.spotify_on else 0.56), pw * 0.42))
        label(center, "F O C U S", 12, SUBTLE, "bold").pack(pady=(0, px(20)))
        self.ring = Ring(center, size, max(px(10), int(size * 0.035)), ticks=60,
                         sub_px=max(px(12), int(size * 0.042)))
        self.ring.pack()
        label(center, "Stay on this screen. You've got this.", 13, MUTED).pack(pady=(px(28), px(16)))
        self.actions = tk.Frame(center, bg=BG)       # buttons + Spotify, hidden by the quit prompt
        self.actions.pack()
        btns = tk.Frame(self.actions, bg=BG)
        btns.pack()
        self.break_btn = self._break_button(btns, 11, small=False)
        if self.break_btn:
            self.break_btn.pack(side="left", padx=(0, px(10)))
        RoundButton(btns, "End early", self.show_quit_prompt, kind="ghost", size=10,
                    hover_fg=DANGER, padx=18, pady=9).pack(side="left")
        if self.spotify_on:
            self.spotify_card = SpotifyCard(self.actions, self.spotify, px(300), px(108))
            self.spotify_card.pack(pady=(px(22), 0))
        self.exit_frame = tk.Frame(center, bg=BG)
        self.exit_frame.pack()
        self._show_actions = lambda: self.actions.pack(before=self.exit_frame)

        # black covers on every other monitor, with a quiet copy of the timer
        for (x, y, w, h) in get_monitors():
            if (x, y) == (px_, py_):
                continue
            top = tk.Toplevel(self)
            top.configure(bg="black")
            top.overrideredirect(True)
            top.geometry("%dx%d+%d+%d" % (w, h, x, y))
            top.attributes("-topmost", True)
            box = tk.Frame(top, bg="black")
            box.place(relx=0.5, rely=0.5, anchor="center")
            tk.Label(box, text="F O C U S", bg="black", fg=PANEL,
                     font=(FONT, 16, "bold")).pack()
            t = tk.Label(box, text="", bg="black", fg="#334155", font=(FONT, 40, "bold"))
            t.pack()
            self.cover_labels.append(t)
            top.bind("<Alt-F4>", lambda e: "break")
            self.covers.append(top)
        self.focus_force()

    def _bar(self, ring_color):
        """Compact top-of-screen window used for Study mode and breaks."""
        self.wm_attr(self, "-toolwindow", True)    # small title bar, no minimise button
        self.wm_attr(self, "-topmost", True)
        c = self.container
        c.configure(padx=px(14), pady=px(12))
        row = tk.Frame(c, bg=BG)
        row.pack(anchor="w")
        self.ring = Ring(row, px(86), px(6), color=ring_color, ticks=12, time_px=px(14))
        self.ring.pack(side="left")
        col = tk.Frame(row, bg=BG)
        col.pack(side="left", padx=(px(14), 0))
        self.bar_row = row
        self.exit_frame = tk.Frame(c, bg=BG)
        self.exit_frame.pack(anchor="w")
        return col

    def enter_study_bar(self):
        col = self._bar(ACCENT)
        label(col, "Study mode", 11, FG, "bold").pack(anchor="w")
        self.bar_status = label(col, "", 9, MUTED)
        self.bar_status.pack(anchor="w")
        self.actions = tk.Frame(col, bg=BG)
        self.actions.pack(anchor="w", pady=(px(8), 0))
        self.break_btn = self._break_button(self.actions, 9, small=True)
        if self.break_btn:
            self.break_btn.pack(side="left", padx=(0, px(6)))
        RoundButton(self.actions, "End early", self.show_quit_prompt, kind="ghost", size=9,
                    hover_fg=DANGER, padx=12, pady=5).pack(side="left")
        self._show_actions = lambda: self.actions.pack(anchor="w", pady=(px(8), 0))
        if self.spotify_on:
            self.spotify_card = SpotifyCard(self.bar_row, self.spotify, px(236), px(108))
            self.spotify_card.pack(side="left", padx=(px(18), 0))
        self.update_bar_status()
        self.place_top_center()

    def update_bar_status(self):
        if self.tab_locked:
            if self.ext_outdated():
                text, color = "●  Reload the Focus Lock extension", WARN
            elif time.time() - self.tabsrv.status_at < 6:
                text, color = "●  Chrome tab locked", GOOD
            else:
                text, color = "●  Waiting for Chrome...", WARN
        else:
            n = len(self.allowed)
            text, color = "●  %s allowed" % plural(n, "app"), MUTED
        try:
            self.bar_status.configure(text=text, fg=color)
        except tk.TclError:
            pass

    # ---- quitting early ----------------------------------------------------
    def show_quit_prompt(self):
        if self.quit_entry is not None:
            return
        self.actions.pack_forget()
        big = self.full_lock and self.phase == "focus"
        f = self.exit_frame
        label(f, 'To quit, type  "%s"  and press Enter' % QUIT_PHRASE,
              11 if big else 9, DANGER, wraplength=px(700 if big else 300),
              justify="center").pack(pady=(px(10), px(8)))
        self.quit_box = RoundEntry(f, tk.StringVar(), px(380 if big else 300),
                                   size=13 if big else 10, justify="center")
        self.quit_box.pack()
        self.quit_entry = self.quit_box.entry
        self.quit_entry.bind("<Return>", self.check_quit_phrase)
        self.quit_entry.bind("<Escape>", lambda e: (self.cancel_quit_prompt(), "break")[1])
        RoundButton(f, "Never mind", self.cancel_quit_prompt, kind="ghost",
                    size=9, padx=12, pady=4).pack(pady=(px(8), 0))
        self.quit_entry.focus_force()
        if not self.full_lock or self.phase == "break":
            self.geometry("")

    def cancel_quit_prompt(self):
        self.quit_entry = None
        for w in self.exit_frame.winfo_children():
            w.destroy()
        self._show_actions()
        if not self.full_lock or self.phase == "break":
            self.geometry("")

    def check_quit_phrase(self, _event=None):
        if self.quit_entry and self.quit_entry.get().strip().lower() == QUIT_PHRASE:
            self.finish(completed=False)
        elif self.quit_entry:
            self.quit_box._set(error=True)

    # ---- breaks ------------------------------------------------------------
    def take_break(self):
        if self.phase != "focus" or self.breaks_left <= 0:
            return
        self.credit_focus(stop=True)
        self.remaining = max(1.0, self.end_time - time.monotonic())
        self.breaks_left -= 1
        gen = self.new_phase("break")
        self.blocker.stop()
        self.tabsrv.paused = True           # extension stops enforcing until we resume
        self.release_window()
        self.clear()
        self.break_end = time.monotonic() + self.break_secs

        col = self._bar(WARN)
        label(col, "On a break", 11, WARN, "bold").pack(anchor="w")
        label(col, "Everything is unlocked. Focus resumes by itself.", 9, MUTED).pack(anchor="w")
        self.break_info = label(col, "", 9, SUBTLE)
        self.break_info.pack(anchor="w")
        self.actions = tk.Frame(col, bg=BG)
        self.actions.pack(anchor="w", pady=(px(8), 0))
        RoundButton(self.actions, "Resume now", self.resume_focus, kind="warn", size=9,
                    padx=12, pady=5).pack(side="left", padx=(0, px(6)))
        RoundButton(self.actions, "End session", self.show_quit_prompt, kind="ghost", size=9,
                    hover_fg=DANGER, padx=12, pady=5).pack(side="left")
        self._show_actions = lambda: self.actions.pack(anchor="w", pady=(px(8), 0))
        self.place_top_center()
        self.break_tick(gen)

    def break_tick(self, gen):
        if gen != self.gen:
            return
        left = self.break_end - time.monotonic()
        if left <= 0:
            self.resume_focus()
            return
        self.ring.set(left / self.break_secs, fmt_clock(left))
        info = "%s of focus left" % fmt_clock(self.remaining)
        if self.breaks_left:
            info += "  ·  %s after this" % plural(self.breaks_left, "break")
        if left <= 30:
            info = "Locking again in %d s" % math.ceil(left)
        try:
            self.break_info.configure(text=info, fg=WARN if left <= 30 else SUBTLE)
        except tk.TclError:
            pass
        self.after(200, self.break_tick, gen)

    def resume_focus(self):
        if self.phase != "break":
            return
        self.chime("resume")
        self.enter_focus()

    # ---- loops -------------------------------------------------------------
    def tick(self, gen):
        if gen != self.gen:
            return
        now = time.monotonic()
        remaining = self.end_time - now
        if remaining <= 0:
            self.finish(completed=True)
            return
        if now - self.seg_start >= 30:
            self.credit_focus()         # save progress regularly
        text = fmt_clock(remaining)
        if self.full_lock:
            ends = time.strftime("%H:%M", time.localtime(time.time() + remaining))
            self.ring.set(remaining / self.total_seconds, text, "ends at %s" % ends,
                          top="REMAINING")
            for lab in self.cover_labels:
                lab.configure(text=text)
        else:
            self.ring.set(remaining / self.total_seconds, text)
            self.update_bar_status()
        self.after(200, self.tick, gen)

    def spotify_tick(self, gen):
        if gen != self.gen or self.spotify_card is None:
            return
        try:
            self.spotify.poll()
            self.spotify_card.refresh()
        except tk.TclError:
            return
        self.after(1000, self.spotify_tick, gen)

    def keep_focus(self, gen):
        """Full lock: stay on top of everything, except Task Manager."""
        if gen != self.gen:
            return
        windows = [self] + self.covers
        if foreground_exe() == "taskmgr.exe":
            for w in windows:                      # step aside so you can end us
                self.wm_attr(w, "-topmost", False)
        else:
            for w in windows:
                self.wm_attr(w, "-topmost", True)
                w.lift()
            if self.focus_displayof() is None:
                self.focus_force()
                if self.quit_entry is not None and self.quit_entry.winfo_exists():
                    self.quit_entry.focus_force()
        self.after(250, self.keep_focus, gen)

    def guard_tick(self, gen):
        """Study mode: minimise anything that isn't allowed."""
        if gen != self.gen:
            return
        chrome_visible = False
        for hwnd, _pid, exe, _title in list_app_windows(exclude_pids={os.getpid()}):
            if exe == "chrome.exe":
                chrome_visible = True
            if not exe or exe in self.allowed or exe in ALWAYS_ALLOWED:
                continue
            minimize_window(hwnd)
        self.revive_chrome(chrome_visible)
        self.wm_attr(self, "-topmost", True)
        self.lift()
        self.after(300, self.guard_tick, gen)

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
    def finish(self, completed):
        if self.phase == "focus":
            self.credit_focus(stop=True)
        self.seg_start = None
        self.new_phase("done")
        self.blocker.stop()
        self.blocker.lock_chrome = False
        self.tabsrv.end()                 # extension unlocks Chrome within ~1 second
        self.tab_locked = False
        self.release_window()
        if completed:
            self.chime("done")
        self.show_finished(completed)

    def show_finished(self, completed):
        self.clear()
        outer = tk.Frame(self.container, bg=BG, padx=px(40), pady=px(34))
        outer.pack()
        tk.Frame(outer, width=px(420), height=1, bg=BG).pack()
        focused = min(self.session_focus, self.total_seconds)
        ring = Ring(outer, px(190), px(11), color=GOOD if completed else ACCENT, ticks=60,
                    time_px=px(36), sub_px=px(12))
        ring.set(1.0 if completed else focused / max(1, self.total_seconds),
                 fmt_clock(focused), "focused")
        ring.pack()
        if completed:
            label(outer, "Session complete", 22, GOOD, "bold").pack(pady=(px(18), px(4)))
            label(outer, "Nice work. Take a short break.", 11, MUTED).pack()
        else:
            label(outer, "Session ended early", 20, DANGER, "bold").pack(pady=(px(18), px(4)))
            label(outer, "No worries. Try again when you're ready.", 11, MUTED).pack()
        label(outer, "Today %s   ·   Streak %s" % (
            fmt_duration(self.stats.today()), plural(self.stats.streak(), "day")),
            10, SUBTLE).pack(pady=(px(14), px(22)))
        row = tk.Frame(outer, bg=BG)
        row.pack()
        RoundButton(row, "Another session", self.show_setup, size=11).pack(side="left", padx=px(5))
        RoundButton(row, "Close", self.on_close, kind="secondary", size=11).pack(side="left", padx=px(5))
        self.bind("<Return>", lambda e: self.show_setup() if self.phase == "done" else None)
        self.center_on_primary()


def main():
    if not IS_WINDOWS and not os.environ.get("FOCUSLOCK_DEV"):
        print("Focus Lock only runs on Windows.")
        sys.exit(1)
    if IS_WINDOWS:
        if not acquire_single_instance():
            if not activate_existing_instance():
                user32.MessageBoxW(None, "Focus Lock is already running.", APP_TITLE, 0x40)
            sys.exit(0)
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


if __name__ == "__main__":
    main()
