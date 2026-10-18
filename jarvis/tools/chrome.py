"""Chrome by voice: tabs, pages, videos, clicking and typing, and Claude on claude.ai.

Full control goes through the Jarvis Control extension (extension/chrome, set up with
`jarvis.bat --chrome-setup`). Without it, the basics (new/close/next tab, back, reload, scroll) still work
with keyboard shortcuts sent to the Chrome window.
"""
from __future__ import annotations

import difflib
import re
import time
import urllib.parse
import webbrowser

from .builtin import ToolFailed
from .registry import Spoken, tool

CLAUDE_URL = "https://claude.ai/new"
SETUP_HINT = "Chrome isn't linked to me yet. Run jarvis.bat --chrome-setup once, then try again."
TAB_WORDS = ("tab", "chrome", "browser", "page", "website", "site", "window")
RISKY_CLICK = re.compile(r"\b(buy|pay|purchase|order|checkout|check out|delete|remove|send|post|publish|submit|"
                         r"confirm|subscribe|unsubscribe|transfer|sign out|log out|logout|place)\b", re.I)
SITES = {"youtube": "youtube.com", "gmail": "mail.google.com", "google": "google.com", "github": "github.com",
         "claude": "claude.ai", "chatgpt": "chatgpt.com", "netflix": "netflix.com", "spotify": "open.spotify.com",
         "whatsapp": "web.whatsapp.com", "twitter": "x.com", "x": "x.com", "reddit": "reddit.com",
         "facebook": "facebook.com", "instagram": "instagram.com", "linkedin": "linkedin.com",
         "maps": "maps.google.com", "google maps": "maps.google.com", "drive": "drive.google.com",
         "calendar": "calendar.google.com", "amazon": "amazon.com", "wikipedia": "wikipedia.org"}


def chrome_enabled(cfg) -> bool:
    return (cfg.get("chrome") or {}).get("enabled", True) if cfg is not None else True


def _bridge(ctx):
    from .. import chrome_bridge

    return chrome_bridge.get(ctx.cfg if ctx else None)


def _call(ctx, cmd: str, args: dict | None = None, timeout: float = 15):
    from ..chrome_bridge import NotConnected

    try:
        return _bridge(ctx).call(cmd, args, timeout)
    except NotConnected:
        raise ToolFailed(SETUP_HINT) from None
    except (RuntimeError, TimeoutError) as e:
        raise ToolFailed(str(e)) from None


def _linked(ctx) -> bool:
    try:
        return _bridge(ctx).connected
    except Exception:  # noqa: BLE001  (websockets missing, port busy)
        return False


def _keys(combo: str):
    """Keyboard fallback: bring Chrome to the front and press a shortcut."""
    from .windows import _focus, find_window, send_keys

    hit = find_window("chrome")
    if not hit:
        raise ToolFailed("Chrome isn't open.")
    _focus(hit[0])
    time.sleep(0.15)
    send_keys(combo)


def _url(target: str) -> str:
    t = target.strip()
    key = t.lower().removeprefix("the ").removesuffix(" website").removesuffix(" site").strip()
    if key in SITES:
        return "https://" + SITES[key]
    if re.match(r"https?://", t):
        return t
    if re.fullmatch(r"(localhost|[\w-]+(\.[\w-]+)+)(:\d+)?(/\S*)?", t.replace(" ", "")):
        local = re.match(r"(localhost|\d+\.\d+\.\d+\.\d+)\b", t)   # a dev server on this PC or the LAN
        return ("http://" if local else "https://") + t.replace(" ", "")
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(t)


def _site(url: str) -> str:
    host = urllib.parse.urlparse(url or "").netloc.removeprefix("www.")
    return host.split(".")[0] if host else ""


def _tab_label(t: dict) -> str:
    title = re.sub(r"\s*[-|–]\s*(YouTube|Google Chrome|Gmail)$", "", t.get("title") or "").strip()
    return title[:60] or _site(t.get("url", "")) or "a blank tab"


