"""Which tools a local brain sees for a request.

Sending every tool definition with every request costs about 10,000 tokens, more than a local model's
context window (llm.providers.ollama.num_ctx, 8192 by default). Ollama then quietly cuts the start of the
prompt, Jarvis's own instructions included, and the model loses track of who it is and starts making things
up. A small model also picks better from a short list than from a hundred. So a local brain gets only the
tools that fit the request (plus the ones from the turn before, so follow-ups like "and louder" still work);
Claude, with a far bigger context, still gets all of them.

How a tool earns its place: each meaningful word of the request is looked up among every tool's keywords
(its name, the words people say for it in SAID, its trigger words, its description). A word shared by many
tools ("open", "file") counts for little, one that belongs to a single tool ("screenshot") for a lot.
Misheard words ("whether", "valume", "spotfy", "tymer", "screen shot") are matched by spelling and sound.
Phrases in PHRASES ("can't hear", "what's eating my ram") count in full. A tool whose trigger words are
missing would be refused if called (Tool.allowed_for), so it is never offered. A tool matched only by common
trigger or description words ("folder", "open") needs a higher score than one the request names. What's left
is ranked, and anything far behind the best match is dropped.

tests/test_tool_pick.py holds hundreds of real requests with the tools each needs; when a request misses its
tool, add the words people say to SAID or PHRASES and the request to that test.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import defaultdict

from .wordmatch import STOP, likeness, stem, tokens

log = logging.getLogger(__name__)

# words people say for a tool that its name and description may not contain
SAID = {
    "get_datetime": "time date day today clock month year late hour weekday monday tuesday wednesday thursday "
                    "friday saturday sunday",
    "get_weather": "weather rain raining rainy sunny forecast temperature hot cold warm chilly freezing umbrella "
                   "outside snow snowing wind windy storm humid humidity cloudy degrees jacket coat sunscreen",
    "wikipedia_summary": "wikipedia history biography invented inventor",
    "web_lookup": "news headlines latest price stock score won winner release",
    "web_search": "search google youtube browse",
    "open_app": "open launch start app application",
    "close_app": "close quit exit kill shut",
    "open_website": "website site webpage url",
    "media_control": "pause resume unpause next skip previous playing track song music",
    "play_song": "song songs play listen artist album playlist",
    "app_volume": "spotify discord chrome vlc steam firefox edge teams zoom game",
    "volume": "volume louder quieter softer mute unmute sound loud quiet loudness hear",
    "set_timer": "timer alarm remind reminder wake countdown",
    "list_timers": "timers alarms reminders",
    "cancel_timer": "cancel timer alarm reminder",
    "take_note": "note notes jot shopping groceries todo",
    "read_notes": "notes note todo",
    "find_files": "find file files document documents pdf docx resume cv spreadsheet invoice receipt locate",
    "open_path": "file folder path",
    "read_clipboard": "clipboard copied",
    "copy_to_clipboard": "copy clipboard",
    "type_text": "type typing dictate",
    "take_screenshot": "screenshot capture",
    "look_at_screen": "screen monitor error display showing",
    "lock_computer": "lock",
    "power": "shutdown restart reboot sleep hibernate power",
    "run_routine": "routine",
    "switch_voice": "voice accent female male british american",
    "switch_model": "brain model claude local cloud ollama",
    "chrome_tabs": "tabs",
    "chrome_tab": "tab tabs reopen",
    "chrome_open": "chrome browser",
    "chrome_page": "back forward reload refresh scroll zoom page",
    "chrome_read": "summarize summarise summary article page",
    "chrome_click": "click button link",
    "chrome_type": "box field fill",
    "chrome_video": "video youtube speed rewind fullscreen",
    "claude_chat": "claude clawed clod ask",
    "list_folder": "folder contents inside newest",
    "disk_usage": "space storage drive",
    "largest_files": "biggest largest huge",
    "read_file": "read summarize pdf txt readme docx",
    "find_duplicates": "duplicate duplicates",
    "create_folder": "folder",
    "write_text_file": "text file",
    "copy_item": "copy backup",
    "rename_item": "rename",
    "move_item": "move",
    "delete_item": "delete remove trash erase",
    "organize_folder": "organize organise tidy declutter sort",
    "zip_item": "zip compress",
    "unzip_item": "unzip extract unpack decompress",
    "github_notifications": "notifications mentions",
    "github_builds": "build builds pass passed passing green ci",
    "github_build_failure": "fail failed failing broke broken failure",
    "github_open_items": "pull requests issues prs",
    "github_commits": "commits commit",
    "github_repo_overview": "stars forks overview",
    "github_create_issue": "bug ticket",
    "github_comment": "comment reply",
    "github_rerun_build": "rerun retry",
    "remember": "remember",
        "list_memories": "memories",
    "rgb": "rgb lights lighting led leds colour color rainbow glow purple red blue green white pink orange yellow",
    "system_status": "battery health cpu ram memory disk usage",
    "system_stats": "cpu gpu ram vram temperature fans hot uptime stats statistics",
    "network_status": "internet online offline connection connected ping ip speed lag",
    "find_process": "process running",
    "end_process": "kill frozen terminate responding",
    "set_priority": "priority prioritize prioritise",
    "service_status": "service spooler",
    "service_control": "service spooler restart",
    "list_startup_apps": "startup autostart automatically",
    "set_startup_app": "startup autostart disable enable",
    "radio": "bluetooth airplane wifi wireless radio",
    "brightness": "brightness brighter dimmer dim bright darker",
    "audio_output": "headset headphones headphone speakers speaker output earbuds",
    "recycle_bin_status": "recycle trash",
    "empty_recycle_bin": "empty",
    "clear_temp_files": "temp temporary junk cache",
    "windows_update_status": "updates update",
    "play_movie": "movie film watch episode season",
    "vlc_control": "subtitles subtitle captions vlc episode",
    "list_movies": "movies films",
    "press_keys": "press hit shortcut key keys ctrl control alt shift undo redo paste",
    "desktop_action": "desktop emoji snip snipping explorer",
    "window_action": "focus minimize maximize snap",
    "close_window": "window",
    "open_settings": "settings setting",
    "open_folder": "downloads documents pictures folder",
    "read_aloud": "aloud",
    "top_processes": "processes slow hogging laggy lagging sluggish",
    "theme": "theme dark",
    "wifi": "wifi network networks connect disconnect ssid",
    # only there once set up (Google account, API key, PowerShell switched on)
    "calendar_upcoming": "calendar meeting meetings events schedule appointments agenda",
    "calendar_add_event": "calendar schedule book appointment",
    "gmail_unread": "email emails mail inbox unread",
    "gmail_read": "email mail",
    "gmail_find_address": "address",
    "gmail_draft": "draft",
    "gmail_send": "send email",
    "ask_claude": "claude",
    "run_powershell": "powershell command terminal",
}

# word groups that point straight at a tool, matched against the request's lower-case words with apostrophes
# dropped ("what's" -> "what", "can't" -> "cant")
PHRASES = {
    "get_datetime": (r"what time", r"what (day|date)", r"how late"),
    "get_weather": (r"need (an )?umbrella", r"going to rain", r"(hot|cold|warm) (is it )?outside"),
    "wikipedia_summary": (r"who (was|were|invented|discovered|wrote|painted|founded)", r"history of"),
    "web_lookup": (r"who won", r"what happened"),
    "web_search": (r"search (youtube|google|online|the web)", r"on youtube"),
    "open_website": (r"\w+ dot (com|org|net|io)",),
    "media_control": (r"what (is )?playing", r"(next|previous|last) (song|track)", r"stop (the )?(music|song|playback)",
                      r"(pause|resume|skip) (the |this )?(music|song|track)"),
    "play_song": (r"play .+ by \w+", r"put on some", r"listen to \w+"),
    "app_volume": (r"turn (spotify|discord|chrome|vlc|steam|youtube|music|the music|the game|the song) (up|down)",),
    "volume": (r"turn (it |that |this |the volume |the sound |everything )?(way |right |all the way |a bit |a little )?"
               r"(up|down)", r"(barely|cant|can not) hear", r"crank (it|that)", r"too (loud|quiet)",
               r"keep it down"),
    "set_timer": (r"wake me", r"remind me", r"(in|for) (a |an |\w+ )?(seconds?|minutes?|hours?)",
                  r"set (a |an |the )?(timer|alarm|reminder)", r"(timer|alarm|countdown) for"),
    "list_timers": (r"time (is )?left", r"how long (is )?left", r"(what|which|any|my|active) (timers|alarms|reminders)",
                    r"(timers|alarms|reminders) (do i have|are|set|running)"),
    "cancel_timer": (r"(stop|cancel|kill|silence|turn off|delete) (the |that |my |all |all my )?"
                     r"(alarm|timer|reminder)",),
    "take_note": (r"(write|note|jot|put) (that |this |it )?down", r"(make|take) a note", r"shopping list",
                  r"add .+ to my (\w+ )?list"),
    "read_notes": (r"to do list", r"my (notes|list)", r"what did i (write|note|jot)"),
    "find_files": (r"where (is|are) my", r"search my (computer|pc|files|drive|laptop)",
                   r"find (my|the) \w+ (file|document|pdf)"),
    "open_path": (r"open (it|that|this|the (first|second|third|last|other) one)", r"open the file"),
    "read_clipboard": (r"what did i (just )?copy", r"on (my |the )?clipboard"),
    "copy_to_clipboard": (r"copy (that|this|it) to", r"to (my |the )?clipboard"),
    "look_at_screen": (r"on (my |the )?(screen|monitor|display)", r"this error", r"what (is )?wrong",
                       r"look at (my |the |this )?(screen|monitor|display|window|error)", r"looking at",
                       r"look at (this|that)"),
    "take_screenshot": (r"screen (shot|shoot|grab)", r"(picture|photo|shot) of (my |the )?screen"),
    "power": (r"shut (it |the computer |the pc )?down", r"turn off the (pc|computer|laptop)", r"to sleep"),
    "switch_model": (r"switch to (claude|cloud|local)", r"local (model|brain)"),
    "chrome_tabs": (r"what tabs", r"tabs (do i have )?open", r"list (my |the )?(chrome )?tabs"),
    "chrome_tab": (r"(this|that|the|next|previous|new|last|other) tab", r"(switch to|close) the \w+ tab"),
    "chrome_open": (r"in (chrome|the browser|a new tab)",),
    "claude_chat": (r"ask (claude|cloud|clawed|clod)",),
    "ask_claude": (r"ask (claude|cloud|clawed|clod)",),
    "chrome_page": (r"go (back|forward)", r"scroll (up|down)", r"zoom (in|out)"),
    "chrome_read": (r"this (page|article)",),
    "chrome_type": (r"(in|into) the \w+ (box|field)", r"search box", r"text box", r"fill in"),
    "chrome_video": (r"(skip|go) (ahead|forward|back)", r"full screen"),
    "list_folder": (r"what (is )?in (my|the)", r"on my desktop", r"newest files",
                    r"list (my |the )?(files|desktop|downloads|documents|folder)"),
    "disk_usage": (r"(taking|using) (up )?(the most )?(space|storage)", r"free space", r"space left"),
    "largest_files": (r"(biggest|largest) files",),
    "read_file": (r"read the (file|document|pdf)", r"(file|pdf|document) say"),
    "create_folder": (r"new folder", r"(create|make) a folder"),
    "write_text_file": (r"text file", r"to a file", r"(create|make|write|save) a file"),
    "copy_item": (r"back up", r"copy (the|my) \w+ to",
                  r"copy .+ (to|into|onto) (my |the |a )?(\w+ )?"
                  r"(folder|drive|desktop|documents|downloads|pictures|usb|stick)"),
    "move_item": (r"move .+ (to|into)",),
    "delete_item": (r"get rid of", r"throw away", r"(in|into|to) the (bin|trash)"),
    "organize_folder": (r"clean up", r"tidy up"),
    "github_create_issue": (r"(file|create|open|make|raise) (a|an) (bug|issue|ticket)",),
    "github_rerun_build": (r"run (it |the build )?again", r"re run"),
    "github_builds": (r"did the (last )?build", r"build pass", r"ci green"),
    "github_build_failure": (r"why did (the |my )?(build|ci|it) fail", r"what broke"),
    "github_open_items": (r"pull requests?", r"open issues"),
    "list_memories": (r"what (do|did|have) you (know|remember|remembered)", r"(know|remember|remembered) about me",
                      r"(saved|stored|kept|have) (on|about) me",
                      r"what have i told you"),
    # not "forget it" (= never mind), and not "don't forget": either could wipe a remembered fact
    "forget": (r"(?<!dont )(?<!not )forget (about )?(my|the|what|everything|all)",
               r"(?<!dont )(?<!not )forget (about )?(that i|that my) \w+",
               r"(?<!dont )(?<!not )forget \w+ (name|number|birthday|address)"),
    "rgb": (r"turn (of|off|on) the lights",),
    "system_status": (r"how (is )?(my |the )?(computer|pc|laptop|machine|system)( doing| running)?",
                      r"(computer|pc|laptop) (health|doing)",
                      r"battery (left|life|level)"),
    "system_stats": (r"how hot", r"system stats"),
    "network_status": (r"internet (down|working|slow|speed)", r"am i online", r"ip address",
                       r"check (my |the )?connection"),
    "find_process": (r"is \w+ running", r"find the process"),
    "end_process": (r"not responding", r"force (close|quit)", r"frozen"),
    "top_processes": (r"(eating|using|hogging|taking|eats|uses|hogs) (up )?(all |so much |the most )?(of )?(my |the )?"
                      r"(ram|memory|cpu|gpu|resources)", r"(which|what) (program|app|process)", r"so slow",
                      r"(running|really|very) slow", r"lagging"),
    "service_status": (r"is the \w+ (service|spooler) running",),
    "service_control": (r"(start|stop|restart) (the )?\w+ (service|spooler)", r"(start|stop|restart) (the )?spooler"),
    "list_startup_apps": (r"(start|starts|starting|launch|launches|run|runs) (with|at|on) (windows|startup|boot|login)",
                          r"at startup", r"when windows (start|boot)"),
    "set_startup_app": (r"(stop|disable|enable|remove) .+ (starting|at startup|from startup|on startup|with windows)",),
    "radio": (r"blue tooth", r"(airplane|flight) mode", r"turn (on|off) (the )?(wifi|wi fi|bluetooth|blue tooth)"),
    "brightness": (r"too (bright|dark)", r"darker in here", r"(dim|brighten) the screen"),
    "audio_output": (r"play (the sound |it )?through", r"(sound|audio) to (my|the)",
                     r"switch (the )?(sound|audio|output)(?! track)"),
    "recycle_bin_status": (r"in the (recycle|recycling) bin", r"how full"),
    "empty_recycle_bin": (r"empty (the |my )?(recycle|recycling|trash|bin)",),
    "clear_temp_files": (r"temp files", r"junk files"),
    "windows_update_status": (r"windows updates?", r"restart pending"),
    "play_movie": (r"watch \w+", r"(play|watch|put on|start) the (movie|film)", r"season \w+ episode"),
    "vlc_control": (r"audio track", r"subtitles? (on|off)", r"next episode", r"in the movie",
                    r"(stop|pause|close|resume|rewind) (the )?(movie|film|episode|vlc)"),
    "list_movies": (r"what (movies|films|shows)", r"do i have the (movie|film|show)", r"my (movies|films|shows)",
                    r"tv shows"),
    "press_keys": (r"alt tab", r"(control|ctrl) \w+", r"press \w+"),
    "desktop_action": (r"task manager", r"(show|go to) the desktop", r"clipboard history", r"emoji (panel|picker)",
                       r"(next|previous|new) desktop", r"minimi[sz]e everything", r"start menu", r"file explorer"),
    "list_windows": (r"what windows", r"which windows", r"windows (are|do i have) open", r"open windows",
                     r"what (is )?open"),
    "window_action": (r"switch to \w+", r"snap \w+", r"(maximi[sz]e|minimi[sz]e) \w+", r"bring .+ to the front",
                      r"(switch|go) to the \w+ window"),
    "close_window": (r"close (the |that |this )?(\w+ )?window",),
    "open_settings": (r"\w+ settings", r"open windows update"),
    "open_folder": (r"open (my |the )?(downloads|documents|desktop|pictures|music|videos|recycle bin)",),
    "read_aloud": (r"read (this|that|it) (to me|out)", r"out loud", r"read aloud", r"read .+ aloud"),
    "theme": (r"(dark|light) (mode|theme)",),
    "wifi": (r"wi fi", r"connect to \w+"),
    "calendar_upcoming": (r"on my calendar", r"any meetings"),
    "calendar_add_event": (r"to my calendar", r"schedule a"),
    "gmail_unread": (r"new (emails|mail)", r"my inbox"),
    "gmail_read": (r"read the email",),
    "gmail_find_address": (r"email address",),
    "gmail_send": (r"email \w+ that", r"send an email"),
    "gmail_draft": (r"draft an email",),
}

# after these tools, people usually go on to those ("find my resume" ... "open it")
FOLLOW_ON = {
    "find_files": "open_path read_file", "list_folder": "open_path read_file", "largest_files": "delete_item",
    "find_duplicates": "delete_item", "set_timer": "cancel_timer", "list_timers": "cancel_timer",
    "find_process": "end_process", "top_processes": "end_process", "chrome_tabs": "chrome_tab",
    "chrome_open": "chrome_page chrome_click", "list_movies": "play_movie", "play_movie": "vlc_control",
    "list_windows": "window_action close_window", "recycle_bin_status": "empty_recycle_bin",
    "list_startup_apps": "set_startup_app", "service_status": "service_control",
    "github_builds": "github_build_failure github_rerun_build", "github_notifications": "github_open_items",
    "read_notes": "take_note", "gmail_unread": "gmail_read", "gmail_find_address": "gmail_send gmail_draft",
    "list_memories": "forget",
}

GENERIC_NAME_WORDS = {"get", "set", "list", "read", "take", "run", "open", "find", "chrome", "action", "control",
                      "status", "info", "item", "items", "app", "apps", "to", "at", "write", "switch", "forget",
                      "look"}
NEGATION = {"dont", "not", "never"}
DESC_STOP = {"user", "users", "asks", "first", "when", "with", "into", "that", "your", "from", "like", "them",
             "then", "their", "each", "also", "used", "uses", "using", "gets", "says", "result", "results",
             "name", "names", "current", "currently", "else", "brief", "briefly", "always"}
WEB_ENDINGS = {"com", "org", "net", "io", "co", "uk", "dev", "ai", "tv", "gov", "edu"}
FILE_ENDINGS = {"txt", "pdf", "doc", "docx", "xls", "xlsx", "csv", "ppt", "pptx", "md", "py", "json", "zip", "png",
                "jpg", "jpeg", "mp3", "mp4", "mkv", "exe"}

# how much a matching word counts, by where the tool has it (then scaled down by how many tools share it)
NAME_W, SAID_W, TRIGGER_W, DESC_W = 3.0, 3.0, 1.5, 1.0
DESC_CAP = 2.0          # description words alone never carry a tool far
PHRASE_W = 3.0
NEAR = 0.85             # a misheard word counts a bit less than the word itself
PREVIOUS, AFTER_PREVIOUS = 2.5, 1.5
MIN_SCORE = 1.0         # with a name / SAID / phrase match
WEAK_MIN_SCORE = 2.0    # with only trigger and description words ("folder", "open"), shared by many tools
RELATIVE = 0.25         # drop tools scoring under this share of the best one


DESC, TRIGGER, STRONG = 0, 1, 2       # how a tool came to have a word: description, trigger word, name / SAID


_FIRST = {**dict.fromkeys("ckq", "k"), **dict.fromkeys("sz", "s"), **dict.fromkeys("fv", "f"),
          **dict.fromkeys("gj", "g"), **dict.fromkeys("aeiouy", "a")}


def _bucket(word: str) -> tuple[str, int]:
    """Speech-to-text keeps a word's first sound: "valume", "whether", "kontrol" are looked up only among
    keywords starting alike ("volume" is "v" or "f", "control" "c" or "k")."""
    return _FIRST.get(word[:1], word[:1]), len(word)


def _trigger_forms(trigger: str) -> list[str]:
    t = trigger.replace("\\b", "")
    return [t[:-2], t[:-2] + "s"] if t.endswith("s?") else [t]


def _description(tool, cfg) -> str:
    desc = tool.description(cfg) if callable(tool.description) else tool.description
    # "Never for stories...", "ONLY when the user says type": words a request must not be matched on
    keep = [s for s in re.split(r"(?<=[.!?])\s+", desc or "")
            if not re.search(r"\b(never|don't|doesn't|not|only)\b", s, re.I)]
    return " ".join(keep)


class _Index:
    """Every tool's keywords, built once per set of tools."""

    def __init__(self, toolbox):
        self.signature = tuple(toolbox.tools)
        self.order = {n: i for i, n in enumerate(toolbox.tools)}
        entries = []                                 # (stem, tool, weight, kind)
        phrase_rules = []                            # (pattern, tool, weight or None = trigger: scale by sharing)
        prefixes = []                                # (start of word, tool) for trigger words like "summar"
        # spoken keywords by first sound and length, for near-spellings
        self.vocab: dict[tuple[str, int], set[str]] = defaultdict(set)
        for name, tool in toolbox.tools.items():
            for w in name.split("_"):
                if w not in GENERIC_NAME_WORDS:
                    entries.append((stem(w), name, NAME_W, STRONG))
                    self.vocab[_bucket(w)].add(w)
            for w in SAID.get(name, "").split():
                entries.append((stem(w), name, SAID_W, STRONG))
                self.vocab[_bucket(w)].add(w)
            for p in PHRASES.get(name, ()):
                phrase_rules.append((re.compile(rf"\b(?:{p})\b"), name, PHRASE_W))
            for trigger in tool.triggers:
                for form in _trigger_forms(trigger):
                    words = tokens(form)
                    if len(words) == 1:
                        entries.append((stem(words[0]), name, TRIGGER_W, TRIGGER))
                        self.vocab[_bucket(words[0])].add(words[0])
                        if len(words[0]) >= 4:
                            prefixes.append((words[0], name))
                    elif words:
                        phrase_rules.append((re.compile(r"\b" + re.escape(" ".join(words))), name, None))
            for w in tokens(_description(tool, toolbox.cfg)):
                if len(w) >= 4 and w.isalpha() and w not in STOP and w not in DESC_STOP:
                    entries.append((stem(w), name, DESC_W, DESC))
        if "run_routine" in toolbox.tools:          # "good morning", "work mode"
            from .routines import routines_from

            for routine in routines_from(toolbox.cfg):
                phrase_rules.append((re.compile(r"\b" + re.escape(" ".join(tokens(routine))) + r"\b"),
                                     "run_routine", PHRASE_W))

        strong, every = defaultdict(set), defaultdict(set)   # tools that name / say a word; that have it at all
        for key, name, _, kind in entries:
            every[key].add(name)
            if kind == STRONG:
                strong[key].add(name)
        # word stem -> tool -> (weight, kind)
        self.words: dict[str, dict[str, tuple[float, int]]] = defaultdict(dict)
        for key, name, weight, kind in entries:
            w = weight / math.sqrt(len(strong[key]) if kind == STRONG else len(every[key]))
            if kind == TRIGGER and len(every[key]) == 1:
                kind = STRONG                        # a trigger word no other tool has ("cost", "spooler")
            if w > self.words[key].get(name, (0.0, DESC))[0]:
                self.words[key][name] = (w, kind)
        self.prefixes: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        for start, name in prefixes:
            self.prefixes[start[:4]].append((start, name, TRIGGER_W / math.sqrt(len(every[stem(start)]) or 1)))
        sharing = defaultdict(int)
        for pattern, _, weight in phrase_rules:
            sharing[pattern.pattern] += weight is None
        # (pattern, tool, weight, kind): PHRASES are strong evidence, and so is a trigger phrase only a tool or two
        # have ("bring up", "keep in mind"); a common one ("in my", "go to") is not
        self.phrases = [(p, n, w, STRONG) if w is not None else
                        (p, n, TRIGGER_W / math.sqrt(sharing[p.pattern]),
                         STRONG if sharing[p.pattern] <= 2 else TRIGGER)
                        for p, n, w in phrase_rules]
        self._near: dict[str, list[tuple[str, float]]] = {}

    def known(self, word: str) -> bool:
        """A tool has this word as a name, SAID or trigger word (not just in its description)."""
        return any(kind > DESC for _, kind in self.words.get(stem(word), {}).values())

    def near(self, word: str) -> list[tuple[str, float]]:
        """Keywords this word was probably meant to be ("valume" -> volume), best first."""
        if word not in self._near:
            found = []
            if len(word) >= 4 and word.isalpha() and word not in STOP:
                first = _bucket(word)[0]
                for n in range(len(word) - 2, len(word) + 3):
                    for k in self.vocab.get((first, n), ()):
                        sure = likeness(word, k)
                        if sure and stem(k) != stem(word):
                            found.append((k, sure))
            self._near[word] = sorted(found, key=lambda p: -p[1])[:2]
        return self._near[word]

    def hits(self, word: str) -> dict[str, tuple[float, int]]:
        """tool -> (how much this word counts for it, kind)."""
        found = dict(self.words.get(stem(word), {}))
        for start, name, w in self.prefixes.get(word[:4], ()):
            if word.startswith(start) and w > found.get(name, (0.0, DESC))[0]:
                found[name] = (w, TRIGGER)
        if not self.known(word):                     # no real match: maybe a misheard word
            for k, _ in self.near(word):
                for name, (w, kind) in self.words.get(stem(k), {}).items():
                    if w * NEAR > found.get(name, (0.0, DESC))[0]:
                        found[name] = (w * NEAR, kind)
        return found


