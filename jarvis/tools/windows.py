"""Windows control: windows, desktops, shortcuts, Settings pages, folders, processes, dark mode, Wi-Fi."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import difflib
import os
import re
import subprocess
import time

from .registry import tool

NO_WINDOW = 0x08000000
user32 = ctypes.windll.user32 if os.name == "nt" else None

# ---------------------------------------------------------------- keyboard

VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
    "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "prtsc": 0x2C, "capslock": 0x14, "menu": 0x5D,
    "volumeup": 0xAF, "volumedown": 0xAE, "mute": 0xAD, "playpause": 0xB3,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
    ".": 0xBE, ",": 0xBC, "-": 0xBD, "=": 0xBB, ";": 0xBA, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC, "'": 0xDE,
}
EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x5B, 0x5D}


def parse_keys(combo: str) -> list[int]:
    """'ctrl+shift+esc' -> virtual key codes, modifiers first."""
    codes = []
    for part in re.split(r"\s*\+\s*", combo.strip().lower()):
        part = part.replace(" ", "")
        if part in VK:
            codes.append(VK[part])
        elif len(part) == 1 and part.isalnum():
            codes.append(ord(part.upper()))
        else:
            raise ValueError(f"unknown key {part!r}")
    return codes


def send_keys(combo: str):
    codes = parse_keys(combo)
    for vk in codes:
        user32.keybd_event(vk, 0, 1 if vk in EXTENDED else 0, 0)
        time.sleep(0.01)
    for vk in reversed(codes):
        user32.keybd_event(vk, 0, (1 if vk in EXTENDED else 0) | 2, 0)
        time.sleep(0.01)


@tool("Press a keyboard shortcut in the focused window, e.g. 'ctrl+c', 'alt+tab', 'win+shift+s', 'f5'.",
      {"keys": {"type": "string", "description": "Keys joined with +"}},
      triggers=("press", "hit", "shortcut", "key", "copy", "paste", "undo", "redo", "save", "refresh", "select all"))
def press_keys(keys: str):
    send_keys(keys)
    return f"Pressed {keys}."


DESKTOP_KEYS = {
    "show_desktop": "win+d", "task_view": "win+tab", "next_desktop": "win+ctrl+right",
    "previous_desktop": "win+ctrl+left", "new_desktop": "win+ctrl+d", "close_desktop": "win+ctrl+f4",
    "notifications": "win+n", "quick_settings": "win+a", "emoji_panel": "win+.", "snip": "win+shift+s",
    "start_menu": "win", "file_explorer": "win+e", "run_dialog": "win+r", "task_manager": "ctrl+shift+esc",
    "clipboard_history": "win+v", "minimize_all": "win+m", "restore_all": "win+shift+m",
}


@tool("Desktop actions: show the desktop, task view, switch/create/close virtual desktops, open notifications, "
      "quick settings, emoji panel, snipping tool, Start menu, File Explorer, Run, Task Manager, clipboard history.",
      {"action": {"type": "string", "enum": list(DESKTOP_KEYS)}},
      triggers=("desktop", "task view", "notification", "quick settings", "emoji", "snip", "start menu",
                "explorer", "run", "task manager", "clipboard", "minimize", "restore", "show"))
def desktop_action(action: str):
    send_keys(DESKTOP_KEYS[action])
    return f"Done: {action.replace('_', ' ')}."


# ---------------------------------------------------------------- windows

def _visible_windows() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def enum(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):   # 4 = GW_OWNER: skip owned popups
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value.strip()
                if title and title not in ("Program Manager", "Windows Input Experience"):
                    found.append((hwnd, title))
        return True

    user32.EnumWindows(enum, 0)
    return found


def _process_name(hwnd: int) -> str:
    import psutil

    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name().lower().removesuffix(".exe")
    except Exception:
        return ""


def find_window(name: str) -> tuple[int, str] | None:
    """Match by title or process name ('chrome', 'spotify', 'visual studio code')."""
    q = name.lower().strip()
    wins = _visible_windows()
    for hwnd, title in wins:
        if q in title.lower():
            return hwnd, title
    for hwnd, title in wins:
        if q.replace(" ", "") in _process_name(hwnd).replace(" ", ""):
            return hwnd, title
    titles = [t.lower() for _, t in wins]
    close = difflib.get_close_matches(q, titles, n=1, cutoff=0.5)
    return next(((h, t) for h, t in wins if t.lower() == close[0]), None) if close else None


def _focus(hwnd: int):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                    # SW_RESTORE
    # Windows only lets the foreground app hand over focus; a synthetic Alt press unlocks it
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


@tool("List the open windows (titles).", triggers=("window", "open", "running", "what's"))
def list_windows():
    wins = _visible_windows()
    return "\n".join(t for _, t in wins[:25]) if wins else "No windows open."


WINDOW_ACTIONS = ["focus", "minimize", "maximize", "restore", "snap_left", "snap_right"]


@tool("Control an open window by its title or app name: focus (switch to it), minimize, maximize, restore, "
      "snap_left, snap_right. (To close a window use close_window.)",
      {"name": {"type": "string", "description": "Window title or app name, e.g. 'chrome', 'notepad'"},
       "action": {"type": "string", "enum": WINDOW_ACTIONS}},
      triggers=("switch", "focus", "go to", "bring", "minimi", "maximi", "restore", "snap", "window",
                "left", "right", "full screen", "fullscreen"))
def window_action(name: str, action: str):
    hit = find_window(name)
    if not hit:
        return f"No open window matches {name}."
    hwnd, title = hit
    if action == "focus":
        _focus(hwnd)
    elif action in ("minimize", "maximize", "restore"):
        user32.ShowWindow(hwnd, {"minimize": 6, "maximize": 3, "restore": 9}[action])
    elif action in ("snap_left", "snap_right"):
        _focus(hwnd)
        time.sleep(0.15)
        send_keys("win+left" if action == "snap_left" else "win+right")
    return f"{action.replace('_', ' ').capitalize()}: {title}."


def _close_question(args: dict) -> str:
    hit = find_window(args.get("name", ""))
    return f"Close the window '{hit[1]}'? Anything unsaved in it may be lost." if hit else \
        f"Close {args.get('name')}?"


@tool("Close one open window by its title or app name (asks first).",
      {"name": {"type": "string", "description": "Window title or app name"}},
      dangerous=True, confirm=_close_question, triggers=("close", "shut", "get rid", "exit", "quit"))
def close_window(name: str):
    hit = find_window(name)
    if not hit:
        return f"No open window matches {name}."
    hwnd, title = hit
    user32.PostMessageW(hwnd, 0x0010, 0, 0)       # WM_CLOSE, same as clicking the X
    return f"Closed {title}."


# ---------------------------------------------------------------- settings, folders

SETTINGS = {
    "display": "display", "sound": "sound", "bluetooth": "bluetooth", "wifi": "network-wifi",
    "network": "network", "windows update": "windowsupdate", "apps": "appsfeatures", "storage": "storagesense",
    "power": "powersleep", "battery": "batterysaver", "notifications": "notifications",
    "personalization": "personalization", "background": "personalization-background",
    "colors": "colors", "themes": "themes", "default apps": "defaultapps", "mouse": "mousetouchpad",
    "keyboard": "typing", "privacy": "privacy", "microphone": "privacy-microphone", "camera": "privacy-webcam",
    "about": "about", "night light": "nightlight", "focus": "quiethours", "vpn": "network-vpn",
    "printers": "printers", "startup apps": "startupapps", "accounts": "yourinfo", "time": "dateandtime",
    "language": "regionlanguage", "gaming": "gaming-gamebar", "accessibility": "easeofaccess",
}


@tool("Open a Windows Settings page.",
      {"page": {"type": "string", "enum": list(SETTINGS)}},
      triggers=("setting", "open", "show", "go to", "change", "turn", "bluetooth", "wifi", "display", "sound",
                "update", "night light", "privacy", "startup"))
def open_settings(page: str):
    os.startfile(f"ms-settings:{SETTINGS.get(page.lower(), page)}")
    return f"Opened {page} settings."


FOLDERS = {
    "downloads": "shell:Downloads", "documents": "shell:Personal", "desktop": "shell:Desktop",
    "pictures": "shell:My Pictures", "music": "shell:My Music", "videos": "shell:My Video",
    "recycle bin": "shell:RecycleBinFolder", "this pc": "shell:MyComputerFolder", "startup": "shell:Startup",
    "screenshots": "shell:Screenshots", "appdata": "shell:AppData", "temp": "%TEMP%",
}


@tool("Open a common folder in File Explorer (Downloads, Documents, Desktop, Pictures, Recycle Bin, This PC...).",
      {"folder": {"type": "string", "enum": list(FOLDERS)}},
      triggers=("open", "show", "folder", "go to", "explorer", "recycle", "downloads", "documents"))
def open_folder(folder: str):
    target = os.path.expandvars(FOLDERS.get(folder.lower(), folder))
    subprocess.Popen(["explorer.exe", target])
    return f"Opened {folder}."


# ---------------------------------------------------------------- read aloud (local TTS)

@tool("Read text aloud word for word with the local voice: the clipboard, the text selected in the focused "
      "window, or given text. Use for 'read this to me', 'read my clipboard'.",
      {"source": {"type": "string", "enum": ["selection", "clipboard", "text"]},
       "text": {"type": "string", "description": "Only for source 'text'"}},
      required=["source"], triggers=("read", "say", "speak", "aloud", "out loud"))
def read_aloud(source: str, text: str = "", ctx=None):
    from .builtin import _ps

    if source == "selection":
        send_keys("ctrl+c")          # copy the selection, then read it from the clipboard
        time.sleep(0.25)
    if source in ("selection", "clipboard"):
        text = _ps("Get-Clipboard -Raw")
    text = (text or "").strip()
    if not text:
        return "There's nothing to read."
    limit = 4000
    clipped = text[:limit]
    speak = ctx.context.get("speak") if ctx else None
    if not speak:
        return clipped   # no voice attached (text mode): hand the text back
    speak(clipped)
    more = f" (first {limit} characters of {len(text)})" if len(text) > limit else ""
    return f"Reading it aloud now{more}. Don't repeat the text; just say you're reading it, or nothing."


# ---------------------------------------------------------------- processes, theme, wifi

@tool("Show which programs are using the most CPU or memory right now.",
      {"sort": {"type": "string", "enum": ["memory", "cpu"]},
       "count": {"type": "integer", "description": "default 5"}}, required=[])
def top_processes(sort: str = "memory", count: int = 5):
    import psutil

    procs = list(psutil.process_iter(["name", "memory_info"]))
    if sort == "cpu":
        for p in procs:
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
        time.sleep(0.5)
    rows: dict[str, float] = {}
    for p in procs:
        try:
            name = (p.info["name"] or "?").removesuffix(".exe")
            value = p.cpu_percent(None) / psutil.cpu_count() if sort == "cpu" else p.info["memory_info"].rss / 2**20
            rows[name] = rows.get(name, 0) + value          # group Chrome's many processes, etc.
        except (psutil.Error, AttributeError, TypeError):
            pass
    top = sorted(rows.items(), key=lambda kv: kv[1], reverse=True)[:count]
    unit = "% CPU" if sort == "cpu" else " MB"
    return "; ".join(f"{n} {v:.0f}{unit}" for n, v in top)


THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def _broadcast_theme_change():
    result = wt.DWORD()
    user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "ImmersiveColorSet", 0x0002, 1000, ctypes.byref(result))


@tool("Switch Windows between dark mode and light mode.",
      {"mode": {"type": "string", "enum": ["dark", "light", "toggle"]}},
      triggers=("dark", "light", "theme", "mode"))
def theme(mode: str):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, THEME_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        try:
            light_now = winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 1
        except FileNotFoundError:
            light_now = True
        light = {"dark": False, "light": True, "toggle": not light_now}[mode]
        for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1 if light else 0)
    _broadcast_theme_change()
    return f"{'Light' if light else 'Dark'} mode on."


def _netsh(*args) -> str:
    out = subprocess.run(["netsh", "wlan", *args], capture_output=True, text=True, timeout=20,
                         creationflags=NO_WINDOW)
    return (out.stdout or out.stderr).strip()


@tool("Wi-Fi: status, list saved networks, connect to a saved network, or disconnect.",
      {"action": {"type": "string", "enum": ["status", "saved", "connect", "disconnect"]},
       "network": {"type": "string", "description": "Saved network name for 'connect'"}},
      required=["action"], triggers=("wifi", "wi-fi", "wireless", "network", "internet", "connect"))
def wifi(action: str, network: str = ""):
    if action == "status":
        out = _netsh("show", "interfaces")
        if not out or "no wireless interface" in out.lower() or "wlansvc" in out.lower() \
                or "is not running" in out.lower():
            return "Wi-Fi isn't in use: this PC has no active Wi-Fi adapter, so it's probably on Ethernet."
        fields = dict(re.findall(r"^\s*(SSID|State|Signal)\s*:\s*(.+)$", out, re.M))
        return ", ".join(f"{k} {v}" for k, v in fields.items()) or out[:300]
    if action == "saved":
        names = re.findall(r"All User Profile\s*:\s*(.+)", _netsh("show", "profiles"))
        return ", ".join(n.strip() for n in names) or "No saved Wi-Fi networks."
    if action == "connect":
        if not network:
            return "Which network?"
        return _netsh("connect", f"name={network}")[:300]
    return _netsh("disconnect")[:300]
