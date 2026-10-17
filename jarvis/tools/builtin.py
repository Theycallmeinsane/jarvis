"""Built-in Windows workflows Jarvis can run. Add your own with the @tool decorator."""
from __future__ import annotations

import ctypes
import datetime as dt
import difflib
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from ..config import resolve_path
from .registry import Spoken, tool

log = logging.getLogger(__name__)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _ps(command: str, timeout: int = 20) -> str:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
    )
    return (out.stdout or out.stderr).strip()


# ---------------------------------------------------------------- time & info

@tool("Get the current local date and time.")
def get_datetime():
    now = dt.datetime.now()
    return Spoken("It's " + now.strftime("%I:%M %p on %A, %d %B %Y").lstrip("0") + ".")


@tool("Report computer health: CPU, memory, GPU, disk space and battery.")
def system_status():
    import psutil

    parts = [f"CPU {psutil.cpu_percent(interval=0.5):.0f}%"]
    vm = psutil.virtual_memory()
    parts.append(f"RAM {vm.percent:.0f}% used ({vm.used / 2**30:.1f} of {vm.total / 2**30:.0f} GB)")
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
            creationflags=NO_WINDOW).stdout.strip()
        if gpu:
            name, util, used, total, temp = [x.strip() for x in gpu.split(",")]
            parts.append(f"GPU {name} {util}% busy, {used}/{total} MB VRAM, {temp} C")
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    for part in psutil.disk_partitions():
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            parts.append(f"{part.mountpoint[:2]} {u.free / 2**30:.0f} GB free")
        except OSError:
            pass
    bat = psutil.sensors_battery()
    if bat:
        parts.append(f"battery {bat.percent:.0f}%{' charging' if bat.power_plugged else ''}")
    return "; ".join(parts)


@tool("Get current weather and today's forecast for a place.",
      {"location": {"type": "string", "description": "City name, e.g. 'Paris'. Empty for auto-detect."}},
      required=[], enabled=lambda cfg: cfg.tools.get("weather", True))
def get_weather(location: str = ""):
    r = requests.get(f"https://wttr.in/{urllib.parse.quote(location)}", params={"format": "j1"}, timeout=10)
    r.raise_for_status()
    data = r.json()
    cur = data["current_condition"][0]
    today = data["weather"][0]
    area = location or data.get("nearest_area", [{}])[0].get("areaName", [{}])[0].get("value", "here")
    return (f"{area}: {cur['weatherDesc'][0]['value']}, {cur['temp_C']} C (feels like {cur['FeelsLikeC']} C), "
            f"humidity {cur['humidity']}%. Today {today['mintempC']} to {today['maxtempC']} C.")


@tool("Look up a short factual summary about a person, place or thing on Wikipedia.",
      {"topic": {"type": "string"}})
def wikipedia_summary(topic: str):
    headers = {"User-Agent": "JarvisVoiceAssistant/0.1"}
    s = requests.get("https://en.wikipedia.org/w/api.php", headers=headers, timeout=10, params={
        "action": "opensearch", "search": topic, "limit": 1, "format": "json"}).json()
    if not s[1]:
        return f"No Wikipedia article found for {topic}."
    title = s[1][0]
    r = requests.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}",
                     headers=headers, timeout=10).json()
    return f"{title}: {r.get('extract', 'no summary available')}"


@tool("Look something up on the web and get text back to answer from (current events, prices, scores, "
      "release dates, anything after your training data). Doesn't open a browser.",
      {"query": {"type": "string"},
       "news": {"type": "boolean", "description": "true for latest news / headlines"}},
      required=["query"], enabled=lambda cfg: cfg.tools.get("web_lookup", True),
      # only for things that change: well-known facts ("capital of Japan") are answered directly
      triggers=("latest", "news", "today", "current", "now", "recent", "price", "cost", "score", "won", "winner",
                "release", "update", "search", "look up", "lookup", "google", "online", "internet", "web",
                "this week", "this year", "yesterday", "tonight", "tomorrow", "stock", "2025", "2026", "2027",
                "who is the", "what happened", "trending", "headline"))
def web_lookup(query: str, news: bool = False):
    from ddgs import DDGS

    with DDGS() as d:
        if news:
            rows = d.news(query, max_results=5)
            lines = [f"{r.get('date', '')[:10]} {r.get('source', '')}: {r['title']}. {r.get('body', '')[:200]}"
                     for r in rows]
        else:
            rows = d.text(query, max_results=5)
            lines = [f"{r['title']}: {r.get('body', '')[:300]}" for r in rows]
    return "\n".join(lines) or f"Nothing found for {query}."