def _index(toolbox) -> _Index:
    idx = getattr(toolbox, "_pick_index", None)
    if idx is None or idx.signature != tuple(toolbox.tools):
        idx = _Index(toolbox)
        try:
            toolbox._pick_index = idx
        except AttributeError:
            pass
    return idx


def _units(words: list[str], idx: _Index) -> list[str]:
    """The request's meaningful words, plus two-word pairs that make a known word ("screen shot", "wi fi")."""
    out = []
    for i, w in enumerate(words):
        if "." in w:                                 # github.com, notes.txt
            end = w.rsplit(".", 1)[1]
            out += ["website"] if end in WEB_ENDINGS else ["file"] if end in FILE_ENDINGS else []
        elif w in STOP or w.isdigit():
            continue
        elif i and words[i - 1] in NEGATION or words[max(0, i - 2):i] == ["do", "not"]:
            continue                                 # "don't forget ..." is no reason to offer forget
        else:
            out.append(w)
    out += [a + b for a, b in zip(words, words[1:]) if (a + b).isalpha() and idx.known(a + b)]
    return list(dict.fromkeys(out))


def _corrected(word: str, idx: _Index) -> str:
    if word in STOP or idx.known(word):
        return word
    near = idx.near(word)
    return near[0][0] if near else word


def _ranked(toolbox, request: str, previous=()) -> list[tuple[float, str, bool]]:
    """(score, tool, strong evidence) for every tool that fits the request and may run for it, best first."""
    idx = _index(toolbox)
    words = tokens(request)
    text = " ".join(words)
    # the same words with misheard ones put right ("set a tymer" -> "set a timer"), for the phrases
    fixed = " ".join(_corrected(w, idx) for w in words)
    total, desc_part, strong = defaultdict(float), defaultdict(float), set()
    for unit in _units(words, idx):
        for name, (w, kind) in idx.hits(unit).items():
            (desc_part if kind == DESC else total)[name] += w
            if kind == STRONG:
                strong.add(name)
    for name, w in desc_part.items():
        total[name] += min(DESC_CAP, w)
    for pattern, name, w, kind in idx.phrases:
        if pattern.search(text) or fixed != text and pattern.search(fixed):
            total[name] += w
            if kind == STRONG:
                strong.add(name)
    for name in previous:
        total[name] += PREVIOUS
        strong.add(name)
        for after in FOLLOW_ON.get(name, "").split():
            total[after] += AFTER_PREVIOUS
            strong.add(after)
    ranked = [(s, n, n in strong) for n, s in total.items()
              if n in toolbox.tools and s >= (MIN_SCORE if n in strong else WEAK_MIN_SCORE)
              and toolbox.tools[n].allowed_for(request)]
    return sorted(ranked, key=lambda r: (-r[0], idx.order[r[1]]))


