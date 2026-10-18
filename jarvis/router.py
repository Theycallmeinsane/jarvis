"""Fast path: common commands go straight to a tool, no language model involved (well under a second).

Anything that doesn't clearly match falls through to the brain, so the rules can stay conservative.
Risky tools (closing apps, power, email...) are never routed here; they always go through the brain and
its "Shall I…?" confirmation.
"""
from __future__ import annotations

import re

WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45,
    "fifty": 50, "sixty": 60, "ninety": 90, "half an": 0.5, "a half": 0.5,
}
UNIT_SECONDS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600}

FILLER = re.compile(r"^(hey |ok |okay )?(jarvis[, ]*)?(please |can you |could you |would you |will you |"
                    r"i want you to |go ahead and |just )*", re.I)
APPS_NOT_FOR_OPEN = re.compile(r"\b(settings?|folder|file|website|\.com|\.org|tab|link|page|document|my notes)\b")


def normalize(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"[^\w\s:%'.-]", " ", t)        # keep letters, numbers, times, percentages
    t = re.sub(r"\s+", " ", t).strip(" .")
    t = FILLER.sub("", t).strip()
    t = re.sub(r"\s*(please|for me|now|thanks|thank you)$", "", t).strip(" .")
    return t


def _number(s: str) -> float | None:
    s = s.strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    return WORD_NUMBERS.get(s)


MEDIA_APP = r"(?: (?:on|in) (?P<app>spotify|youtube|chrome|vlc|media player))?"
MEDIA_NOUN = r"(?: (?:the |my |some |this )?(?:music|song|track|playback|spotify|video|audio|it))?"


