"""More Chrome by voice: bookmarks, history, hands-free scrolling, and the current site's own search box.

"Open my bank bookmark" and "open the page I was reading about rust" match loosely, so a misheard name still finds
the right page. "Keep scrolling" scrolls slowly until "stop scrolling" (or the wheel, or the end of the page).
All of these need the Jarvis Control extension, version 1.2 or newer.
"""
from __future__ import annotations

import datetime as dt
import re
import time
import urllib.parse

from .. import fuzzy
from .builtin import ToolFailed
from .chrome import SETUP_HINT, _call, _linked, chrome_enabled
from .registry import Spoken, tool

SEARCH_PAGES = re.compile(r"google\.[a-z.]+/search|bing\.com/search|duckduckgo\.com/\?|youtube\.com/results|"
                          r"/search\?|[?&](q|query|search_query)=")
_scrolling = {"url": ""}           # the page "keep scrolling" started on, so "stop" finds it after a tab switch


def _host(url: str) -> str:
    return urllib.parse.urlparse(url or "").netloc.removeprefix("www.")


def _title(item: dict) -> str:
    t = re.sub(r"\s*[-|–·]\s*(YouTube|Google Chrome|Google Search|Wikipedia)$", "", item.get("title") or "").strip()
    return t[:70] or _host(item.get("url", "")) or "that page"


KIND_WORDS = {"repo", "repository", "page", "site", "website", "link", "account", "portal", "login",
              "homepage", "dashboard", "app", "one", "thing", "tab"}


def _match(spoken: str, item: dict) -> float:
    """How well a spoken name fits a bookmark or page: its title, its site ('hbl'), or both. Words that only say
    what kind of page it is ('jarvis repo', 'bank login') don't count against it."""
    kept = " ".join(w for w in spoken.split() if w.lower() not in KIND_WORDS)
    spoken = kept or spoken
    host = _host(item.get("url", ""))
    site = host.rsplit(".", 2)[-2] if host.count(".") >= 1 else host
    s = max(fuzzy.score(spoken, item.get("title") or ""), fuzzy.score(spoken, site),
            fuzzy.score(spoken, f"{site} {item.get('title') or ''}"))
    if item.get("folder") and fuzzy.score(spoken, item["folder"]) >= 0.9:
        s = max(s, 0.8)
    return s


def _need_link(ctx):
    if not _linked(ctx):
        raise ToolFailed(SETUP_HINT)


# ------------------------------------------------------------------ bookmarks

@tool("Chrome bookmarks: open one by name (a rough or misheard name is fine), bookmark the current page, read out "
      "the bookmarks, or open the bookmarks manager.",
      {"action": {"type": "string", "enum": ["open", "add", "list", "manager"]},
       "name": {"type": "string", "description": "For open: the bookmark's name or site, e.g. 'bank', 'jarvis repo'"}},
      required=["action"], enabled=chrome_enabled, triggers=("bookmark", "bookmarks", "favorite", "favourite"))
def chrome_bookmarks(action: str, name: str = "", ctx=None):
    _need_link(ctx)
    if action == "add":
        r = _call(ctx, "bookmarks", {"action": "add"})
        return Spoken(f"Bookmarked {_title(r)}." if r.get("added") else f"It's already bookmarked as {_title(r)}.")
    if action == "manager":
        _call(ctx, "open", {"url": "chrome://bookmarks"})
        return Spoken("Here are your bookmarks.")
    marks = _call(ctx, "bookmarks", {"action": "list"}) or []
    if not marks:
        return Spoken("You don't have any bookmarks yet. Say bookmark this page to add one.")
    if action == "list":
        latest = sorted(marks, key=lambda m: -m.get("added", 0))[:6]
        names = ", ".join(_title(m) for m in latest)
        return Spoken(f"You have {len(marks)} bookmark{'s' * (len(marks) != 1)}. The newest: {names}.")
    if not name.strip():
        raise ToolFailed("Which bookmark?")
    top = sorted(((_match(name, m), m) for m in marks), key=lambda p: -p[0])
    if top[0][0] < 0.72:
        near = ", ".join(_title(m) for s, m in top[:3] if s >= 0.5)
        raise ToolFailed(f"I don't see a bookmark for {name}." + (f" Close ones: {near}." if near else ""))
    pick = top[0][1]
    _call(ctx, "open", {"url": pick["url"]})
    return Spoken(f"Opening {_title(pick)}.")


