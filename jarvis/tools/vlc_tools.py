"""Local movies and series in VLC, controlled by voice.

Jarvis starts its own VLC with the local web interface switched on (127.0.0.1 only, random password), so it
can read the real state (playing/paused, position, volume, tracks) and control it. Without VLC the file
opens in the default player and play/pause goes through media_control.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import requests

from .registry import Spoken, tool

log = logging.getLogger(__name__)

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".m2ts"}
JUNK = re.compile(r"\b(1080p|720p|480p|2160p|4k|uhd|hdr|x264|x265|h264|h265|hevc|bluray|blu ray|brrip|bdrip|webrip|"
                  r"web dl|webdl|hdrip|dvdrip|hdtv|aac|ac3|dts|ddp?5 1|10bit|yts|yify|rarbg|psa|extended|remastered|"
                  r"proper|repack|multi|dual audio|esub|subs?)\b")
EPISODE = re.compile(r"[sS](\d{1,2})[ ._-]?[eE](\d{1,3})|\b(\d{1,2})x(\d{2})\b")
FOLDER_NAMES = re.compile(r"^(movies?|films?|series|tv|tv shows|shows|anime|cinema|videos?)$", re.I)


def _fail(msg: str):
    from .builtin import ToolFailed

    raise ToolFailed(msg)


# ---------------------------------------------------------------- finding videos

def _clean(name: str) -> str:
    n = Path(name).stem.lower()
    n = re.sub(r"[\[\(\{].*?[\]\)\}]", " ", n)              # [YTS], (2019) ...
    n = re.sub(r"[._]+", " ", n)
    n = JUNK.sub(" ", n)
    n = re.sub(r"\b(19|20)\d\d\b", " ", n)                    # years
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def video_folders(cfg) -> list[Path]:
    m = (cfg.get("media") or {}) if cfg is not None else {}
    dirs = [Path(os.path.expanduser(d)) for d in (m.get("movie_dirs") or [])]
    home = Path.home()
    dirs += [home / "Videos", home / "Downloads"]
    if m.get("auto_find_movie_folders", True):          # D:\Movies, E:\Series ... on non-system drives
        system = Path(os.environ.get("SystemDrive", "C:") + "\\")
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(f"{letter}:\\")
            if root == system or not root.exists():
                continue
            try:
                for child in root.iterdir():
                    if child.is_dir() and FOLDER_NAMES.match(child.name):
                        dirs.append(child)
                        dirs += [g for g in child.iterdir() if g.is_dir() and FOLDER_NAMES.match(g.name)]
            except OSError:
                continue
    seen, out = set(), []
    for d in dirs:
        key = str(d).lower()
        if key not in seen and d.is_dir():
            seen.add(key)
            out.append(d)
    return out


_index: tuple[float, list[Path]] | None = None
_index_lock = threading.Lock()


def index_videos(cfg, max_seconds: float = 6.0, refresh: bool = False) -> list[Path]:
    """All video files in the video folders (cached for 10 minutes)."""
    global _index
    with _index_lock:
        if _index and not refresh and time.time() - _index[0] < 600:
            return _index[1]
        files: list[Path] = []
        deadline = time.monotonic() + max_seconds
        for root in video_folders(cfg):
            for dirpath, dirnames, names in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith((".", "$")) and d.lower() != "sample"]
                files += [Path(dirpath) / n for n in names if Path(n).suffix.lower() in VIDEO_EXT]
                if time.monotonic() > deadline:
                    break
        _index = (time.time(), files)
        return files


def _episode(text: str) -> tuple[int, int] | None:
    m = re.search(r"season (\d+)(?:,)? episode (\d+)", text) or re.search(r"\bs(\d+) ?e(\d+)\b", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _file_episode(name: str) -> tuple[int, int] | None:
    m = EPISODE.search(name)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(4)))


def find_video(query: str, files: list[Path]) -> tuple[Path | None, list[Path]]:
    """Best match for a spoken title ('the dark knight', 'friends season 2 episode 3'), plus close runners-up."""
    q = query.lower().strip()
    ep = _episode(q)
    q_title = _clean(re.sub(r"season \d+.*|\bs\d+ ?e\d+.*|\bepisode \d+.*", "", q))
    q_title = re.sub(r"^(the movie|movie|the film|film|the show|show|series)\s+", "", q_title)
    scored = []
    for f in files:
        name = _clean(f.name)
        # folder names help for series ("Friends/Season 2/03.mkv") and badly named movie files
        context = f"{_clean(f.parent.name)} {_clean(f.parent.parent.name)}"
        if ep:
            fe = _file_episode(f.name) or _file_episode(str(f.parent))
            if fe != ep:
                continue
        words = set(q_title.split())
        hay = set((name + " " + context).split())
        overlap = len(words & hay) / max(len(words), 1)
        ratio = difflib.SequenceMatcher(None, q_title, name).ratio()
        score = max(overlap * 0.9 + ratio * 0.1, ratio)
        if q_title and q_title in name:
            score = max(score, 0.95)
        scored.append((score, f))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored or scored[0][0] < 0.55:
        return None, [f for _, f in scored[:3]]
    return scored[0][1], [f for s, f in scored[1:4] if s >= 0.55]


# ---------------------------------------------------------------- VLC process + web interface

def vlc_path(cfg=None) -> str | None:
    custom = ((cfg.get("media") or {}) if cfg is not None else {}).get("vlc_path")
    candidates = [custom] if custom else []
    candidates += [r"C:\Program Files\VideoLAN\VLC\vlc.exe", r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"]
    try:
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (0, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\VideoLAN\VLC", 0, winreg.KEY_READ | view) as k:
                        candidates.append(winreg.QueryValueEx(k, None)[0])
                except OSError:
                    pass
    except ImportError:
        pass
    return next((c for c in candidates if c and Path(c).is_file()), None)


class VLC:
    """One VLC instance started by Jarvis, driven through its local HTTP interface."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.password = ""
        self.title = ""

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, exe: str, file: Path, fullscreen: bool, playlist: list[Path]):
        self.stop()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.password = secrets.token_urlsafe(12)
        args = [exe, "--no-one-instance", "--extraintf=http", "--http-host=127.0.0.1", f"--http-port={self.port}",
                f"--http-password={self.password}", "--no-qt-privacy-ask", "--play-and-exit"]
        if fullscreen:
            args.append("--fullscreen")
        args += [str(file)] + [str(p) for p in playlist]
        self.proc = subprocess.Popen(args, creationflags=0x08000000)
        for _ in range(40):                     # wait for the web interface
            time.sleep(0.25)
            try:
                st = self.status()
                if st.get("state") in ("playing", "paused"):
                    break
            except requests.RequestException:
                continue
        self.title = file.name

    def _get(self, **params) -> dict:
        r = requests.get(f"http://127.0.0.1:{self.port}/requests/status.json", params=params,
                         auth=("", self.password), timeout=3)
        r.raise_for_status()
        return r.json()

    def status(self) -> dict:
        return self._get()

    def command(self, command: str, val: str | None = None) -> dict:
        params = {"command": command}
        if val is not None:
            params["val"] = val
        self._get(**params)
        time.sleep(0.25)
        return self.status()

    def stop(self):
        if self.running:
            try:
                self.proc.terminate()
                self.proc.wait(5)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        self.proc = None