def find_tab(tabs: list[dict], name: str) -> dict | None:
    """Best tab for a spoken name: 'youtube', 'the github one', 'my email', 'the second tab'."""
    want = re.sub(r"\b(the|my|tab|one|page|website|site)\b", " ", name.lower()).strip()
    want = {"email": "gmail", "mail": "gmail", "e-mail": "gmail"}.get(want, want)
    ordinals = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "last": -1}
    if want in ordinals and tabs:
        return tabs[ordinals[want]]
    if not want:
        return None
    best, score = None, 0.0
    for t in tabs:
        hay = f"{t.get('title', '')} {t.get('url', '')}".lower()
        s = 1.0 if want in hay else max(
            [difflib.SequenceMatcher(None, want, w).ratio() for w in re.findall(r"[a-z0-9]+", hay)] or [0])
        if s > score:
            best, score = t, s
    return best if score >= 0.75 else None


# ------------------------------------------------------------------ tabs

@tool("List the tabs open in Chrome.", enabled=chrome_enabled, triggers=TAB_WORDS)
def chrome_tabs(ctx=None):
    tabs = _call(ctx, "tabs")
    if not tabs:
        return Spoken("Chrome has no tabs open.")
    names = [_tab_label(t) + (" (playing)" if t.get("audible") else "") for t in tabs[:10]]
    more = f", and {len(tabs) - 10} more" if len(tabs) > 10 else ""
    return Spoken(f"You have {len(tabs)} tab{'s' * (len(tabs) != 1)} open: {'; '.join(names)}{more}.")


@tool("Chrome tabs: switch to a tab by name, close one (by name, or 'this' for the current tab), close all other "
      "tabs, open a new tab, go to the next/previous tab, or reopen the last closed tab.",
      {"action": {"type": "string", "enum": ["switch", "close", "close_others", "new", "next", "previous", "reopen"]},
       "name": {"type": "string", "description": "Which tab: part of its title or site ('youtube'), 'this', "
                                                 "'second', 'last'. Empty for new/next/previous/reopen."}},
      required=["action"], enabled=chrome_enabled, triggers=TAB_WORDS)
def chrome_tab(action: str, name: str = "", ctx=None):
    if not _linked(ctx):
        combos = {"new": "ctrl+t", "next": "ctrl+tab", "previous": "ctrl+shift+tab", "reopen": "ctrl+shift+t"}
        if action in combos or (action == "close" and name.lower() in ("", "this", "current", "this tab")):
            _keys(combos.get(action, "ctrl+w"))
            return Spoken({"new": "New tab open.", "next": "Next tab.", "previous": "Previous tab.",
                           "reopen": "Reopened the last tab you closed."}.get(action, "Closed this tab."))
        raise ToolFailed(SETUP_HINT)
    tabs = _call(ctx, "tabs")
    current = next((t for t in tabs if t.get("active")), tabs[0] if tabs else None)
    if action == "new":
        _call(ctx, "open", {"url": "chrome://newtab"})
        return Spoken("New tab open.")
    if action == "reopen":
        t = _call(ctx, "reopen")
        return Spoken(f"Reopened {_tab_label(t)}." if t else "There's no closed tab to bring back.")
    if action in ("next", "previous"):
        same = [t for t in tabs if current and t["windowId"] == current["windowId"]]
        i = next((k for k, t in enumerate(same) if t["id"] == current["id"]), 0)
        t = same[(i + (1 if action == "next" else -1)) % len(same)]
        _call(ctx, "activate", {"tabId": t["id"]})
        return Spoken(f"On {_tab_label(t)}.")
    if action == "close_others":
        others = [t["id"] for t in tabs if current and t["id"] != current["id"] and not t.get("pinned")]
        if others:
            _call(ctx, "close", {"tabIds": others})
        return Spoken(f"Closed {len(others)} other tab{'s' * (len(others) != 1)}. Say reopen tab to bring one back.")
    target = current if name.lower().strip() in ("", "this", "current", "this tab", "it") else find_tab(tabs, name)
    if not target:
        raise ToolFailed(f"I don't see a tab for {name}. Ask me to list your tabs.")
    if action == "close":
        _call(ctx, "close", {"tabIds": [target["id"]]})
        return Spoken(f"Closed {_tab_label(target)}.")
    _call(ctx, "activate", {"tabId": target["id"]})
    return Spoken(f"Switched to {_tab_label(target)}.")