# ------------------------------------------------------------------ history

def _window(when: str) -> tuple[int, int | None, str]:
    """'today' / 'yesterday' / 'this week' -> (since_ms, until_ms, how to say it)."""
    now = dt.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ms = lambda d: int(d.timestamp() * 1000)  # noqa: E731
    w = (when or "").lower().strip()
    if w == "yesterday":
        return ms(midnight - dt.timedelta(days=1)), ms(midnight), "yesterday"
    if w in ("week", "this week", "last week", "lately", "recently"):
        return ms(now - dt.timedelta(days=7)), None, "this week"
    if w in ("this morning",):
        return ms(midnight), ms(midnight.replace(hour=12)), "this morning"
    return ms(midnight), None, "today"


@tool("Chrome history: reopen a page you visited by what it was about (a rough or misheard description is fine), "
      "say which sites you visited today / yesterday / this week, or open the history page.",
      {"action": {"type": "string", "enum": ["open", "recent", "manager"]},
       "query": {"type": "string", "description": "For open: what the page was about, e.g. 'rust ownership'"},
       "when": {"type": "string", "description": "today, yesterday or this week (default: any time for open, "
                                                 "today for recent)"}},
      required=["action"], enabled=chrome_enabled, triggers=("history", "visited", "was reading", "was watching",
                                                             "was on", "earlier"))
def chrome_history(action: str, query: str = "", when: str = "", ctx=None):
    _need_link(ctx)
    if action == "manager":
        _call(ctx, "open", {"url": "chrome://history"})
        return Spoken("Here's your history.")
    if action == "recent":
        since, until, said = _window(when)
        items = _call(ctx, "history", {"since": since, "until": until, "max": 2000}) or []
        if not items:
            return Spoken(f"I don't see any browsing {said}.")
        sites: dict[str, int] = {}
        for h in items:
            host = _host(h["url"])
            sites[host] = sites.get(host, 0) + max(1, h.get("visits", 1))
        top = sorted(sites, key=lambda k: -sites[k])[:3]
        latest = max(items, key=lambda h: h.get("last", 0))
        named = top[0] if len(top) == 1 else ", ".join(top[:-1]) + " and " + top[-1]
        return Spoken(f"{said.capitalize()} you visited {len(sites)} site{'s' * (len(sites) != 1)}, mostly "
                      f"{named}. The latest was {_title(latest)}.")
    if not query.strip():
        raise ToolFailed("What was the page about?")
    since, until = None, None
    if when:
        since, until, _ = _window(when)
    window = {"since": since, "until": until} if since else {"days": 90}
    # Chrome's own search first (exact words), then a loose match over recent pages for misheard words
    found = _call(ctx, "history", {"text": query, "max": 300, **window}) or []
    pool = found or _call(ctx, "history", {"text": "", "max": 3000, **(window if since else {"days": 30})}) or []
    newest = max((h.get("last", 0) for h in pool), default=0) or 1

    def rank(h: dict) -> float:
        s = _match(query, h)
        if SEARCH_PAGES.search(h["url"]):
            s -= 0.12                                   # the page itself, not the search that found it
        return s + 0.03 * (h.get("last", 0) / newest) + 0.01 * min(h.get("visits", 0), 5)

    ranked = sorted(pool, key=rank, reverse=True)
    if not ranked or (not found and _match(query, ranked[0]) < 0.72) or (found and rank(ranked[0]) < 0.45):
        raise ToolFailed(f"I can't find a page about {query} in your history.")
    pick = ranked[0]
    _call(ctx, "open", {"url": pick["url"]})
    ago = _ago(pick.get("last", 0))
    return Spoken(f"Opening {_title(pick)}" + (f", from {ago}." if ago else "."))


def _ago(ms: int) -> str:
    if not ms:
        return ""
    then = dt.datetime.fromtimestamp(ms / 1000)
    days = (dt.date.today() - then.date()).days
    if days == 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return then.strftime("%A")
    return f"{days} days ago"


# ------------------------------------------------------------------ hands-free scrolling

@tool("Scroll the current Chrome page slowly by itself (for reading hands-free): start, faster, slower or stop.",
      {"action": {"type": "string", "enum": ["start", "faster", "slower", "stop"]}},
      required=["action"], enabled=chrome_enabled, triggers=("scroll", "scrolling"))