def route(text: str, toolbox, cfg=None) -> list[tuple[str, dict]] | None:
    """Return [(tool, args), ...] for a clear-cut command, or None to let the brain handle it."""
    t = normalize(text)
    if not t or len(t) > 80:
        return None
    has = lambda name: toolbox.get(name) is not None  # noqa: E731

    # ---- media
    if has("media_control"):
        m = re.fullmatch(r"(?:pause|stop|halt)" + MEDIA_NOUN + MEDIA_APP, t)
        if m and t not in ("stop", "halt"):        # a bare "stop" means "cancel" (handled before routing)
            return [("media_control", {"action": "pause",
                                       "app": m.group("app") or ("spotify" if "spotify" in t else "")})]
        m = re.fullmatch(r"(?:play|resume|continue|unpause|start)" + MEDIA_NOUN + MEDIA_APP, t)
        if m:
            app = m.group("app") or ("spotify" if "spotify" in t else "")
            return [("media_control", {"action": "play", "app": app})]
        m = re.fullmatch(r"(?:play )?(?:the )?(?:next|skip)(?: (?:song|track|one))?(?: (?:this|it))?" + MEDIA_APP, t) \
            or re.fullmatch(r"skip(?: (?:this|the)(?: song| track)?)?" + MEDIA_APP, t)
        m = m or re.fullmatch(r"(?:switch|change|go|move|skip)(?: on)?(?: to)? (?:the |a )?(?:next|another|different|new)"
                              r"(?: song| track| music| one)?" + MEDIA_APP, t) \
            or re.fullmatch(r"(?:switch|change|skip) (?:the |this )?(?:song|music|track)" + MEDIA_APP, t)
        if m:
            return [("media_control", {"action": "next", "app": m.groupdict().get("app") or ""})]
        m = re.fullmatch(r"(?:play )?(?:the )?(?:previous|last|prior)(?: (?:song|track|one))?" + MEDIA_APP, t) \
            or re.fullmatch(r"go back(?: a| one)?(?: song| track)", t)
        if m:
            return [("media_control", {"action": "previous", "app": m.groupdict().get("app") or ""})]
        if re.fullmatch(r"what(?:'s| is)(?: this| the)?(?: song| music| track)?(?: (?:that's|that is))? playing"
                        r"(?: now| right now)?|what song is this|what(?:'s| is) this song|who sings this", t):
            return [("media_control", {"action": "status"})]
        # "play glimpse of us by joji", "instead, put on bohemian rhapsody on spotify"
        m = re.fullmatch(r"(?:instead |now |then )?(?:play|put on)(?: me)? (?P<song>.+?)(?: (?:on|in) spotify)?", t)
        if m and has("play_song"):
            song = m.group("song").strip()
            generic = re.fullmatch(r"(?:the |my |some |this |a |that )?(?:music|song|track|playlist|spotify|it|"
                                   r"something|anything|video|audio)(?: (?:on|in) spotify)?", song)
            if not generic and len(song) > 2:
                return [("play_song", {"song": song})]

    # ---- one app's volume: "spotify volume to 30", "turn spotify down", "mute discord", "music volume up"
    if has("app_volume"):
        app_re = r"(?P<app>spotify|chrome|youtube|discord|vlc|steam|edge|firefox|teams|zoom|the music|music|the game)"
        m = re.fullmatch(r"(?:set |change |put )?(?:the )?" + app_re + r"(?:'s|s)? volume(?: to| at)? (?P<n>\d{1,3})"
                         r"(?: ?%| percent)?", t) \
            or re.fullmatch(r"(?:set |turn |put )(?:the )?" + app_re + r"(?: volume)? (?:to|at) (?P<n>\d{1,3})"
                            r"(?: ?%| percent)?", t)
        if m:
            return [("app_volume", {"app": m.group("app"), "action": "set", "percent": int(m.group("n"))})]
        m = re.fullmatch(r"(?:turn )?(?:the )?" + app_re + r"(?:'s|s)?(?: volume)? (?P<d>up|down)(?: a bit| a little)?", t) \
            or re.fullmatch(r"turn (?P<d>up|down) (?:the )?" + app_re + r"(?:'s|s)?(?: volume)?", t) \
            or re.fullmatch(r"(?:make )?(?:the )?" + app_re + r" (?P<d>louder|quieter)", t)
        if m:
            d = {"louder": "up", "quieter": "down"}.get(m.group("d"), m.group("d"))
            return [("app_volume", {"app": m.group("app"), "action": d})]
        m = re.fullmatch(r"(?P<a>mute|unmute) (?:the )?" + app_re, t)
        if m:
            return [("app_volume", {"app": m.group("app"), "action": m.group("a")})]
        m = re.fullmatch(r"what(?:'s| is) (?:the )?" + app_re + r"(?:'s|s)? volume(?: at)?", t) \
            or re.fullmatch(r"how loud is (?:the )?" + app_re, t)
        if m:
            return [("app_volume", {"app": m.group("app"), "action": "get"})]

    # ---- volume
    if has("volume"):
        m = re.fullmatch(r"(?:set |turn |change )?(?:the )?volume(?: to| at)? (\d{1,3})(?: ?%| percent)?", t) \
            or re.fullmatch(r"(?:set |turn )?(?:the )?volume (?:up|down) to (\d{1,3})(?: ?%| percent)?", t)
        if m:
            return [("volume", {"action": "set", "percent": int(m.group(1))})]
        if re.fullmatch(r"(?:turn (?:it|the volume|the sound) up|volume up|louder|increase (?:the )?volume|"
                        r"raise (?:the )?volume|turn up (?:the )?volume)(?: a bit| a little)?", t):
            return [("volume", {"action": "up"})]
        if re.fullmatch(r"(?:turn (?:it|the volume|the sound) down|volume down|quieter|lower (?:the )?volume|"
                        r"decrease (?:the )?volume|turn down (?:the )?volume)(?: a bit| a little)?", t):
            return [("volume", {"action": "down"})]
        if re.fullmatch(r"mute(?: the)?(?: sound| volume| audio| computer| pc)?", t):
            return [("volume", {"action": "mute"})]
        if re.fullmatch(r"unmute(?: the)?(?: sound| volume| audio| computer| pc)?", t):
            return [("volume", {"action": "unmute"})]

    # ---- time / date
    if has("get_datetime") and re.fullmatch(
            r"what(?:'s| is) the (?:time|date)(?: now| today)?|what time is it(?: now)?|"
            r"what(?:'s| is) (?:the date |the day )?today(?:'s date)?|what day is (?:it|today)", t):
        return [("get_datetime", {})]

    # ---- timers
    if has("set_timer"):
        m = re.fullmatch(r"(?:set |start )?(?:a |an )?(?:timer|alarm|reminder)(?: for| in)? (?P<n>[\w. ]+?) "
                         r"(?P<u>second|sec|minute|min|hour)s?(?: (?:to|for|that|about) (?P<msg>.+))?", t) \
            or re.fullmatch(r"remind me (?:in|after) (?P<n>[\w. ]+?) (?P<u>second|sec|minute|min|hour)s? "
                            r"(?:to|that|about) (?P<msg>.+)", t) \
            or re.fullmatch(r"remind me to (?P<msg>.+?) in (?P<n>[\w. ]+?) (?P<u>second|sec|minute|min|hour)s?", t)
        if m:
            n = _number(m.group("n"))
            if n:
                args = {"seconds": int(n * UNIT_SECONDS[m.group("u")])}
                if m.group("msg"):
                    args["message"] = m.group("msg").strip()
                return [("set_timer", args)]

    # ---- system stats, network, brightness, radios, sound output... (rules live in tools/system_ext.py)
    from .tools.system_ext import route_system

    plan = route_system(t, has)
    if plan:
        return plan

    # ---- desktop & windows
    if has("lock_computer") and re.fullmatch(r"lock (?:the |my )?(?:computer|pc|screen|laptop|windows)", t):
        return [("lock_computer", {})]
    if has("desktop_action"):
        if re.fullmatch(r"(?:show|go to)(?: me)? (?:the |my )?desktop|minimi[sz]e (?:all|everything)(?: windows)?", t):
            return [("desktop_action", {"action": "show_desktop"})]
        if re.fullmatch(r"(?:open )?task manager", t):
            return [("desktop_action", {"action": "task_manager"})]
    if has("take_screenshot") and re.fullmatch(r"(?:take a |grab a )?screen ?shot(?: of (?:the|my) screen)?", t):
        return [("take_screenshot", {})]

    # ---- routines: "run work mode", "good morning", "movie mode"
    if has("run_routine") and cfg is not None:
        from .routines import routines_from

        names = routines_from(cfg)
        m = re.fullmatch(r"(?:run|start|activate|do|begin|enable|turn on)? ?(?:my |the )?(?P<name>.+?)(?: routine)?", t)
        name = m.group("name") if m else ""
        # routines that need a spoken briefing (summarize: true) go through the brain instead
        if name in names and names[name].get("summarize", True) is False:
            return [("run_routine", {"name": name})]

    # ---- switch to an open window ("switch to chrome") or open an app ("open spotify")
    m = re.fullmatch(r"(?:switch|go|change) to (?:the )?(?P<w>[\w .'-]+?)(?: window| app)?", t)
    if m and has("window_action") and not re.search(r"\b(voice|male|female|claude|local|model|brain|mode|"
                                                    r"desktop|light|dark|lm studio)\b", m.group("w")):
        from .tools.windows import find_window

        if find_window(m.group("w")):
            return [("window_action", {"name": m.group("w"), "action": "focus"})]
    m = re.fullmatch(r"(?:open|launch|start|fire up|bring up|run) (?:up )?(?:the |my )?(?P<app>[\w .'+-]+?)(?: app| application| program)?", t)
    if m and has("open_app") and not APPS_NOT_FOR_OPEN.search(m.group("app")):
        app = m.group("app")
        if has("open_folder"):
            from .tools.windows import FOLDERS

            if app in FOLDERS:
                return [("open_folder", {"folder": app})]
        return [("open_app", {"name": app})]
    return None
