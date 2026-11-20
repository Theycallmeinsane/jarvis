"""Spotify by voice through the Spotify Web API: search and play anything, the player, library and playlists.

Sign-in uses OAuth with PKCE, so only a Client ID is needed (no secret): add SPOTIFY_CLIENT_ID to .env and run
`jarvis.bat --spotify-login` once (or say "connect Spotify"). The token is kept in data/spotify_token.json and
refreshed automatically. Without it, Jarvis still plays, pauses and skips Spotify through the Windows media
session (jarvis/media.py), which needs no account setup at all.

Endpoints follow Spotify's February 2026 Web API changes for Development Mode apps: playlist entries live
under /playlists/{id}/items, saving to the library is PUT/DELETE /me/library?uris=..., new playlists are
POST /me/playlists and search returns at most 10 results per type.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import http.server
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"   # Spotify only accepts loopback IPs (not "localhost")
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public "
          "user-library-read user-library-modify")
KINDS = ("track", "artist", "album", "playlist")


class SpotifyError(Exception):
    """Something went wrong; the message is a sentence that can be said to the user."""


class NotConnected(SpotifyError):
    pass


class NoDevice(SpotifyError):
    pass


class PremiumRequired(SpotifyError):
    pass


# ---------------------------------------------------------------- settings

def _section(cfg) -> dict:
    return dict((cfg.get("spotify") or {}) if cfg is not None else {})


def client_id() -> str:
    return os.environ.get("SPOTIFY_CLIENT_ID", "").strip()


def redirect_uri() -> str:
    return os.environ.get("SPOTIFY_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT


def enabled(cfg) -> bool:
    """Offer the Spotify tools? 'auto' (default) = once SPOTIFY_CLIENT_ID is set."""
    flag = _section(cfg).get("enabled", "auto")
    if flag in (False, "false", "off", "no"):
        return False
    return flag in (True, "true", "on", "yes") or bool(client_id())


def token_path(cfg=None) -> Path:
    from .config import resolve_path

    return resolve_path(_section(cfg).get("token_file") or "data/spotify_token.json")


SETUP_HINT = ("Spotify isn't connected yet. Add your Spotify app's Client ID to the .env file as SPOTIFY_CLIENT_ID, "
              "then say 'connect Spotify'.")


# ---------------------------------------------------------------- sign-in (OAuth 2 with PKCE)

def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(challenge: str, state: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id(), "response_type": "code", "redirect_uri": redirect_uri(),
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state, "scope": SCOPES})


def _token_request(data: dict) -> dict:
    try:
        r = requests.post(TOKEN_URL, data=data, timeout=15,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    except requests.RequestException as e:
        raise SpotifyError("I couldn't reach Spotify to sign in. Check the internet connection.") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or "access_token" not in body:
        log.warning("spotify token request failed: %s %s", r.status_code, r.text[:300])
        if body.get("error") == "invalid_grant":
            raise NotConnected("Spotify signed me out. Say 'connect Spotify' to sign in again.")
        raise SpotifyError("Spotify refused the sign-in: " + (body.get("error_description") or body.get("error")
                                                             or f"error {r.status_code}") + ".")
    return body


def save_token(body: dict, path: Path, old: dict | None = None) -> dict:
    tok = {"access_token": body["access_token"],
           "refresh_token": body.get("refresh_token") or (old or {}).get("refresh_token"),
           "expires_at": time.time() + int(body.get("expires_in", 3600)) - 60,
           "scope": body.get("scope", (old or {}).get("scope", ""))}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tok, indent=1), encoding="utf-8")
    return tok


def wait_for_code(redirect: str, state: str, timeout: float = 180.0) -> str:
    """Listen on the redirect URI (127.0.0.1) until Spotify sends the browser back with ?code=..."""
    u = urllib.parse.urlparse(redirect)
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parts = urllib.parse.urlparse(self.path)
            if parts.path.rstrip("/") != (u.path or "/").rstrip("/"):
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(parts.query)
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in got and got.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Spotify is connected to Jarvis. You can close this tab." if ok else \
                "Spotify sign-in didn't complete (" + got.get("error", "unknown error") + "). You can close this tab."
            self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode())

        def log_message(self, *a):  # keep the console quiet
            pass

    try:
        server = http.server.HTTPServer((u.hostname or "127.0.0.1", u.port or 80), Handler)
    except OSError as e:
        raise SpotifyError(f"Port {u.port} is busy, so I can't finish the Spotify sign-in. Close whatever uses "
                           f"it, or pick another port in SPOTIFY_REDIRECT_URI (and in your Spotify app).") from e
    server.timeout = 1.0
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and not got:
            server.handle_request()
    finally:
        server.server_close()
    if not got:
        raise SpotifyError("The Spotify sign-in timed out. Say 'connect Spotify' to try again.")
    if got.get("state") != state:
        raise SpotifyError("The Spotify sign-in reply didn't match, so I ignored it. Please try again.")
    if "code" not in got:
        raise SpotifyError("Spotify sign-in was cancelled (" + got.get("error", "no code") + ").")
    return got["code"]


def login(cfg=None, open_browser=webbrowser.open, timeout: float = 180.0) -> str:
    """Open Spotify's consent page, catch the redirect on 127.0.0.1, store the token. Returns a sentence."""
    if not client_id():
        raise NotConnected(SETUP_HINT)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    open_browser(authorize_url(challenge, state))
    code = wait_for_code(redirect_uri(), state, timeout)
    body = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
                           "client_id": client_id(), "code_verifier": verifier})
    save_token(body, token_path(cfg))
    return "Spotify is connected. Try 'play Blinding Lights' or 'add this song to my liked songs'."


