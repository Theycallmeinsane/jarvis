"""Media control through the Windows media session API (what the volume flyout's media controls use).

Unlike blind media keys, this knows which app is playing (Spotify, a browser tab, Windows Media Player...),
what's playing, and whether a command actually worked. Falls back to media keys if WinRT isn't available.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}
APP_NAMES = {"spotify": "spotify", "youtube": "chrome", "chrome": "chrome", "edge": "msedge", "firefox": "firefox",
             "vlc": "vlc", "media player": "zunemusic", "groove": "zunemusic", "apple music": "applemusic",
             "itunes": "itunes", "music": ""}
LAUNCH = {"spotify": "spotify:"}


@dataclass
class Session:
    app: str
    title: str
    artist: str
    status: str
    _raw: object = None

    @property
    def app_name(self) -> str:
        a = self.app.lower()
        for name, key in APP_NAMES.items():
            if key and key in a:
                return name.title() if name != "youtube" else "Chrome"
        return self.app.split("!")[0].split(".")[0] or "an app"

    def what(self) -> str:
        return f"{self.title} by {self.artist}" if self.artist else (self.title or "something")

    def describe(self) -> str:
        return f"{self.what()} on {self.app_name} ({self.status})"


def available() -> bool:
    try:
        import winrt.windows.media.control  # noqa: F401
        return True
    except ImportError:
        return False


async def _sessions_async() -> list[Session]:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

    mgr = await Manager.request_async()
    out = []
    for s in mgr.get_sessions():
        try:
            props = await s.try_get_media_properties_async()
            title, artist = props.title or "", props.artist or ""
        except Exception:  # noqa: BLE001
            title = artist = ""
        status = STATUS.get(int(s.get_playback_info().playback_status), "unknown")
        out.append(Session(s.source_app_user_model_id or "", title, artist, status, s))
    current = mgr.get_current_session()
    if current is not None:   # put Windows' "current" session first
        cid = current.source_app_user_model_id
        out.sort(key=lambda x: x.app != cid)
    return out


def _run(coro):
    return asyncio.run(coro)


def sessions() -> list[Session]:
    return _run(_sessions_async())


def _pick(all_sessions: list[Session], app: str | None) -> Session | None:
    if app:
        key = APP_NAMES.get(app.lower(), app.lower())
        if key:
            hits = [s for s in all_sessions if key in s.app.lower()]
            return hits[0] if hits else None
    playing = [s for s in all_sessions if s.status == "playing"]
    return (playing or all_sessions or [None])[0]


async def _command(session_raw, action: str) -> bool:
    call = {"play": session_raw.try_play_async, "pause": session_raw.try_pause_async,
            "toggle": session_raw.try_toggle_play_pause_async, "next": session_raw.try_skip_next_async,
            "previous": session_raw.try_skip_previous_async}[action]
    return bool(await call())


def _launch(app: str) -> Session | None:
    """Start the app (Spotify) and wait for its media session to appear."""
    target = LAUNCH.get(app.lower())
    if not target:
        return None
    os.startfile(target)
    for _ in range(30):            # up to ~15 s for a cold start
        time.sleep(0.5)
        s = _pick(sessions(), app)
        if s:
            return s
    return None


def _same(app_id: str) -> Session | None:
    return next((x for x in sessions() if x.app == app_id), None)


def control(action: str, app: str | None = None, default_app: str | None = None) -> tuple[bool, str]:
    """action: play | pause | toggle | next | previous | stop | status.
    Returns (success, a sentence that is true about what happened)."""
    action = {"stop": "pause", "resume": "play", "play_pause": "toggle", "skip": "next", "back": "previous"}.get(
        action, action)
    if not available():
        return _media_keys(action)

    all_sessions = sessions()
    s = _pick(all_sessions, app)
    if action == "status":
        if not all_sessions:
            return True, "Nothing is playing right now."
        return True, "Now: " + "; ".join(x.describe() for x in all_sessions[:3]) + "."
    if s is None and not app and action in ("play", "toggle") and default_app:
        app = default_app          # "play some music" with nothing open: start the usual player
        s = _pick(all_sessions, app)
    if s is None:
        if app and action in ("play", "toggle"):
            s = _launch(app)
            if s is None:
                return False, f"I opened {app.title()} but it didn't start playing; press play there once."
        else:
            where = f" in {app.title()}" if app else ""
            return False, f"Nothing is playing{where}, so there's nothing to {action}."
    if action == "play" and s.status == "playing":
        return True, f"{s.what()} is already playing on {s.app_name}."
    if action == "pause" and s.status in ("paused", "stopped"):
        return True, f"{s.app_name} is already paused."

    before = (s.title, s.status)
    ok = _run(_command(s._raw, action))
    # confirm it actually happened (apps update their state a moment later)
    for _ in range(8):
        time.sleep(0.25)
        now = _same(s.app)
        if now is None:
            continue
        if action in ("play",) and now.status == "playing":
            return True, f"Playing {now.title or 'music'} on {now.app_name}."
        if action == "pause" and now.status in ("paused", "stopped"):
            return True, f"Paused {now.app_name}."
        if action == "toggle" and now.status != before[1]:
            return True, f"{'Playing' if now.status == 'playing' else 'Paused'} {now.app_name}."
        if action in ("next", "previous") and now.title != before[0]:
            return True, f"Now playing {now.what()}."
    if ok:
        return True, f"I sent {action} to {s.app_name}, but couldn't confirm it changed."
    return False, f"{s.app_name} didn't accept {action}."


def _similar(a: str, b: str) -> bool:
    import difflib
    import re

    norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower())  # noqa: E731
    a, b = norm(a), norm(b)
    return bool(a and b) and (a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() > 0.7)


def find_spotify_track(query: str) -> tuple[str, str] | None:
    """(track id, title) for a song request like 'glimpse of us by joji', via a web search + Spotify oEmbed."""
    import re

    import requests
    from ddgs import DDGS

    with DDGS() as d:
        rows = d.text(f"{query} site:open.spotify.com/track", max_results=8)
    for r in rows:
        m = re.search(r"open\.spotify\.com/(?:intl-\w+/)?track/([A-Za-z0-9]{22})", r.get("href", ""))
        if not m:
            continue
        try:
            title = requests.get("https://open.spotify.com/oembed", timeout=8,
                                 params={"url": f"https://open.spotify.com/track/{m.group(1)}"}).json().get("title", "")
        except (requests.RequestException, ValueError):
            title = r.get("title", "").split(" - ")[0]
        song = re.split(r"\s+by\s+", query, flags=re.I)[0]
        if title and _similar(song, title):
            return m.group(1), title
    return None


def play_song(query: str) -> tuple[bool, str]:
    """Play a named song on Spotify, then check what's actually playing and say so honestly."""
    hit = find_spotify_track(query)
    if not hit:
        return False, f"I couldn't find {query} on Spotify."
    track_id, title = hit
    os.startfile(f"spotify:track:{track_id}")       # the desktop app opens the song's page
    for _ in range(12):
        time.sleep(0.5)
        s = _pick(sessions(), "spotify")
        if s and _similar(title, s.title) and s.status == "playing":
            return True, f"Playing {s.what()} on Spotify."
    s = _pick(sessions(), "spotify")
    if s and s.status != "playing":
        _run(_command(s._raw, "play"))
        time.sleep(1.0)
        s = _same(s.app)
        if s and _similar(title, s.title) and s.status == "playing":
            return True, f"Playing {s.what()} on Spotify."
    now = f" Spotify is playing {s.what()}." if s and s.status == "playing" else ""
    return False, (f"I've opened {title} in Spotify, but I can't start that exact song from here; "
                   f"press play on it.{now}")


def _media_keys(action: str) -> tuple[bool, str]:
    import ctypes

    vk = {"toggle": 0xB3, "play": 0xB3, "pause": 0xB3, "next": 0xB0, "previous": 0xB1}.get(action)
    if vk is None:
        return False, "I can't check what's playing on this PC."
    u = ctypes.windll.user32
    u.keybd_event(vk, 0, 1, 0)
    u.keybd_event(vk, 0, 3, 0)
    return True, f"I pressed the {action} media key (I can't confirm what happened on this PC)."