def chrome_autoscroll(action: str, ctx=None):
    _need_link(ctx)
    r = _call(ctx, "run", {"fn": "autoscroll", "args": {"action": action}})
    if action == "stop":
        if not r.get("was") and _scrolling["url"]:
            # it was started on another tab
            tab = next((t for t in _call(ctx, "tabs") or [] if t.get("url") == _scrolling["url"]), None)
            if tab:
                r = _call(ctx, "run", {"fn": "autoscroll", "args": {"action": "stop"}, "tabId": tab["id"]})
        _scrolling["url"] = ""
        return Spoken("Stopped scrolling." if r.get("was") else "I wasn't scrolling.")
    tabs = _call(ctx, "tabs") or []
    active = [t for t in tabs if t.get("active")]
    if active:
        _scrolling["url"] = active[0].get("url", "")
    if action == "start":
        return Spoken("Scrolling. Say faster, slower, or stop scrolling.")
    return Spoken("Faster." if action == "faster" else "Slower.")


# ------------------------------------------------------------------ the site's own search box

@tool("Search the site that's open in Chrome with its own search box (a shop, a forum, docs). Falls back to "
      "a Google search of that site.",
      {"query": {"type": "string"}}, required=["query"], enabled=chrome_enabled,
      triggers=("search this site", "search the site", "this website", "this site"))
def search_this_site(query: str, ctx=None):
    _need_link(ctx)
    r = _call(ctx, "run", {"fn": "sitesearch", "args": {"text": query}})
    host = r.get("host", "")
    if r.get("ok"):
        time.sleep(0.2)
        return Spoken(f"Searching {host.removeprefix('www.') or 'this site'} for {query}.")
    if not host:
        raise ToolFailed(f"I couldn't: {r.get('reason')}.")
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(f"site:{host} {query}")
    _call(ctx, "open", {"url": url})
    return Spoken(f"{host} has no search box I can use, so I searched it on Google.")


# ------------------------------------------------------------------ fast-path phrases

YOUTUBE = r"(?: on youtube)?"