vlc = VLC()


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _now_title(st: dict) -> str:
    meta = (((st.get("information") or {}).get("category") or {}).get("meta") or {})
    return meta.get("title") or meta.get("filename") or vlc.title


def _streams(st: dict, kind: str) -> list[tuple[int, str]]:
    """[(track id, label)] for 'Audio' or 'Subtitle' streams in VLC's status."""
    out = []
    for key, info in ((st.get("information") or {}).get("category") or {}).items():
        m = re.match(r"Stream (\d+)", key)
        if m and isinstance(info, dict) and info.get("Type") == kind:
            label = info.get("Language") or info.get("Description") or f"track {m.group(1)}"
            out.append((int(m.group(1)), label))
    return sorted(out)


# ---------------------------------------------------------------- tools

@tool("Play a movie or episode from the PC's own video files (in VLC, fullscreen). Understands titles and "
      "'show season 2 episode 3'.",
      {"title": {"type": "string", "description": "Movie or show as the user said it"},
       "fullscreen": {"type": "boolean", "description": "default true"}},
      required=["title"], triggers=("play", "watch", "movie", "film", "episode", "season", "put on", "start", "show",
                                    "series", "video"))
def play_movie(title: str, fullscreen: bool = True, ctx=None):
    cfg = ctx.cfg if ctx else None
    files = index_videos(cfg)
    if not files:
        _fail("I didn't find any video files. Add your movie folders under media: movie_dirs in your settings.")
    best, others = find_video(title, files)
    if best is None:
        hint = f" Closest: {', '.join(_clean(f.name) for f in others[:2])}." if others else ""
        _fail(f"I couldn't find {title} in your videos.{hint}")
    exe = vlc_path(cfg)
    if not exe:
        os.startfile(str(best))     # default player; media_control handles play/pause
        return Spoken(f"Playing {_clean(best.name)} in your default player (VLC isn't installed).")
    # series: queue the following episodes from the same folder so "next episode" works
    playlist = []
    if _file_episode(best.name):
        siblings = sorted(p for p in best.parent.iterdir() if p.suffix.lower() in VIDEO_EXT)
        playlist = siblings[siblings.index(best) + 1:]
    vlc.start(exe, best, fullscreen if fullscreen is not None else True, playlist)
    try:
        st = vlc.status()
    except requests.RequestException:
        _fail(f"I started VLC with {best.name}, but it isn't answering me.")
    if st.get("state") != "playing":
        _fail(f"VLC opened {best.name} but it isn't playing.")
    return Spoken(f"Playing {_clean(best.name).title()}.")