@tool("Open a website or search in Chrome: a site name ('youtube'), an address, or words to search for.",
      {"target": {"type": "string", "description": "e.g. 'youtube', 'github.com/theycallmeinsane', 'weather in Lahore'"},
       "new_tab": {"type": "boolean", "description": "Default true; false replaces the current tab"}},
      enabled=chrome_enabled, triggers=("open", "go to", "visit", "chrome", "tab", "browser", "search", "pull up", "load"))
def chrome_open(target: str, new_tab: bool = True, ctx=None):
    url = _url(target)
    if not _linked(ctx):
        webbrowser.open(url)
        return Spoken(f"Opened {target}.")
    _call(ctx, "open", {"url": url, "newTab": new_tab})
    return Spoken(f"Opened {target}.")


PAGE_ACTIONS = ["back", "forward", "reload", "scroll_down", "scroll_up", "top", "bottom", "zoom_in", "zoom_out",
                "zoom_reset", "mute_tab", "unmute_tab", "pin", "unpin", "duplicate", "new_window"]
PAGE_KEYS = {"back": "alt+left", "forward": "alt+right", "reload": "f5", "scroll_down": "pagedown",
             "scroll_up": "pageup", "top": "home", "bottom": "end", "zoom_in": "ctrl+=", "zoom_out": "ctrl+-",
             "zoom_reset": "ctrl+0", "new_window": "ctrl+n"}


@tool("Control the current Chrome page: back, forward, reload, scroll, zoom, mute or pin the tab, new window.",
      {"action": {"type": "string", "enum": PAGE_ACTIONS}}, enabled=chrome_enabled,
      triggers=TAB_WORDS + ("back", "forward", "reload", "refresh", "scroll", "zoom", "mute", "pin", "duplicate"))
def chrome_page(action: str, ctx=None):
    said = action.replace("_", " ")
    if not _linked(ctx):
        if action not in PAGE_KEYS:
            raise ToolFailed(SETUP_HINT)
        _keys(PAGE_KEYS[action])
        return Spoken("Done.")
    if action.startswith("scroll") or action in ("top", "bottom"):
        _call(ctx, "run", {"fn": "scroll", "args": {"to": action.removeprefix("scroll_")}})
        return Spoken("Done.")
    nav = {"mute_tab": "mute", "unmute_tab": "unmute"}.get(action, action)
    r = _call(ctx, "nav", {"action": nav})
    if action.startswith("zoom"):
        return Spoken(f"Zoom is {r}%.")
    return Spoken({"back": "Gone back.", "forward": "Gone forward.", "reload": "Reloaded."}.get(action, f"Done: {said}."))


@tool("Read the current Chrome page (or what's selected on it) so you can summarize it or answer questions about it.",
      {"what": {"type": "string", "enum": ["page", "selection"], "description": "default page"}},
      required=[], enabled=chrome_enabled,
      triggers=TAB_WORDS + ("read", "summar", "article", "what does", "what's on", "this", "screen"))
def chrome_read(what: str = "page", ctx=None):
    r = _call(ctx, "run", {"fn": "read", "args": {"maxChars": 8000}})
    text = r.get("selection") if what == "selection" and r.get("selection") else r.get("text", "")
    if not text.strip():
        raise ToolFailed("That page has no readable text.")
    return f"Page '{r.get('title')}' ({r.get('url')}):\n{text}"


def _click_question(args: dict) -> str:
    return f"Shall I click {args.get('text', 'that')}?"


