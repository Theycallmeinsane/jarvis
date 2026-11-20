"""Spotify by voice, end to end: play any song/artist/album/playlist or your liked songs, the player (shuffle,
repeat, seek, queue, devices, Spotify's own volume), liking songs and managing your playlists.

These tools appear once SPOTIFY_CLIENT_ID is in .env (see "Spotify" in the README). If Spotify isn't signed in,
needs Premium, or the internet is down, play/pause/next/previous/what's playing fall back to the Windows media
session (media keys as a last resort), and songs open through the Spotify app.
"""
from __future__ import annotations

import logging
import re
import threading
from urllib.parse import quote

from .. import spotify as sp
from .registry import Spoken, tool

log = logging.getLogger(__name__)

PLAY_KINDS = ["auto", "track", "artist", "album", "playlist", "liked"]
CONTROL_ACTIONS = ["pause", "resume", "next", "previous", "status", "volume", "shuffle", "repeat", "seek", "forward",
                   "back", "queue", "up_next", "like", "unlike", "is_liked", "devices", "transfer", "connect", "open"]
PLAYLIST_ACTIONS = ["list", "create", "add", "remove"]
FALLBACK = (sp.NotConnected, sp.PremiumRequired, sp.NoDevice)
SPOTIFY_WORDS = ("spotify", "play", "song", "music", "track", "playlist", "album", "artist", "liked", "like", "love",
                 "save", "shuffle", "repeat", "loop", "queue", "next", "skip", "previous", "back", "pause", "resume",
                 "playing", "volume", "seek", "forward", "rewind", "jump", "device", "phone", "speaker", "move",
                 "switch", "transfer", "connect", "listen", "put on", "hear", "add", "remove", "create", "make",
                 "unlike", "favou?rite", "stop", "continue", "go", "open")


def _fail(msg: str):
    from .builtin import ToolFailed

    raise ToolFailed(msg)


def _enabled(cfg) -> bool:
    return sp.enabled(cfg)


def _client(ctx):
    return sp.client(ctx.cfg if ctx else None)


def _basic(action: str) -> str:
    """play/pause/next/previous/status without the Web API (Windows media session, then media keys)."""
    from .. import media

    ok, message = media.control(action, "spotify")
    if not ok:
        _fail(message)
    return message


def _connect_in_background(ctx) -> str:
    if not sp.client_id():
        _fail(sp.SETUP_HINT)
    cfg = ctx.cfg if ctx else None

    def run():
        try:
            log.info(sp.login(cfg))
            sp.client(cfg)._tok = None          # pick up the new token
        except sp.SpotifyError as e:
            log.warning("spotify sign-in: %s", e)

    threading.Thread(target=run, name="spotify-login", daemon=True).start()
    return "I've opened Spotify's sign-in page in your browser. Approve it there and I'll be connected."


@tool("Play something on Spotify: a song ('Glimpse of Us by Joji'), an artist, an album, one of the user's "
      "playlists (or any public one), or their liked songs. Empty query resumes. Starts Spotify if it's closed.",
      {"query": {"type": "string", "description": "What to play, as the user said it"},
       "kind": {"type": "string", "enum": PLAY_KINDS,
                "description": "auto = figure it out; liked = the user's Liked Songs"}},
      required=[], enabled=_enabled,
      triggers=("play", "put on", "listen", "hear", "song", "music", "spotify", "track", "album", "artist",
                "playlist", "liked", "start"))
def spotify_play(query: str = "", kind: str = "auto", ctx=None):
    try:
        return Spoken(_client(ctx).play(query, kind))
    except FALLBACK as e:
        log.info("spotify_play falling back: %s", e)
        if isinstance(e, sp.NoDevice) and "started Spotify" in str(e):
            _fail(str(e))
        if not query.strip():
            return _basic("play")
        if kind in ("auto", "track"):
            from .. import media

            ok, message = media.play_song(query)
            if not ok:
                _fail(message)
            return Spoken(message)
        import os

        if hasattr(os, "startfile"):
            if kind == "liked":
                os.startfile("spotify:collection:tracks")
                _fail(f"{e} I've opened your Liked Songs in Spotify; press play there.")
            os.startfile(f"spotify:search:{quote(query)}")
            _fail(f"{e} I've opened a Spotify search for {query} instead.")
        _fail(str(e))
    except sp.SpotifyError as e:
        _fail(str(e))