# ---------------------------------------------------------------- apps & web

_start_apps: list[dict] | None = None

ALIASES = {
    "browser": "microsoft-edge:", "edge": "microsoft-edge:", "settings": "ms-settings:",
    "calculator": "calc.exe", "notepad": "notepad.exe", "file explorer": "explorer.exe",
    "explorer": "explorer.exe", "task manager": "taskmgr.exe", "terminal": "wt.exe",
    "command prompt": "cmd.exe", "paint": "mspaint.exe", "control panel": "control.exe",
}


def _apps() -> list[dict]:
    """Everything in the Start menu (desktop + Store apps), cached after first use."""
    global _start_apps
    if _start_apps is None:
        try:
            raw = _ps("Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress", timeout=30)
            data = json.loads(raw) if raw else []
            _start_apps = data if isinstance(data, list) else [data]
        except (ValueError, subprocess.SubprocessError):
            _start_apps = []
    return _start_apps


def _match_app(name: str) -> dict | None:
    apps = _apps()
    q = name.lower().strip()
    exact = [a for a in apps if a["Name"].lower() == q]
    if exact:
        return exact[0]
    contains = sorted((a for a in apps if q in a["Name"].lower()), key=lambda a: len(a["Name"]))
    if contains:
        return contains[0]
    close = difflib.get_close_matches(q, [a["Name"].lower() for a in apps], n=1, cutoff=0.6)
    return next((a for a in apps if a["Name"].lower() == close[0]), None) if close else None


@tool("Open / launch an application installed on this PC (e.g. Spotify, Chrome, VS Code, Steam, Settings).",
      {"name": {"type": "string", "description": "App name as the user said it"}},
      triggers=("open", "launch", "start", "run", "fire up", "bring up", "load", "show"))
def open_app(name: str):
    key = name.lower().strip()
    app = _match_app(name)
    if app:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['AppID']}"])
        return f"Opened {app['Name']}."
    if key in ALIASES:
        os.startfile(ALIASES[key])
        return f"Opened {name}."
    try:
        os.startfile(name)
        return f"Opened {name}."
    except OSError:
        return f"I couldn't find an app called {name}."


@tool("Close a running application by name (e.g. 'chrome', 'spotify').",
      {"name": {"type": "string"}}, dangerous=True, triggers=("close", "quit", "kill", "exit", "shut", "stop"))
def close_app(name: str):
    import psutil

    q = name.lower().replace(" ", "")
    killed = set()
    for p in psutil.process_iter(["name"]):
        pname = (p.info["name"] or "").lower()
        if q in pname.replace(" ", "") and pname not in ("python.exe", "pythonw.exe", "explorer.exe"):
            try:
                p.terminate()
                killed.add(p.info["name"])
            except psutil.Error:
                pass
    return f"Closed {', '.join(sorted(killed))}." if killed else f"No running app matched {name}."


@tool("Open a website in the default browser.", {"url": {"type": "string"}},
      triggers=("open", "go to", "visit", "website", "site", "pull up", "load", "show", "launch"))
def open_website(url: str):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.replace(" ", "")
    webbrowser.open(url)
    return f"Opened {url}."


@tool("Open a Google or YouTube search in the browser. ONLY when the user explicitly asks to search, google, "
      "or look something up in the browser. Never for stories, chat or questions you can answer yourself.",
      {"query": {"type": "string"},
       "site": {"type": "string", "enum": ["google", "youtube"], "description": "default google"}},
      triggers=("search", "google", "look up", "lookup", "youtube", "browse", "online", "video"))
def web_search(query: str, site: str = "google"):
    q = urllib.parse.quote_plus(query)
    url = (f"https://www.youtube.com/results?search_query={q}" if site == "youtube"
           else f"https://www.google.com/search?q={q}")
    webbrowser.open(url)
    return f"Searching {site} for {query}."


# ---------------------------------------------------------------- media & volume

def _key(vk: int):
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 1, 0)          # KEYEVENTF_EXTENDEDKEY
    user32.keybd_event(vk, 0, 1 | 2, 0)      # + KEYEVENTF_KEYUP


MEDIA_ACTIONS = ["play", "pause", "toggle", "next", "previous", "status"]


class ToolFailed(Exception):
    """A tool couldn't do what was asked; the message is said to the user as-is."""