def route_browse(t: str, has) -> list[tuple[str, dict]] | None:
    if has("chrome_video"):
        if re.fullmatch(r"skip (?:the |this |these )?(?:youtube |video )?(?:ads?|advert(?:isement)?s?|commercials?)", t):
            return [("chrome_video", {"action": "skip_ad"})]
        m = re.fullmatch(r"(?:turn|switch|put) (?P<a>on|off) (?:the )?(?:closed )?(?:captions|subtitles|cc)|"
                         r"(?:turn|switch|put) (?:the )?(?:captions|subtitles|cc) (?P<b>on|off)|"
                         r"(?P<c>enable|disable|show|hide) (?:the )?(?:captions|subtitles|cc)|"
                         r"(?:captions|subtitles) (?P<d>on|off)", t)
        if m:
            on = (m.group("a") or m.group("b") or m.group("d") or m.group("c")) in ("on", "enable", "show")
            return [("chrome_video", {"action": "captions_on" if on else "captions_off"})]
        m = re.fullmatch(r"(?P<a>like|unlike) (?:this|the) video" + YOUTUBE + r"|(?:give (?:this|it) a )?thumbs up" +
                         YOUTUBE + r"|remove (?:my |the )?like" + YOUTUBE, t)
        if m:
            return [("chrome_video", {"action": "unlike" if m.group("a") == "unlike" or t.startswith("remove")
                                      else "like"})]
        if re.fullmatch(r"subscribe(?: to)?(?: (?:this|the|his|her|their) channel)?" + YOUTUBE, t):
            return [("chrome_video", {"action": "subscribe"})]
        if re.fullmatch(r"(?:(?:turn on|turn off|switch to|enter|exit|leave|use|toggle) )?theat(?:er|re) mode"
                        r"(?: on| off)?", t):
            return [("chrome_video", {"action": "theater"})]
    if has("chrome_bookmarks"):
        if re.fullmatch(r"(?:bookmark|save) (?:this|the) (?:page|site|website|tab|link)(?: as a bookmark| to (?:my )?"
                        r"bookmarks)?|add (?:this|the) (?:page|site|website|tab) to (?:my )?bookmarks|bookmark (?:it|this)", t):
            return [("chrome_bookmarks", {"action": "add"})]
        if re.fullmatch(r"(?:list|read|tell me|what are)(?: me)? (?:all )?(?:of )?my bookmarks|what bookmarks do i have", t):
            return [("chrome_bookmarks", {"action": "list"})]
        if re.fullmatch(r"(?:open|show)(?: me)? (?:my |the )?bookmarks(?: page| manager)?", t):
            return [("chrome_bookmarks", {"action": "manager"})]
        m = re.fullmatch(r"(?:open|go to|show me|take me to|load) (?:my |the )?(?P<n>.+?) bookmark", t) \
            or re.fullmatch(r"(?:open|go to|load) (?:the |my )?bookmark (?:for |called |named )?(?P<n>.+)", t) \
            or re.fullmatch(r"(?:open|go to) (?P<n>.+?) from (?:my )?bookmarks", t)
        if m:
            return [("chrome_bookmarks", {"action": "open", "name": m.group("n")})]
    if has("chrome_history"):
        when = r"(?: (?P<w>today|yesterday|earlier(?: today)?|this morning|this week|last week))?"
        m = re.fullmatch(r"(?:open|reopen|find|go back to|take me (?:back )?to|bring up|pull up) (?:the |that )?"
                         r"(?:page|site|website|article|video|link|tab|thing|post|recipe|doc|document) (?:that )?i "
                         r"(?:was |were )?(?:reading|watching|looking at|on|visited|opened|saw|read|watched|had open)"
                         + when + r" (?:about|on|for|with|called|titled|named) (?P<q>.+)", t)
        if m:
            w = (m.group("w") or "").replace("earlier today", "today").replace("earlier", "today")
            return [("chrome_history", {"action": "open", "query": m.group("q"), **({"when": w} if w else {})})]
        m = re.fullmatch(r"(?:search|look in|check|look through) (?:my )?(?:browser |chrome |browsing )?history "
                         r"for (?P<q>.+)|find (?P<r>.+?) in my (?:browser |chrome |browsing )?history", t)
        if m:
            return [("chrome_history", {"action": "open", "query": m.group("q") or m.group("r")})]
        m = re.fullmatch(r"what (?:sites|websites|pages) (?:did|have) i (?:visit|visited|open|opened|go to|gone to|"
                         r"been on|been to|browse|browsed)(?: (?P<w>today|yesterday|this week|recently|lately))?", t)
        if m:
            return [("chrome_history", {"action": "recent", "when": m.group("w") or "today"})]
        if re.fullmatch(r"(?:open|show)(?: me)? (?:my |the )?(?:browser |chrome |browsing )?history", t):
            return [("chrome_history", {"action": "manager"})]
    if has("chrome_autoscroll"):
        if re.fullmatch(r"(?:start |begin )?(?:auto ?scroll(?:ing)?|scrolling automatically|keep scrolling(?: down)?|"
                        r"scroll(?: down)? (?:automatically|slowly|by itself|for me)|slowly scroll(?: down)?)"
                        r"(?: (?:the|this) page)?", t):
            return [("chrome_autoscroll", {"action": "start"})]
        if re.fullmatch(r"scroll (?:a bit |a little )?faster|faster scrolling|speed up(?: the)? scroll(?:ing)?", t):
            return [("chrome_autoscroll", {"action": "faster"})]
        if re.fullmatch(r"scroll (?:a bit |a little )?slower|slower scrolling|slow down(?: the)? scroll(?:ing)?", t):
            return [("chrome_autoscroll", {"action": "slower"})]
        if re.fullmatch(r"(?:stop|pause|end|quit) (?:the )?(?:auto ?)?scroll(?:ing)?", t):
            return [("chrome_autoscroll", {"action": "stop"})]
    if has("search_this_site"):
        m = re.fullmatch(r"search (?:this|the) (?:site|website|shop|store) for (?P<q>.+)|"
                         r"search for (?P<r>.+?) on (?:this|the) (?:site|website|shop|store)|"
                         r"(?:search|look) (?:in )?here for (?P<s>.+)", t)
        if m:
            return [("search_this_site", {"query": m.group("q") or m.group("r") or m.group("s")})]
    if has("chrome_find"):
        m = re.fullmatch(r"search (?:this|the) page for (?P<q>.+)", t)
        if m:
            return [("chrome_find", {"text": m.group("q")})]
    return None