@tool("Control Spotify: pause, resume, next, previous, status (what's playing), volume (Spotify's own: 0-100, "
      "up, down), shuffle (on/off), repeat (track/playlist/off), seek (to a time like 1:30), forward/back "
      "(seconds), queue (add a song), up_next (what's queued), like/unlike (this song or a named one), is_liked, "
      "devices, transfer (to a device like 'phone'), connect (sign in), open (start the app).",
      {"action": {"type": "string", "enum": CONTROL_ACTIONS},
       "value": {"type": "string", "description": "volume: 0-100/up/down; shuffle: on/off; repeat: track/playlist/"
                                                   "off; seek: 1:30; forward/back: seconds; queue/like/unlike: "
                                                   "song name (empty = this song); transfer: device name"}},
      required=["action"], enabled=_enabled, triggers=SPOTIFY_WORDS)
def spotify_control(action: str, value: str = "", ctx=None):
    action = (action or "").lower().strip()
    value = str(value or "").strip()
    if action == "connect":
        return Spoken(_connect_in_background(ctx))
    if action == "open":
        sp._launch_app()
        return "Opening Spotify."
    c = _client(ctx)
    calls = {
        "pause": c.pause, "resume": c.resume, "status": c.now_playing,
        "next": lambda: c.skip("next"), "previous": lambda: c.skip("previous"),
        "volume": lambda: c.volume(value), "shuffle": lambda: c.shuffle(value or "on"),
        "repeat": lambda: c.repeat(value or "track"), "seek": lambda: c.seek(value),
        "forward": lambda: c.seek(value or "15", relative=1), "back": lambda: c.seek(value or "15", relative=-1),
        "queue": lambda: c.queue_add(value), "up_next": c.queue_show,
        "like": lambda: c.like(value), "unlike": lambda: c.like(value, unlike=True), "is_liked": c.is_liked,
        "devices": c.list_devices, "transfer": lambda: c.transfer(value),
    }
    if action not in calls:
        _fail(f"I can't do {action} on Spotify.")
    if action == "queue" and not value:
        _fail("Which song should I add to the queue?")
    try:
        return Spoken(calls[action]())
    except FALLBACK as e:
        if action in ("pause", "resume", "next", "previous", "status"):
            log.info("spotify_control %s falling back: %s", action, e)
            return _basic({"resume": "play"}.get(action, action))
        _fail(str(e) if isinstance(e, sp.NotConnected) else f"{e} {_needs_api(action)}")
    except sp.SpotifyError as e:
        _fail(str(e))


def _needs_api(action: str) -> str:
    return {"volume": "Try 'Spotify volume to 30' for the Windows mixer instead."}.get(
        action, "That needs Spotify connected with Premium.")


@tool("Manage the user's Spotify playlists: list them, create one, add a song to one, remove a song from one. "
      "The song defaults to whatever is playing.",
      {"action": {"type": "string", "enum": PLAYLIST_ACTIONS},
       "playlist": {"type": "string", "description": "Playlist name (for create: the new name)"},
       "song": {"type": "string", "description": "Optional song name; empty = the song playing now"}},
      required=["action"], enabled=_enabled,
      triggers=("playlist", "playlists", "add", "remove", "create", "make", "new", "list", "delete", "take"))