@tool("Control music/video playback and check what's playing (Spotify, YouTube in a browser, any player). "
      "Starts Spotify if asked to play on it and it's closed. The result says what really happened.",
      {"action": {"type": "string", "enum": MEDIA_ACTIONS,
                  "description": "status = what's playing now; stop = pause"},
       "app": {"type": "string", "description": "Only if the user named one, e.g. 'spotify'"}},
      required=["action"],
      triggers=("play", "pause", "stop", "next", "skip", "previous", "resume", "music", "song", "track", "back",
                "playing", "spotify", "unpause", "continue", "put on", "listen", "hear", "playlist", "album",
                "tune", "media", "video", "youtube"))
def media_control(action: str, app: str = "", ctx=None):
    from .. import media

    default_app = ((ctx.cfg.get("media") or {}).get("default_app") if ctx else None) or None
    ok, message = media.control(action, app or None, default_app=default_app)
    if not ok:
        raise ToolFailed(message)
    return message


def _endpoint_volume():
    from pycaw.pycaw import AudioUtilities

    speakers = AudioUtilities.GetSpeakers()
    if hasattr(speakers, "EndpointVolume"):           # pycaw >= 2025
        return speakers.EndpointVolume
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume

    iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


@tool("Play a specific song (or 'song by artist') on Spotify. For just play/pause/next use media_control.",
      {"song": {"type": "string", "description": "e.g. 'Glimpse of Us by Joji'"}},
      triggers=("play", "put on", "listen", "song", "music", "spotify", "track", "hear"))
def play_song(song: str):
    from .. import media

    ok, message = media.play_song(song)
    if not ok:
        raise ToolFailed(message)
    return Spoken(message)


@tool("Volume of ONE app in the Windows volume mixer (Spotify, Chrome, Discord, VLC, a game...): get, set, "
      "up, down, mute, unmute. Use app='music' for whatever is playing. Reads the real level back.",
      {"app": {"type": "string", "description": "App name, or 'music' for the app that's playing"},
       "action": {"type": "string", "enum": ["get", "set", "up", "down", "mute", "unmute"]},
       "percent": {"type": "integer", "description": "0-100 for 'set'; step for up/down (default 15)"}},
      required=["app", "action"], triggers=("volume", "louder", "quieter", "mute", "loud", "quiet", "sound", "turn"))
def app_volume(app: str, action: str, percent: int | None = None, ctx=None):
    from .. import appvolume

    unduck = ctx.context.get("unduck") if ctx else None
    if unduck:
        unduck()          # read/set the real level, not the lowered one while Jarvis is talking
    ok, message = appvolume.control(app, action, percent)
    if not ok:
        raise ToolFailed(message)
    return Spoken(message)


@tool("Set, raise, lower, mute or unmute the overall SYSTEM volume (all apps). For one app use app_volume.",
      {"action": {"type": "string", "enum": ["set", "up", "down", "mute", "unmute", "get"]},
       "percent": {"type": "integer", "description": "0-100 for 'set'; step size for up/down (default 10)"}},
      required=["action"])
def volume(action: str, percent: int | None = None):
    import comtypes

    comtypes.CoInitialize()
    try:
        vol = _endpoint_volume()
        current = round(vol.GetMasterVolumeLevelScalar() * 100)
        if action == "get":
            return f"Volume is {current}%{' (muted)' if vol.GetMute() else ''}."
        if action in ("mute", "unmute"):
            vol.SetMute(1 if action == "mute" else 0, None)
            return f"{action.capitalize()}d."
        step = percent if percent is not None else 10
        target = {"set": step, "up": current + step, "down": current - step}[action]
        target = max(0, min(100, int(target)))
        vol.SetMute(0, None)
        vol.SetMasterVolumeLevelScalar(target / 100, None)
        return f"Volume set to {target}%."
    finally:
        comtypes.CoUninitialize()


# ---------------------------------------------------------------- timers & reminders

_timers: dict[int, tuple[threading.Timer, str, float]] = {}
_timer_ids = iter(range(1, 10_000))


@tool("Set a timer or reminder. Jarvis will speak the message when it goes off.",
      {"seconds": {"type": "integer", "description": "Delay in seconds (convert minutes/hours)"},
       "message": {"type": "string", "description": "What to say, e.g. 'Your tea is ready'"}},
      required=["seconds"])
def set_timer(seconds: int, message: str = "", ctx=None):
    tid = next(_timer_ids)
    text = message or "Your timer is done."
    notify = (ctx.context.get("notify") if ctx else None) or (lambda m: log.info("TIMER: %s", m))

    def fire():
        _timers.pop(tid, None)
        notify(f"Reminder: {text}")

    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    _timers[tid] = (t, text, time.time() + seconds)
    mins, secs = divmod(int(seconds), 60)
    when = f"{mins} minute{'s' * (mins != 1)}" + (f" {secs} seconds" if secs else "") if mins else f"{secs} seconds"
    return f"Timer {tid} set for {when}."