@tool("Click a link or button on the current Chrome page by its text, e.g. 'sign in', 'next', 'the first video'.",
      {"text": {"type": "string", "description": "The link or button's text"},
       "index": {"type": "integer", "description": "Which match, when several have that text (0 = first)"}},
      enabled=chrome_enabled, triggers=("click", "press", "tap", "hit", "select", "choose", "open the"))
def chrome_click(text: str, index: int = 0, ctx=None):
    confirm = ctx.context.get("confirm") if ctx else None
    if RISKY_CLICK.search(text) and (not confirm or not confirm(_click_question({"text": text}))):
        return Spoken("Okay, I didn't click it.")
    r = _call(ctx, "run", {"fn": "click", "args": {"text": text, "index": index}})
    if not r.get("clicked"):
        options = ", ".join(r.get("options", [])[:8])
        raise ToolFailed(f"I can't find {text} on this page." + (f" I can see: {options}." if options else ""))
    return Spoken(f"Clicked {r['clicked']}.")


@tool("Type text into a box on the current Chrome page (the focused box, or one named like 'search'), "
      "optionally pressing Enter. Never used for passwords or card numbers.",
      {"text": {"type": "string"}, "field": {"type": "string", "description": "Which box, e.g. 'search', 'email'"},
       "submit": {"type": "boolean", "description": "Press Enter afterwards"}},
      required=["text"], enabled=chrome_enabled, triggers=("type", "write", "enter", "fill", "search", "put"))
def chrome_type(text: str, field: str = "", submit: bool = False, ctx=None):
    r = _call(ctx, "run", {"fn": "type", "args": {"text": text, "field": field, "submit": submit}})
    if not r.get("typed"):
        raise ToolFailed(f"I didn't type it: {r.get('reason', 'no text box found')}.")
    return Spoken(f"Typed it{' and pressed Enter' if submit else ''}.")


@tool("Control the video on the current Chrome tab (YouTube etc.): play, pause, skip forward or back some seconds, "
      "restart, next video, speed, full screen, mute, or status.",
      {"action": {"type": "string", "enum": ["play", "pause", "toggle", "forward", "back", "restart", "next",
                                             "speed", "fullscreen", "mute", "unmute", "status"]},
       "seconds": {"type": "integer", "description": "For forward/back (default 10)"},
       "rate": {"type": "number", "description": "For speed, e.g. 1.5"}},
      required=["action"], enabled=chrome_enabled,
      triggers=("video", "youtube", "skip", "rewind", "forward", "speed", "full screen", "fullscreen", "seconds"))
def chrome_video(action: str, seconds: int = 10, rate: float = 1.0, ctx=None):
    r = _call(ctx, "run", {"fn": "media", "args": {"action": action, "seconds": seconds, "rate": rate}})
    if not r.get("ok"):
        raise ToolFailed(f"I couldn't: {r.get('reason')}.")
    if action == "status":
        m, s = divmod(r.get("time", 0), 60)
        return Spoken(f"{'Paused' if r['paused'] else 'Playing'} at {m}:{s:02d} of {r['duration'] // 60} minutes.")
    return Spoken({"forward": f"Skipped {seconds} seconds.", "back": f"Went back {seconds} seconds.",
                   "speed": f"Speed {r.get('rate')}x."}.get(action, "Done."))


# ------------------------------------------------------------------ Claude (claude.ai in Chrome)

def _claude_tab(ctx, new_chat: bool) -> dict:
    tabs = _call(ctx, "tabs")
    host = urllib.parse.urlparse(CLAUDE_URL).netloc
    tab = next((t for t in tabs if host in (t.get("url") or "")), None)
    if tab is None or new_chat:
        url = CLAUDE_URL
        if tab is None:
            tab = _call(ctx, "open", {"url": url, "wait": True}, timeout=25)
        else:
            _call(ctx, "activate", {"tabId": tab["id"]})
            tab = _call(ctx, "open", {"url": url, "newTab": False, "wait": True}, timeout=25)
    else:
        _call(ctx, "activate", {"tabId": tab["id"]})
    for _ in range(20):
        st = _call(ctx, "run", {"fn": "claudeState", "tabId": tab["id"]})
        if st.get("signedOut"):
            raise ToolFailed("Claude in Chrome is signed out. Sign in at claude.ai, then ask me again.")
        if st.get("ready"):
            return tab
        time.sleep(0.5)
    raise ToolFailed("Claude's page didn't finish loading.")