def spotify_playlist(action: str, playlist: str = "", song: str = "", ctx=None):
    c = _client(ctx)
    action = (action or "").lower()
    if action != "list" and not playlist.strip():
        _fail("Which playlist?")
    try:
        if action == "list":
            return Spoken(c.list_playlists())
        if action == "create":
            return Spoken(c.create_playlist(playlist, add_current=bool(song)))
        if action == "add":
            return Spoken(c.add_to_playlist(playlist, song))
        if action == "remove":
            return Spoken(c.remove_from_playlist(playlist, song))
    except sp.SpotifyError as e:
        _fail(str(e))
    _fail(f"I can't {action} playlists.")


@tool("Delete one of the user's Spotify playlists (it can be restored from their Spotify account page).",
      {"playlist": {"type": "string", "description": "Playlist name"}},
      required=["playlist"], dangerous=True, enabled=_enabled,
      confirm=lambda a: f"Delete your Spotify playlist {a.get('playlist', '')}?",
      triggers=("delete", "remove", "get rid", "playlist"))
def spotify_delete_playlist(playlist: str, ctx=None):
    try:
        return Spoken(_client(ctx).delete_playlist(playlist))
    except sp.SpotifyError as e:
        _fail(str(e))


# ---------------------------------------------------------------- fast-path rules (no language model)

ON_SPOTIFY = r"(?: (?:on|in|from) spotify)?"
THIS = r"(?:this|that|it|the current|current)(?: song| track| one)?"
DEVICE = (r"(?P<d>(?:my |the )?(?:[\w' ]+ )?(?:phone|iphone|android|laptop|pc|computer|desktop|tv|television|speaker|"
          r"speakers|tablet|ipad|echo|alexa|google home|nest|car|headphones|soundbar|xbox|playstation))")
UNITS = r"(?P<u>seconds?|secs?|minutes?|mins?)"


def _secs(n: str, u: str) -> str:
    return str(int(n) * (60 if u.startswith("m") else 1))