@tool("List active timers / reminders.")
def list_timers():
    if not _timers:
        return "No active timers."
    now = time.time()
    return "; ".join(f"#{i}: '{msg}' in {int(end - now)}s" for i, (_, msg, end) in _timers.items())


@tool("Cancel a timer by its number, or all timers.",
      {"timer_id": {"type": "integer", "description": "Omit to cancel all"}}, required=[])
def cancel_timer(timer_id: int | None = None):
    ids = [timer_id] if timer_id else list(_timers)
    for i in ids:
        entry = _timers.pop(i, None)
        if entry:
            entry[0].cancel()
    return f"Cancelled {len(ids)} timer(s)." if ids else "No timers to cancel."


# ---------------------------------------------------------------- notes, files, clipboard

def _notes_path(ctx) -> Path:
    return resolve_path(ctx.cfg.tools.get("notes_file", "data/notes.md"))


@tool("Save a note or to-do item for later.", {"text": {"type": "string"}})
def take_note(text: str, ctx=None):
    path = _notes_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- [{dt.datetime.now():%Y-%m-%d %H:%M}] {text}\n")
    return "Noted."


@tool("Read back the most recent notes.",
      {"count": {"type": "integer", "description": "How many (default 5)"}}, required=[])
def read_notes(count: int = 5, ctx=None):
    path = _notes_path(ctx)
    if not path.exists():
        return "You have no notes yet."
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return "\n".join(lines[-count:]) or "You have no notes yet."


@tool("Find files by name in Desktop, Documents and Downloads.",
      {"name": {"type": "string", "description": "Part of the file name"}})
def find_files(name: str, ctx=None):
    # spoken queries like "resume pdf" should match "Resume_Muhammad.pdf": every word must appear
    words = [w for w in re.split(r"[\s_.-]+", name.lower()) if w]
    norm = lambda f: re.sub(r"[\s_.-]+", " ", f.lower())  # noqa: E731
    hits: list[Path] = []
    deadline = time.monotonic() + 4    # big folders: give up rather than stall the conversation
    for d in ctx.cfg.tools.get("search_dirs", []):
        for dirpath, dirnames, filenames in os.walk(resolve_path(d)):
            dirnames[:] = [x for x in dirnames if not x.startswith(".") and x not in ("node_modules", ".venv",
                                                                                     "__pycache__")]
            hits += [Path(dirpath) / f for f in filenames if all(w in norm(f) for w in words)]
            if len(hits) >= 25 or time.monotonic() > deadline:
                break
    if not hits:
        return f"No files matching {name}."

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    hits.sort(key=mtime, reverse=True)
    return "\n".join(str(p) for p in hits[:10])


@tool("Open a file or folder with its default program.", {"path": {"type": "string"}})
def open_path(path: str):
    p = resolve_path(path)
    if not p.exists():
        return f"{p} doesn't exist."
    os.startfile(str(p))
    return f"Opened {p.name}."


@tool("Read the text currently on the clipboard.")
def read_clipboard():
    text = _ps("Get-Clipboard -Raw")
    return text[:2000] if text else "The clipboard is empty."


@tool("Copy text to the clipboard.", {"text": {"type": "string"}})
def copy_to_clipboard(text: str):
    # clip.exe only reads UTF-16 correctly when the byte-order mark is present
    subprocess.run(["clip"], input=b"\xff\xfe" + text.encode("utf-16-le"), check=True, creationflags=NO_WINDOW)
    return "Copied to clipboard."


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


@tool("Type text into the focused window. ONLY when the user explicitly says 'type ...' or 'dictate ...'. "
      "Never use this to reply to the user.", {"text": {"type": "string"}}, triggers=("type", "dictate", "write"))
def type_text(text: str):
    KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002
    time.sleep(0.3)
    raw = text.encode("utf-16-le")   # UTF-16 code units, so emoji go through as surrogate pairs
    for unit in (int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)):
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = _INPUT(type=1)
            inp.ki = _KEYBDINPUT(0, unit, flags, 0, None)
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    return "Typed it."


# ---------------------------------------------------------------- screen & power

@tool("Take a screenshot and save it to Pictures/Jarvis.", triggers=("screenshot", "screen shot", "capture", "snap"))
def take_screenshot(ctx=None):
    from PIL import ImageGrab

    folder = resolve_path(ctx.cfg.tools.get("screenshots_dir", "~/Pictures/Jarvis"))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
    ImageGrab.grab(all_screens=True).save(path)
    return f"Screenshot saved to {path}."