def _wait_answer(ctx, tab_id: int, before: int, limit: float) -> str:
    deadline, last, stable = time.monotonic() + limit, None, 0
    while time.monotonic() < deadline:
        time.sleep(1.0)
        st = _call(ctx, "run", {"fn": "claudeState", "tabId": tab_id})
        text = st.get("text", "")
        if st.get("count", 0) > before and text and not st.get("streaming"):
            stable = stable + 1 if text == last else 0
            if stable >= 1:
                return text
        last = text
    return ""


@tool("Use Claude (claude.ai in Chrome): ask it something and hear its answer, read its last answer, stop it "
      "writing, or just open it. For quick questions you answer yourself, don't use this.",
      {"action": {"type": "string", "enum": ["ask", "read_last", "stop", "open"]},
       "prompt": {"type": "string", "description": "What to send Claude, in the user's words"},
       "new_chat": {"type": "boolean", "description": "Start a fresh chat (default true; false continues the open one)"}},
      required=["action"], enabled=chrome_enabled, triggers=("claude", "cloud", "clawed", "clod"))
def claude_chat(action: str, prompt: str = "", new_chat: bool = True, ctx=None):
    cfg = (ctx.cfg.get("chrome") or {}) if ctx else {}
    if not _linked(ctx):
        if action == "ask" and prompt:
            webbrowser.open(CLAUDE_URL + "?q=" + urllib.parse.quote(prompt))
            return Spoken("I've opened Claude with your question. " + SETUP_HINT.split(". ", 1)[0] +
                          ", so I can't read its answer back yet.")
        if action == "open":
            webbrowser.open(CLAUDE_URL)
            return Spoken("Claude is open.")
        raise ToolFailed(SETUP_HINT)
    if action == "open":
        _claude_tab(ctx, new_chat=False)
        return Spoken("Claude is open.")
    if action == "stop":
        tab = _claude_tab(ctx, new_chat=False)
        r = _call(ctx, "run", {"fn": "claudeStop", "tabId": tab["id"]})
        return Spoken("Stopped Claude." if r.get("stopped") else "Claude isn't writing anything right now.")
    if action == "read_last":
        tab = _claude_tab(ctx, new_chat=False)
        text = _call(ctx, "run", {"fn": "claudeState", "tabId": tab["id"]}).get("text", "")
        if not text:
            raise ToolFailed("There's no answer from Claude on the page yet.")
        return Spoken("Claude's last answer: " + _speakable(text, limit=2500))
    if not prompt.strip():
        raise ToolFailed("What should I ask Claude?")
    tab = _claude_tab(ctx, new_chat)
    before = _call(ctx, "run", {"fn": "claudeState", "tabId": tab["id"]}).get("count", 0)
    r = _call(ctx, "run", {"fn": "claudeSend", "tabId": tab["id"], "args": {"text": prompt}})
    if not r.get("sent"):
        raise ToolFailed(f"I couldn't send it: {r.get('reason')}.")
    answer = _wait_answer(ctx, tab["id"], before, float(cfg.get("claude_wait_seconds", 90)))
    if not answer:
        return Spoken("I've sent it to Claude. It's still writing; ask me to read Claude's answer when it's done.")
    return Spoken("Claude says: " + _speakable(answer))


def _speakable(answer: str, limit: int = 600) -> str:
    """Short answers whole; long ones (code, essays) as their opening, with the rest left on screen."""
    text = re.sub(r"```.*?```", " (code on screen) ", answer, flags=re.S)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[:end + 1] if end > 150 else cut.rsplit(" ", 1)[0] + "...") + " The full answer is on screen in Claude."