def route_spotify(t: str, has) -> list[tuple[str, dict]] | None:
    """Spotify commands that only the Web API can do. Plain play/pause/next stay with media_control."""
    if not has("spotify_control"):
        return None
    ctl = lambda action, value="": [("spotify_control", {"action": action, "value": value})]  # noqa: E731

    if re.fullmatch(r"(?:connect|link|log ?in(?: to)?|sign in(?: to)?|set up)(?: to)? (?:my )?spotify(?: account)?", t):
        return ctl("connect")

    # ---- playing particular things
    if has("spotify_play"):
        m = re.fullmatch(r"(?P<s>shuffle|play|put on|start)(?: me)?(?: all)? (?:my |the )?(?:liked|saved|favou?rite|"
                         r"loved) (?:songs|tracks|music)" + ON_SPOTIFY, t)
        if m:
            plan = [("spotify_play", {"query": "liked songs", "kind": "liked"})]
            return plan + ctl("shuffle", "on") if m.group("s") == "shuffle" else plan
        m = re.fullmatch(r"(?:play|put on)(?: me)?(?: some)? (?:music|songs|tracks|stuff|something) (?:by|from) "
                         r"(?P<q>.+?)" + ON_SPOTIFY, t) \
            or re.fullmatch(r"(?:play|put on) (?:the )?artist (?P<q>.+?)" + ON_SPOTIFY, t)
        if m:
            return [("spotify_play", {"query": m.group("q"), "kind": "artist"})]
        m = re.fullmatch(r"(?:play|put on) (?:the )?album (?P<q>.+?)" + ON_SPOTIFY, t) \
            or re.fullmatch(r"(?:play|put on) (?:the )?(?P<q>.+?) album" + ON_SPOTIFY, t)
        if m:
            return [("spotify_play", {"query": m.group("q"), "kind": "album"})]
        m = re.fullmatch(r"(?P<s>shuffle|play|put on|start)(?: me)? (?:my |the )?playlist (?:called |named )?"
                         r"(?P<q>.+?)" + ON_SPOTIFY, t) \
            or re.fullmatch(r"(?P<s>shuffle|play|put on|start)(?: me)? (?:my |the )?(?P<q>.+?) playlist" + ON_SPOTIFY, t)
        if m and m.group("q") not in ("a", "some", "any", "this", "that"):
            plan = [("spotify_play", {"query": m.group("q"), "kind": "playlist"})]
            return plan + ctl("shuffle", "on") if m.group("s") == "shuffle" else plan

    # ---- liking
    if re.fullmatch(r"(?:like|love|heart) " + THIS + ON_SPOTIFY + r"|save " + r"(?:this|that|the current|current) "
                    r"(?:song|track)" + ON_SPOTIFY + r"|add " + THIS + r" to (?:my )?(?:liked|saved|favou?rite|loved)"
                    r"(?: songs| tracks| music)?|i (?:really )?(?:like|love) this (?:song|track)", t):
        return ctl("like")
    if re.fullmatch(r"(?:unlike|unsave) " + THIS + r"|(?:remove|delete|take) " + THIS + r" (?:from|out of|off) "
                    r"(?:my )?(?:liked|saved|favou?rite|loved)(?: songs| tracks| music)?", t):
        return ctl("unlike")
    if re.fullmatch(r"(?:is|have i (?:liked|saved)) " + THIS + r"(?: in my (?:liked|saved)(?: songs)?| liked| saved)?|"
                    r"did i (?:like|save) " + THIS, t) and re.search(r"\b(liked|saved|like|save)\b", t):
        return ctl("is_liked")

    # ---- queue
    if re.fullmatch(r"what(?:'s| is) (?:next|up next|coming up(?: next)?|in (?:my |the )?queue)|"
                    r"(?:show|read|tell) (?:me )?(?:my |the )?queue|what(?:'s| is) (?:my |the )?queue", t):
        return ctl("up_next")
    m = re.fullmatch(r"(?:add|put) (?P<s>.+?) (?:to|in|on|into) (?:the |my )?queue|queue(?: up)? (?P<s2>.+)|"
                     r"play (?P<s3>.+?) (?:next|after this(?: song)?)", t)
    if m:
        song = (m.group("s") or m.group("s2") or m.group("s3")).strip()
        song = re.sub(ON_SPOTIFY + "$", "", song)
        if not re.fullmatch(THIS + r"|the (?:next|last|previous)(?: one| song| track)?|something|a song", song):
            return ctl("queue", song)

    # ---- playlists
    if has("spotify_playlist"):
        pl = lambda action, playlist="", song="": [("spotify_playlist", {"action": action, "playlist": playlist,  # noqa: E731
                                                                       "song": song})]
        if re.fullmatch(r"(?:what|which) playlists do i have|(?:list|show|read)(?: me)? (?:all )?(?:of )?my playlists|"
                        r"what are my playlists|(?:list|show)(?: me)? (?:my )?spotify playlists", t):
            return pl("list")
        m = re.fullmatch(r"(?:create|make|start)(?: me)? (?:a |an )?(?:new )?(?:spotify )?playlist (?:called |named )?"
                         r"(?P<n>.+)", t) \
            or re.fullmatch(r"(?:create|make|start)(?: me)? (?:a |an )?(?:new )?(?P<n>.+?) playlist", t)
        if m and m.group("n") not in ("a", "new", "spotify"):
            return pl("create", m.group("n"))
        m = re.fullmatch(r"(?:add|put|save) " + THIS + r" (?:to|in|into|on) (?:my |the )?(?P<p>.+?)(?: playlist)?", t)
        if m and not re.fullmatch(r"(?:liked|saved|favou?rite|loved)(?: songs| tracks| music)?|queue", m.group("p")):
            return pl("add", m.group("p"))
        m = re.fullmatch(r"(?:add|put) (?P<s>.+?) (?:to|in|into|on) (?:my |the )?(?P<p>.+?) playlist", t)
        if m:
            return pl("add", m.group("p"), m.group("s"))
        m = re.fullmatch(r"(?:remove|delete|take) " + THIS + r" (?:from|off|out of) (?:my |the )?(?P<p>.+?)"
                         r"(?: playlist)?", t)
        if m and not re.fullmatch(r"(?:liked|saved|favou?rite|loved)(?: songs| tracks| music)?|queue", m.group("p")):
            return pl("remove", m.group("p"))
        # deleting a whole playlist always goes through the brain and its "Shall I…?" confirmation

    # ---- shuffle / repeat
    if re.fullmatch(r"(?:turn |switch |put )?(?:on )?shuffle(?: on| mode on)?(?: the music| it| my music)?" + ON_SPOTIFY
                    + r"|shuffle (?:the |my |this )?(?:music|songs|playlist|it|queue)" + ON_SPOTIFY
                    + r"|(?:turn|switch|put) shuffle on|enable shuffle", t):
        return ctl("shuffle", "on")
    if re.fullmatch(r"(?:turn |switch )?(?:off )?shuffle off|(?:turn|switch) off shuffle|(?:stop|no) shuffl(?:e|ing)|"
                    r"disable shuffle|don't shuffle|unshuffle", t):
        return ctl("shuffle", "off")
    if re.fullmatch(r"(?:turn |switch )?(?:off )?repeat off|(?:turn|switch) off (?:repeat|loop)|(?:stop|no) (?:repeat|"
                    r"loop)(?:ing)?|disable repeat|don't repeat", t):
        return ctl("repeat", "off")
    if re.fullmatch(r"(?:repeat|loop) (?:this |the )?(?:playlist|album)|(?:repeat|loop) (?:everything|all)", t):
        return ctl("repeat", "playlist")
    if re.fullmatch(r"(?:repeat|loop) (?:this|the) (?:song|track)|loop (?:this|it)|(?:put|play|keep) " + THIS +
                    r" on (?:repeat|loop)|(?:turn on|enable) (?:repeat|loop)(?: mode)?|(?:repeat|loop)(?: mode)? on", t):
        return ctl("repeat", "track")

    # ---- seeking (a movie in VLC was already handled before this)
    m = re.fullmatch(r"(?:skip|go|jump|fast)(?: ahead| forward)(?: by)? (?P<n>\d+) ?" + UNITS + r"|"
                     r"(?:skip|jump|fast forward) (?P<n2>\d+) ?(?P<u2>seconds?|secs?|minutes?|mins?)(?: ahead| forward)?", t)
    if m:
        return ctl("forward", _secs(m.group("n") or m.group("n2"), m.group("u") or m.group("u2")))
    m = re.fullmatch(r"(?:rewind|go back|skip back|jump back)(?: by)? (?P<n>\d+) ?" + UNITS, t)
    if m:
        return ctl("back", _secs(m.group("n"), m.group("u")))
    m = re.fullmatch(r"(?:skip|jump|go|seek|fast forward) to (?:the )?(?P<at>\d{1,2}:\d{2}(?::\d{2})?)(?: mark)?", t)
    if m:
        return ctl("seek", m.group("at"))
    if re.fullmatch(r"(?:start|play) (?:this|the|it|the song|this song) (?:from the|over from the|again from the) "
                    r"(?:start|beginning)|restart (?:this|the) (?:song|track)", t):
        return ctl("seek", "0")

    # ---- devices
    if re.fullmatch(r"(?:what|which|list)(?: my)?(?: spotify)? devices(?: (?:are|can|do).*)?|(?:list|show)(?: me)? "
                    r"(?:my )?(?:spotify )?devices|where can i play (?:spotify|music)", t):
        return ctl("devices")
    m = re.fullmatch(r"(?:play|move|switch|transfer|put|send|continue)(?: the)?(?: music| spotify| it| playback| this|"
                     r" song| this song| everything)?(?: on| to| over to| onto| on to) " + DEVICE, t)
    if m:
        return ctl("transfer", re.sub(r"^(?:my|the) ", "", m.group("d")))
    return None