# ---------------------------------------------------------------- matching what was said

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def likeness(said: str, name: str) -> float:
    a, b = _norm(said), _norm(name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        ratio = max(ratio, 0.75 + 0.2 * min(len(a), len(b)) / max(len(a), len(b)))
    return ratio


def split_by(query: str) -> tuple[str, str]:
    """'glimpse of us by joji' -> ('glimpse of us', 'joji')."""
    parts = re.split(r"\s+by\s+", query.strip(), maxsplit=1, flags=re.I)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (query.strip(), "")


def describe(item: dict | None) -> str:
    if not item:
        return "something"
    name = item.get("name") or "something"
    artists = item.get("artists") or []
    if artists:
        return f"{name} by {', '.join(a.get('name', '') for a in artists[:2])}"
    show = (item.get("show") or {}).get("name")
    owner = (item.get("owner") or {}).get("display_name")
    return f"{name} from {show}" if show else (f"{name} by {owner}" if owner else name)


def parse_time(value: str) -> int | None:
    """'1:30' / '90' / '2 minutes' / '1:02:03' -> seconds."""
    v = (value or "").strip().lower()
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}", v):
        secs = 0
        for p in v.split(":"):
            secs = secs * 60 + int(p)
        return secs
    m = re.fullmatch(r"(\d+(?:\.\d+)?) ?(s|sec|secs|seconds?|m|min|mins|minutes?)?", v)
    if m:
        n = float(m.group(1))
        return int(n * 60 if (m.group(2) or "").startswith("m") else n)
    return None


def _clock(ms: int) -> str:
    s = int(ms or 0) // 1000
    return f"{s // 60}:{s % 60:02d}"


# ---------------------------------------------------------------- the client