def scores(toolbox, request: str, previous=()) -> dict[str, float]:
    """How well each offered-worthy tool fits the request (for tuning and tests)."""
    return {n: s for s, n, _ in _ranked(toolbox, request, previous)}


def pick(toolbox, request: str, previous: list[str] | tuple[str, ...] = (), max_tools: int = 16) -> list[str]:
    """Names of the tools worth offering for this request, best first."""
    ranked = _ranked(toolbox, request, previous)
    if not ranked:
        return []
    floor = RELATIVE * ranked[0][0]
    return [n for s, n, _ in ranked if s >= floor][:max_tools]


def estimate_tokens(*parts) -> int:
    """Rough token count of prompt pieces (JSON-ish text runs ~3.3 characters a token)."""
    chars = sum(len(p) if isinstance(p, str) else len(json.dumps(p, ensure_ascii=False)) for p in parts)
    return int(chars / 3.3)


def fit(provider, schemas: list[dict], num_ctx: int, reserve: int = 1024) -> list[dict]:
    """Drop the least relevant tools, then the oldest messages, until the prompt fits the context window with
    room left for the answer. Returns the tools to send."""
    if not num_ctx:
        return schemas
    limit = num_ctx - reserve
    tools = list(schemas)

    def size():
        return estimate_tokens(provider.system_prompt, [{"type": "function", "function": t} for t in tools],
                               provider.messages)

    def trim(keep: int):        # oldest exchanges first, never starting mid tool-call
        while len(provider.messages) > keep and size() > limit:
            provider.messages = provider.messages[1:]
            while provider.messages and provider.messages[0].get("role") != "user":
                provider.messages = provider.messages[1:]

    trim(4)                                  # old conversation goes before the tools this request needs
    while tools and size() > limit:
        tools.pop()
    trim(1)
    if len(tools) < len(schemas):
        log.warning("prompt too big for the %d-token context: dropped %d tools", num_ctx, len(schemas) - len(tools))
    return tools