VLC_ACTIONS = ["pause", "resume", "toggle", "stop", "close", "forward", "back", "seek_to", "volume", "next",
               "previous", "subtitles", "audio_track", "fullscreen", "status"]


@tool("Control the movie playing in VLC: pause, resume, stop, close VLC, skip forward/back N seconds, jump to "
      "a time, volume, next/previous episode, subtitles on/off/next, audio track (language), fullscreen, status.",
      {"action": {"type": "string", "enum": VLC_ACTIONS},
       "value": {"type": "string", "description": "forward/back: seconds (default 10); seek_to: '1:23:00' or "
                                                   "'50%'; volume: 0-200 or 'up'/'down'; subtitles: on/off/next"}},
      required=["action"], triggers=("movie", "film", "vlc", "episode", "pause", "resume", "skip", "forward", "back",
                                     "rewind", "subtitle", "caption", "audio", "language", "volume", "fullscreen",
                                     "full screen", "close", "stop", "where", "how long", "jump", "next", "previous"))
def vlc_control(action: str, value: str = ""):
    if not vlc.running:
        import psutil

        if action == "close":
            n = 0
            for p in psutil.process_iter(["name"]):
                if (p.info["name"] or "").lower() == "vlc.exe":
                    p.terminate()
                    n += 1
            return Spoken("Closed VLC." if n else "VLC isn't open.")
        _fail("No movie is playing in VLC right now. Say 'play' and a movie name.")
    try:
        if action == "close":
            vlc.stop()
            return Spoken("Closed the movie.")
        if action in ("pause", "resume", "toggle"):
            cmd = {"pause": "pl_forcepause", "resume": "pl_forceresume", "toggle": "pl_pause"}[action]
            st = vlc.command(cmd)
            return Spoken(f"{'Paused' if st.get('state') == 'paused' else 'Playing'} {_now_title(st)}.")
        if action == "stop":
            vlc.command("pl_stop")
            return Spoken("Stopped the movie.")
        if action in ("forward", "back"):
            secs = int(re.sub(r"\D", "", value) or 10)
            if "min" in value:
                secs *= 60
            st = vlc.command("seek", f"{'+' if action == 'forward' else '-'}{secs}")
            return Spoken(f"{'Skipped forward' if action == 'forward' else 'Went back'} {secs} seconds, now at "
                          f"{_clock(st.get('time', 0))}.")
        if action == "seek_to":
            v = value.strip()
            if v.endswith("%"):
                target = v
            else:
                parts = [int(x) for x in re.findall(r"\d+", v)] or [0]
                secs = 0
                for x in parts:
                    secs = secs * 60 + x
                target = str(secs)
            st = vlc.command("seek", target)
            return Spoken(f"Jumped to {_clock(st.get('time', 0))} of {_clock(st.get('length', 0))}.")
        if action == "volume":
            v = value.strip().lower()
            if v in ("up", "louder"):
                val = "+32"
            elif v in ("down", "quieter"):
                val = "-32"
            else:
                pct = int(re.sub(r"\D", "", v) or 100)
                val = str(round(max(0, min(200, pct)) * 2.56))     # VLC: 256 = 100%
            st = vlc.command("volume", val)
            return Spoken(f"Movie volume {round(st.get('volume', 0) / 2.56)}%.")
        if action in ("next", "previous"):
            st = vlc.command("pl_next" if action == "next" else "pl_previous")
            time.sleep(0.5)
            st = vlc.status()
            return Spoken(f"Now playing {_now_title(st)}.")
        if action == "fullscreen":
            st = vlc.command("fullscreen")
            return Spoken("Fullscreen on." if st.get("fullscreen") else "Fullscreen off.")
        if action == "subtitles":
            st = vlc.status()
            subs = _streams(st, "Subtitle")
            if value.lower() in ("off", "disable", "hide", "no"):
                vlc.command("subtitle_track", "-1")
                return Spoken("Subtitles off.")
            if not subs:
                _fail("This movie has no subtitle tracks.")
            current = st.get("subtitle_track") if isinstance(st.get("subtitle_track"), int) else None
            ids = [i for i, _ in subs]
            nxt = ids[(ids.index(current) + 1) % len(ids)] if current in ids else ids[0]
            vlc.command("subtitle_track", str(nxt))
            return Spoken(f"Subtitles: {dict(subs)[nxt]}.")
        if action == "audio_track":
            st = vlc.status()
            tracks = _streams(st, "Audio")
            if len(tracks) < 2:
                _fail("This movie has only one audio track.")
            want = value.lower()
            pick = next((i for i, lab in tracks if want and want in lab.lower()), None)
            if pick is None:
                ids = [i for i, _ in tracks]
                cur = st.get("audio_track") if isinstance(st.get("audio_track"), int) else ids[0]
                pick = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
            vlc.command("audio_track", str(pick))
            return Spoken(f"Audio: {dict(tracks)[pick]}.")
        st = vlc.status()      # status
        return Spoken(f"{_now_title(st)} is {st.get('state')}, at {_clock(st.get('time', 0))} of "
                      f"{_clock(st.get('length', 0))}, volume {round(st.get('volume', 0) / 2.56)}%.")
    except requests.RequestException:
        _fail("VLC stopped answering; it may have been closed.")