class Spotify:
    """A thin Spotify Web API client. Every public method returns a sentence that's true about what happened,
    or raises SpotifyError with one."""

    def __init__(self, cfg=None, http=None, sleep=time.sleep, launch=None):
        self.cfg = cfg
        self.http = http or requests.Session()
        self.sleep = sleep
        self.launch = launch or _launch_app
        self._tok: dict | None = None

    # ---- auth plumbing
    def connected(self) -> bool:
        if not client_id():
            return False
        try:
            return bool(json.loads(token_path(self.cfg).read_text(encoding="utf-8")).get("refresh_token"))
        except (OSError, ValueError):
            return False

    def _token(self, force_refresh: bool = False) -> str:
        if not client_id():
            raise NotConnected(SETUP_HINT)
        path = token_path(self.cfg)
        if self._tok is None:
            try:
                self._tok = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise NotConnected("Spotify isn't signed in yet. Say 'connect Spotify' and approve it in the "
                                   "browser.") from None
        if force_refresh or time.time() >= float(self._tok.get("expires_at", 0)):
            if not self._tok.get("refresh_token"):
                raise NotConnected("Spotify signed me out. Say 'connect Spotify' to sign in again.")
            body = _token_request({"grant_type": "refresh_token", "refresh_token": self._tok["refresh_token"],
                                   "client_id": client_id()})
            self._tok = save_token(body, path, self._tok)
        return self._tok["access_token"]

    def call(self, method: str, path: str, params: dict | None = None, body=None, _retry: int = 1):
        try:
            r = self.http.request(method, API + path, params=params, json=body, timeout=10,
                                  headers={"Authorization": "Bearer " + self._token()})
        except requests.RequestException as e:
            raise SpotifyError("I couldn't reach Spotify. Check the internet connection.") from e
        if r.status_code == 401 and _retry:
            self._token(force_refresh=True)
            return self.call(method, path, params, body, _retry - 1)
        if r.status_code == 429 and _retry:
            self.sleep(min(float(r.headers.get("Retry-After", 1) or 1), 5))
            return self.call(method, path, params, body, _retry - 1)
        if r.status_code >= 400:
            try:
                err = r.json().get("error") or {}
            except ValueError:
                err = {}
            if isinstance(err, str):
                err = {"message": err}
            reason, message = err.get("reason", ""), err.get("message", "") or f"error {r.status_code}"
            log.info("spotify %s %s -> %s %s %s", method, path, r.status_code, reason, message)
            if reason == "NO_ACTIVE_DEVICE" or (r.status_code == 404 and "device" in message.lower()):
                raise NoDevice("Spotify isn't open on any device.")
            if reason == "PREMIUM_REQUIRED" or "premium" in message.lower():
                raise PremiumRequired("Controlling Spotify playback needs Spotify Premium.")
            if r.status_code == 403 and "restrict" in message.lower():
                raise SpotifyError("Spotify won't allow that right now (" + message.rstrip(".") + ").")
            raise SpotifyError(f"Spotify said: {message.rstrip('.')}.")
        if r.status_code == 204 or not r.content:
            return None
        try:
            return r.json()
        except ValueError:          # some player calls answer 200 with an empty or non-JSON body
            return None

    # ---- reading state
    def state(self) -> dict | None:
        return self.call("GET", "/me/player", {"additional_types": "track,episode"})

    def devices(self) -> list[dict]:
        return (self.call("GET", "/me/player/devices") or {}).get("devices") or []

    def _pick_device(self, devs: list[dict], name: str = "") -> dict | None:
        devs = [d for d in devs if not d.get("is_restricted")]
        if not devs:
            return None
        if name:
            best = max(devs, key=lambda d: likeness(name, d.get("name", "")))
            if likeness(name, best.get("name", "")) >= 0.6:
                return best
            kind = {"phone": "smartphone", "mobile": "smartphone", "computer": "computer", "pc": "computer",
                    "laptop": "computer", "speaker": "speaker", "tv": "tv", "tablet": "tablet"}
            for word, t in kind.items():
                if re.search(rf"\b{word}\b", name.lower()):
                    hit = [d for d in devs if (d.get("type") or "").lower() == t]
                    if hit:
                        return hit[0]
            return None
        active = [d for d in devs if d.get("is_active")]
        if active:
            return active[0]
        preferred = _section(self.cfg).get("default_device") or ""
        if preferred:
            hit = self._pick_device(devs, preferred)
            if hit:
                return hit
        host = socket.gethostname().lower()
        here = [d for d in devs if d.get("name", "").lower() == host] or \
               [d for d in devs if (d.get("type") or "").lower() == "computer"]
        return (here or devs)[0]

    def ensure_device(self) -> str:
        """The device to play on: the active one, else this PC (starting the Spotify app if it's closed)."""
        dev = self._pick_device(self.devices())
        if dev:
            return dev["id"]
        self.launch()
        for _ in range(20):                 # the desktop app takes a few seconds to show up as a device
            self.sleep(1.0)
            dev = self._pick_device(self.devices())
            if dev:
                return dev["id"]
        raise NoDevice("I started Spotify but it didn't show up as a device. Open it once and try again.")

    def _player(self, method: str, path: str, params: dict | None = None, body=None):
        """A player command that needs a device: if none is active, wake this PC's Spotify and retry there."""
        try:
            return self.call(method, path, params, body)
        except NoDevice:
            dev = self.ensure_device()
            return self.call(method, path, {**(params or {}), "device_id": dev}, body)

    def _now(self, wait: float = 0.7) -> dict | None:
        if wait:
            self.sleep(wait)
        return self.state()

    # ---- searching
    def search(self, query: str, kind: str, limit: int = 10) -> list[dict]:
        res = self.call("GET", "/search", {"q": query, "type": kind, "limit": min(limit, 10)}) or {}
        return [x for x in (res.get(kind + "s") or {}).get("items") or [] if x]

    def find_track(self, query: str) -> dict | None:
        song, artist = split_by(query)
        rows = self.search(f"{song} artist:{artist}", "track") if artist else []
        rows = rows or self.search(query, "track")
        if not rows:
            return None

        def score(i_t):
            i, t = i_t
            s = likeness(song, t.get("name", "")) - i * 0.02
            if artist:
                s += max((likeness(artist, a.get("name", "")) for a in t.get("artists") or []), default=0) * 0.5
            return s
        return max(enumerate(rows), key=score)[1]

    def find(self, query: str, kind: str) -> dict | None:
        if kind == "track":
            return self.find_track(query)
        if kind == "playlist":
            mine = self.my_playlist(query)
            if mine:
                return mine
        rows = self.search(query, kind)
        if not rows:
            return None
        best = max(enumerate(rows), key=lambda i_x: likeness(query, i_x[1].get("name", "")) - i_x[0] * 0.02)[1]
        return best

    def my_playlists(self) -> list[dict]:
        out, offset = [], 0
        while offset < 500:
            page = self.call("GET", "/me/playlists", {"limit": 50, "offset": offset}) or {}
            items = [p for p in page.get("items") or [] if p]
            out += items
            if not page.get("next") or not items:
                break
            offset += 50
        return out

    def my_playlist(self, name: str, threshold: float = 0.72) -> dict | None:
        name = re.sub(r"^(?:my|the)\s+|\s+playlist$", "", name.strip(), flags=re.I)
        lists = self.my_playlists()
        if not lists:
            return None
        best = max(lists, key=lambda p: likeness(name, p.get("name", "")))
        return best if likeness(name, best.get("name", "")) >= threshold else None

    # ---- playing things
    def play(self, query: str = "", kind: str = "auto") -> str:
        query = (query or "").strip()
        kind = (kind or "auto").lower().rstrip("s")
        if kind in ("liked", "liked song", "saved", "favorite", "favourite") or \
                re.fullmatch(r"(?:my )?(?:liked|saved|favou?rite) (?:songs|tracks|music)|my library", query.lower()):
            return self.play_liked()
        if not query:
            return self.resume()
        if kind == "auto":
            kind = self._guess_kind(query)
        if kind not in KINDS:
            kind = "track"
        item = self.find(query, kind)
        if not item:
            raise SpotifyError(f"I couldn't find {query} on Spotify.")
        if kind == "track":
            self._player("PUT", "/me/player/play", body={"uris": [item["uri"]]})
            return self._confirm_playing(item, f"Playing {describe(item)} on Spotify.")
        self._player("PUT", "/me/player/play", body={"context_uri": item["uri"]})
        label = {"artist": f"Playing {item.get('name')} on Spotify.",
                 "album": f"Playing the album {describe(item)}.",
                 "playlist": f"Playing the playlist {item.get('name')}."}[kind]
        return label

    def _guess_kind(self, query: str) -> str:
        """'play drake' -> the artist; 'play hotline bling' -> the song."""
        if split_by(query)[1]:
            return "track"
        artists = self.search(query, "artist", limit=3)
        if artists and likeness(query, artists[0].get("name", "")) >= 0.92:
            return "artist"
        return "track"

    def _confirm_playing(self, item: dict, said: str) -> str:
        st = self._now()
        now = (st or {}).get("item")
        if st and now and now.get("uri") != item.get("uri") and likeness(item.get("name", ""), now.get("name", "")) < 0.8:
            return f"I asked Spotify for {describe(item)}, but it's playing {describe(now)}."
        return said

    def play_liked(self) -> str:
        page = self.call("GET", "/me/tracks", {"limit": 50}) or {}
        uris = [(x.get("track") or x.get("item") or {}).get("uri") for x in page.get("items") or []]
        uris = [u for u in uris if u]
        if not uris:
            raise SpotifyError("Your Liked Songs is empty.")
        self._player("PUT", "/me/player/play", body={"uris": uris})
        return f"Playing your liked songs ({len(uris)} most recent)."

    def resume(self) -> str:
        st = self.state()
        if st and st.get("is_playing"):
            return f"{describe(st.get('item'))} is already playing."
        self._player("PUT", "/me/player/play")
        st = self._now()
        return f"Playing {describe((st or {}).get('item'))}." if st and st.get("item") else "Resumed Spotify."

    def pause(self) -> str:
        st = self.state()
        if not st or not st.get("is_playing"):
            return "Spotify is already paused."
        self.call("PUT", "/me/player/pause")
        return "Paused Spotify."

    def skip(self, direction: str = "next") -> str:
        before = ((self.state() or {}).get("item") or {}).get("uri")
        self._player("POST", "/me/player/" + ("previous" if direction == "previous" else "next"))
        for _ in range(4):
            st = self._now(0.5)
            item = (st or {}).get("item")
            if item and item.get("uri") != before:
                return f"Now playing {describe(item)}."
        return "Skipped." if direction == "next" else "Went back a song."

    def now_playing(self) -> str:
        st = self.state()
        if not st or not st.get("item"):
            return "Nothing is playing on Spotify."
        item = st["item"]
        where = (st.get("device") or {}).get("name")
        verb = "Playing" if st.get("is_playing") else "Paused on"
        pos = f", {_clock(st.get('progress_ms'))} of {_clock(item.get('duration_ms'))}" if item.get("duration_ms") else ""
        return f"{verb} {describe(item)}" + (f" on {where}" if where else "") + pos + "."

    # ---- player settings
    def volume(self, value: str) -> str:
        st = self.state()
        dev = (st or {}).get("device") or {}
        current = dev.get("volume_percent")
        v = str(value or "").strip().lower().rstrip("%").replace("percent", "").strip()
        if v in ("", "get", "status"):
            if current is None:
                raise SpotifyError("I can't read Spotify's volume right now.")
            return f"Spotify's volume is {current}%."
        if not dev.get("supports_volume", True):
            raise SpotifyError(f"Spotify can't change the volume on {dev.get('name', 'that device')}.")
        step = 15
        m = re.fullmatch(r"(up|down)(?: by)? ?(\d{1,3})?", v)
        if m:
            target = (current or 50) + (1 if m.group(1) == "up" else -1) * int(m.group(2) or step)
        elif v in ("max", "maximum", "full"):
            target = 100
        elif v in ("mute", "off"):
            target = 0
        elif re.fullmatch(r"\d{1,3}", v):
            target = int(v)
        else:
            raise SpotifyError(f"I didn't understand the volume {value}.")
        target = max(0, min(100, int(target)))
        self._player("PUT", "/me/player/volume", {"volume_percent": target})
        return f"Spotify volume {target}%."

    def shuffle(self, value: str = "on") -> str:
        v = (value or "on").lower()
        if v in ("toggle", ""):
            on = not (self.state() or {}).get("shuffle_state")
        else:
            on = v not in ("off", "false", "no", "stop", "disable")
        self._player("PUT", "/me/player/shuffle", {"state": "true" if on else "false"})
        return "Shuffle is on." if on else "Shuffle is off."

    def repeat(self, value: str = "track") -> str:
        v = (value or "track").lower()
        mode = "off" if v in ("off", "false", "no", "stop", "none", "disable") else \
            "context" if v in ("context", "playlist", "album", "all", "everything", "queue") else "track"
        self._player("PUT", "/me/player/repeat", {"state": mode})
        return {"off": "Repeat is off.", "context": "Repeating this playlist.", "track": "Repeating this song."}[mode]

    def seek(self, value: str, relative: int = 0) -> str:
        """relative 0: jump to a time; +1 / -1: skip forward/back by that many seconds."""
        secs = parse_time(value) if value else (15 if relative else None)
        if secs is None:
            raise SpotifyError(f"I didn't understand the time {value}.")
        st = self.state()
        if not st or not st.get("item"):
            raise SpotifyError("Nothing is playing on Spotify.")
        dur = int(st["item"].get("duration_ms") or 0)
        pos = int(st.get("progress_ms") or 0) + relative * secs * 1000 if relative else secs * 1000
        pos = max(0, min(pos, dur - 1000 if dur else pos))
        self.call("PUT", "/me/player/seek", {"position_ms": int(pos)})
        return f"At {_clock(pos)}."

    # ---- queue
    def queue_add(self, query: str) -> str:
        item = self.find_track(query) if query else None
        if not item:
            raise SpotifyError(f"I couldn't find {query} on Spotify.")
        self._player("POST", "/me/player/queue", {"uri": item["uri"]})
        return f"Added {describe(item)} to the queue."

    def queue_show(self, count: int = 3) -> str:
        q = self.call("GET", "/me/player/queue") or {}
        nxt = [x for x in q.get("queue") or [] if x][:count]
        if not nxt:
            return "Nothing is queued up."
        return "Up next: " + "; ".join(describe(x) for x in nxt) + "."

    # ---- library
    def _current(self) -> dict:
        st = self.state()
        item = (st or {}).get("item")
        if not item:
            raise SpotifyError("Nothing is playing on Spotify, so I don't know which song you mean.")
        return item

    def _target(self, song: str = "") -> dict:
        if song and not re.fullmatch(r"(?:this|that|the current|current|it)(?: song| track)?", song.strip().lower()):
            item = self.find_track(song)
            if not item:
                raise SpotifyError(f"I couldn't find {song} on Spotify.")
            return item
        return self._current()

    def like(self, song: str = "", unlike: bool = False) -> str:
        item = self._target(song)
        self.call("DELETE" if unlike else "PUT", "/me/library", {"uris": item["uri"]})
        return (f"Removed {describe(item)} from your liked songs." if unlike
                else f"Added {describe(item)} to your liked songs.")

    def is_liked(self) -> str:
        item = self._current()
        res = self.call("GET", "/me/library/contains", {"uris": item["uri"]}) or [False]
        return f"Yes, {describe(item)} is in your liked songs." if res and res[0] else \
            f"No, {describe(item)} isn't in your liked songs."

    # ---- playlists
    def list_playlists(self, count: int = 8) -> str:
        lists = self.my_playlists()
        if not lists:
            return "You don't have any playlists yet."
        names = [p.get("name", "") for p in lists[:count]]
        more = f", and {len(lists) - count} more" if len(lists) > count else ""
        return f"You have {len(lists)} playlists: " + ", ".join(names) + more + "."

    def create_playlist(self, name: str, public: bool = False, add_current: bool = False) -> str:
        name = (name or "").strip()
        if not name:
            raise SpotifyError("What should the playlist be called?")
        existing = self.my_playlist(name, threshold=0.97)
        if existing:
            return f"You already have a playlist called {existing.get('name')}."
        pl = self.call("POST", "/me/playlists", body={"name": name, "public": bool(public),
                                                     "description": "Made by Jarvis"}) or {}
        msg = f"Created the playlist {pl.get('name', name)}."
        if add_current and pl.get("id"):
            try:
                item = self._current()
                self.call("POST", f"/playlists/{pl['id']}/items", body={"uris": [item["uri"]]})
                msg = f"Created the playlist {pl.get('name', name)} with {describe(item)} in it."
            except SpotifyError:
                pass
        return msg

    def _owned_playlist(self, playlist: str) -> dict:
        pl = self.my_playlist(playlist)
        if not pl:
            raise SpotifyError(f"I couldn't find a playlist of yours called {playlist}.")
        return pl

    def add_to_playlist(self, playlist: str, song: str = "") -> str:
        pl = self._owned_playlist(playlist)
        item = self._target(song)
        self.call("POST", f"/playlists/{pl['id']}/items", body={"uris": [item["uri"]]})
        return f"Added {describe(item)} to {pl.get('name')}."

    def remove_from_playlist(self, playlist: str, song: str = "") -> str:
        pl = self._owned_playlist(playlist)
        item = self._target(song)
        self.call("DELETE", f"/playlists/{pl['id']}/items", body={"items": [{"uri": item["uri"]}]})
        return f"Removed {describe(item)} from {pl.get('name')}."

    def delete_playlist(self, playlist: str) -> str:
        pl = self._owned_playlist(playlist)
        # Spotify has no real delete: removing it from your library unfollows it (restorable for 90 days
        # at spotify.com/account/recover-playlists)
        self.call("DELETE", "/me/library", {"uris": pl["uri"]})
        return f"Deleted the playlist {pl.get('name')}. You can restore it from your Spotify account page."

    # ---- devices
    def list_devices(self) -> str:
        devs = self.devices()
        if not devs:
            return "Spotify isn't open on any device."
        parts = [d.get("name", "?") + (" (playing)" if d.get("is_active") else "") for d in devs]
        return "Spotify devices: " + ", ".join(parts) + "."

    def transfer(self, name: str) -> str:
        devs = self.devices()
        if not devs:
            self.launch()
            self.sleep(4)
            devs = self.devices()
        dev = self._pick_device(devs, name or socket.gethostname())
        if not dev:
            names = ", ".join(d.get("name", "?") for d in devs) or "none"
            raise SpotifyError(f"I can't find a Spotify device called {name}. Open devices: {names}.")
        if dev.get("is_active"):
            return f"Spotify is already playing on {dev.get('name')}."
        self.call("PUT", "/me/player", body={"device_ids": [dev["id"]], "play": True})
        return f"Moved Spotify to {dev.get('name')}."


def _launch_app():
    """Start the Spotify desktop app (does nothing off Windows)."""
    if hasattr(os, "startfile"):
        try:
            os.startfile("spotify:")
        except OSError:
            log.warning("couldn't start Spotify", exc_info=True)


_client: Spotify | None = None
_lock = threading.Lock()


def client(cfg=None) -> Spotify:
    """One shared client per process (keeps the access token in memory)."""
    global _client
    with _lock:
        if _client is None or (cfg is not None and _client.cfg is not cfg):
            _client = Spotify(cfg)
        return _client