@tool("Look at the user's screen and answer a question about it (what's open, read an error, "
      "summarize a page, describe an image).",
      {"question": {"type": "string", "description": "What the user wants to know about the screen"}},
      triggers=("screen", "see", "look", "this", "read", "window", "error", "page", "display", "monitor",
                "what's open", "showing", "watch"),
      enabled=lambda cfg: (cfg.get("vision") or {}).get("enabled", True))
def look_at_screen(question: str, ctx=None):
    from ..vision import describe, grab_screen

    return describe(ctx.cfg, grab_screen(), question)


@tool("Lock the computer.", triggers=("lock",))
def lock_computer():
    ctypes.windll.user32.LockWorkStation()
    return "Locked."


@tool("Shut down, restart or put the computer to sleep.",
      {"action": {"type": "string", "enum": ["shutdown", "restart", "sleep", "cancel"]}}, dangerous=True,
      triggers=("shut", "restart", "reboot", "sleep", "power", "turn off", "cancel"))
def power(action: str):
    if action == "sleep":
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        return "Going to sleep."
    flag = {"shutdown": "/s", "restart": "/r", "cancel": "/a"}[action]
    subprocess.run(["shutdown", flag] + ([] if action == "cancel" else ["/t", "15"]), creationflags=NO_WINDOW)
    return "Cancelled the shutdown." if action == "cancel" else f"{action.capitalize()} in 15 seconds. Say 'cancel shutdown' to stop it."


@tool("Run a PowerShell command on this PC and return its output.",
      {"command": {"type": "string"}}, dangerous=True,
      enabled=lambda cfg: bool(cfg.tools.get("allow_shell", False)))
def run_powershell(command: str):
    out = _ps(command, timeout=60)
    return out[:3000] or "(no output)"


# ---------------------------------------------------------------- routines

def _routine_names(cfg) -> str:
    from ..routines import routines_from

    return ", ".join(routines_from(cfg)) or "none"


@tool(lambda cfg: "Run one of the user's routines (named multi-step workflows), then tell the user the "
      f"results briefly. Routines: {_routine_names(cfg)}.",
      {"name": {"type": "string", "description": "Routine name as the user said it"}},
      enabled=lambda cfg: bool(cfg.get("routines")))
def run_routine(name: str, ctx=None):
    from ..routines import find, run_steps

    hit = find(ctx.cfg, name)
    if not hit:
        return f"No routine called {name}. Available: {_routine_names(ctx.cfg)}."
    key, routine = hit
    results = run_steps(ctx, routine)
    intro = routine.get("say", "")
    if not routine.get("summarize", True):
        failed = [r for r in results if r.endswith("not available") or ": failed (" in r]
        note = f" Some steps didn't work: {'; '.join(failed)}." if failed else ""
        return Spoken((intro or f"{key.capitalize()} is done.") + note)
    return (f"Routine '{key}' results (summarize them as a short spoken briefing"
            + (f", starting with: {intro}" if intro else "") + "):\n" + "\n".join(results))


# ---------------------------------------------------------------- voice

def _voice_list(cfg=None) -> str:
    from ..tts import VOICE_PRESETS

    return ", ".join(f"{k} ({v['desc']})" for k, v in VOICE_PRESETS.items())


@tool(lambda cfg: f"Change Jarvis's speaking voice. Voices: {_voice_list()}. "
      "'female' and 'male' also work.",
      {"voice": {"type": "string", "description": "Voice name, or 'female' / 'male' / 'american' / 'hd'"}},
      triggers=("voice", "male", "female", "accent", "sound like", "speak like"))
def switch_voice(voice: str, ctx=None):
    switch = ctx.context.get("switch_voice") if ctx else None
    if not switch:
        return "Voice switching isn't available in this mode."
    return switch(voice)


# ---------------------------------------------------------------- brains

@tool("Switch Jarvis's brain / language model. 'local' = Ollama on this PC; 'claude' = Anthropic Claude; "
      "'lmstudio' = LM Studio server; 'openai_compat' = any OpenAI-compatible server.",
      {"provider": {"type": "string", "enum": ["local", "claude", "lmstudio", "openai_compat"]}})
def switch_model(provider: str, ctx=None):
    return ctx.context["switch_model"](provider)


@tool("Ask Claude (a more powerful cloud model) a hard question: deep reasoning, long explanations, "
      "coding, writing. Only use when the user asks for Claude or the question is beyond you.",
      {"question": {"type": "string", "description": "The full question with all needed context"}},
      enabled=lambda cfg: bool(cfg.tools.get("ask_claude", True)) and bool(
          os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")))
def ask_claude(question: str, ctx=None):
    return ctx.context["ask_claude"](question)