@tool("List or search the movies and shows on this PC.",
      {"query": {"type": "string", "description": "Optional: part of a title"}}, required=[],
      triggers=("movie", "film", "show", "series", "video", "watch", "have", "list"))
def list_movies(query: str = "", ctx=None):
    files = index_videos(ctx.cfg if ctx else None, refresh=True)
    if not files:
        return Spoken("I didn't find any video files in your video folders.")
    if query:
        best, others = find_video(query, files)
        found = [f for f in [best, *others] if f]
        if not found:
            return Spoken(f"No videos matching {query}.")
        return Spoken("I found: " + "; ".join(_clean(f.name).title() for f in found[:5]) + ".")
    # group episodes under their show folder so a series counts once
    titles = sorted({_clean(f.parent.name if _file_episode(f.name) else f.name).title() for f in files})
    sample = ", ".join(titles[:8])
    return Spoken(f"You have {len(files)} videos, about {len(titles)} titles, for example {sample}.")


# ---------------------------------------------------------------- fast path

def route_vlc(t: str, has) -> list[tuple[str, dict]] | None:
    playing = vlc.running
    if has("play_movie"):
        m = re.fullmatch(r"(?:play|watch|put on|start)(?: the)? (?:movie|film|show|episode|series) (?P<t>.+)|"
                         r"(?:i want to |let's |lets )?watch (?P<t2>.+)", t)
        if m:
            title = (m.group("t") or m.group("t2")).strip()
            title = re.sub(r"\s+(?:on|in) vlc$", "", title)
            if title and title not in ("a movie", "something", "tv", "youtube", "a video"):
                return [("play_movie", {"title": title})]
        m = re.fullmatch(r"play (?P<t>.+?) (?:on|in) vlc", t)
        if m:
            return [("play_movie", {"title": m.group("t")})]
    if not (has("vlc_control") and playing):
        if has("vlc_control") and re.fullmatch(r"close vlc|quit vlc|exit vlc", t):
            return [("vlc_control", {"action": "close"})]
        return None
    # these only apply while a movie Jarvis started is open
    rules = [
        (r"(?:pause|stop)(?: the)?(?: movie| film| video| it)?", ("pause", "")),
        (r"(?:resume|continue|unpause|play)(?: the)?(?: movie| film| video| it)?", ("resume", "")),
        (r"(?:close|exit|quit)(?: the)? (?:movie|film|vlc|video)", ("close", "")),
        (r"(?:full ?screen|go full ?screen|exit full ?screen)", ("fullscreen", "")),
        (r"(?:turn )?(?:on |show )?(?:the )?subtitles?(?: on)?|(?:turn on|enable) (?:the )?(?:subtitles?|captions)",
         ("subtitles", "on")),
        (r"(?:turn )?(?:off |hide )?(?:the )?subtitles? off|(?:turn off|disable|hide) (?:the )?(?:subtitles?|captions)",
         ("subtitles", "off")),
        (r"(?:next|change|switch)(?: the)? (?:audio|language)(?: track)?", ("audio_track", "")),
        (r"(?:next|skip to the next) episode", ("next", "")),
        (r"previous episode|last episode", ("previous", "")),
        (r"(?:how long is left|where am i in the movie|what time is it in the movie|movie status)", ("status", "")),
    ]
    for pattern, (action, value) in rules:
        if re.fullmatch(pattern, t):
            return [("vlc_control", {"action": action, "value": value})]
    m = re.fullmatch(r"(?:skip|go|jump|fast)(?: ahead| forward)?(?: by)? (?P<n>\d+) ?(?P<u>seconds?|secs?|minutes?|mins?)"
                     r"(?: forward| ahead)?", t)
    if m:
        return [("vlc_control", {"action": "forward", "value": m.group("n") + (" min" if m.group("u").startswith("m")
                                                                             else "")})]
    m = re.fullmatch(r"(?:rewind|go back|skip back|back)(?: by)? (?P<n>\d+) ?(?P<u>seconds?|secs?|minutes?|mins?)", t)
    if m:
        return [("vlc_control", {"action": "back", "value": m.group("n") + (" min" if m.group("u").startswith("m")
                                                                          else "")})]
    m = re.fullmatch(r"(?:movie |vlc )?volume(?: to)? (\d{1,3})(?: ?%| percent)?", t)
    if m and ("movie" in t or "vlc" in t):
        return [("vlc_control", {"action": "volume", "value": m.group(1)})]
    m = re.fullmatch(r"(?:jump|go|skip) to (?P<v>[\d: ]+(?:%| percent)?)", t)
    if m:
        return [("vlc_control", {"action": "seek_to", "value": m.group("v").replace(" percent", "%")})]
    return None
