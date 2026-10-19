"""Surfing by voice: search a named site, numbered links and buttons ("show links", "click 7"), search results
("read the results", "open the second result"), find text on the page, and a random website when you're bored.

Searching and random sites work without the Chrome extension (they just open a URL); links, results and find need
the Jarvis Control extension.
"""
from __future__ import annotations

import random
import re
import urllib.parse
import webbrowser

from .. import fuzzy
from .builtin import ToolFailed
from .chrome import RISKY_CLICK, SETUP_HINT, TAB_WORDS, _call, _linked, chrome_enabled
from .registry import Spoken, tool

SEARCH = {
    "google": "https://www.google.com/search?q={}",
    "images": "https://www.google.com/search?tbm=isch&q={}",
    "google images": "https://www.google.com/search?tbm=isch&q={}",
    "news": "https://news.google.com/search?q={}",
    "google news": "https://news.google.com/search?q={}",
    "maps": "https://www.google.com/maps/search/{}",
    "google maps": "https://www.google.com/maps/search/{}",
    "youtube": "https://www.youtube.com/results?search_query={}",
    "amazon": "https://www.amazon.com/s?k={}",
    "daraz": "https://www.daraz.pk/catalog/?q={}",
    "ebay": "https://www.ebay.com/sch/i.html?_nkw={}",
    "aliexpress": "https://www.aliexpress.com/w/wholesale-{}.html",
    "reddit": "https://www.reddit.com/search/?q={}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={}",
    "github": "https://github.com/search?q={}",
    "stack overflow": "https://stackoverflow.com/search?q={}",
    "twitter": "https://x.com/search?q={}",
    "x": "https://x.com/search?q={}",
    "instagram": "https://www.instagram.com/explore/search/keyword/?q={}",
    "facebook": "https://www.facebook.com/search/top/?q={}",
    "linkedin": "https://www.linkedin.com/search/results/all/?keywords={}",
    "tiktok": "https://www.tiktok.com/search?q={}",
    "pinterest": "https://www.pinterest.com/search/pins/?q={}",
    "quora": "https://www.quora.com/search?q={}",
    "imdb": "https://www.imdb.com/find/?q={}",
    "spotify": "https://open.spotify.com/search/{}",
    "netflix": "https://www.netflix.com/search?q={}",
    "bing": "https://www.bing.com/search?q={}",
    "duckduckgo": "https://duckduckgo.com/?q={}",
    "chatgpt": "https://chatgpt.com/?q={}",
    "perplexity": "https://www.perplexity.ai/search?q={}",
}
SITE_ALIASES = {"yt": "youtube", "you tube": "youtube", "insta": "instagram", "ig": "instagram", "fb": "facebook",
                "stackoverflow": "stack overflow", "wiki": "wikipedia", "the web": "google", "the internet": "google",
                "online": "google", "web": "google", "image": "images", "pictures": "images", "photos": "images"}

# well-known, safe, no-sign-in places worth a click
RANDOM_SITES = {
    "fun": [("neal.fun", "https://neal.fun", "Neal's playful interactive toys"),
            ("Radio Garden", "https://radio.garden", "live radio from anywhere on a spinning globe"),
            ("Window Swap", "https://www.window-swap.com", "the view from a stranger's window"),
            ("Pointer Pointer", "https://pointerpointer.com", "photos of people pointing at your mouse"),
            ("Quick, Draw!", "https://quickdraw.withgoogle.com", "doodle and let an AI guess it"),
            ("Patatap", "https://patatap.com", "every key plays a sound and an animation"),
            ("The Useless Web", "https://theuselessweb.com", "a button that takes you somewhere pointless"),
            ("Little Alchemy 2", "https://littlealchemy2.com", "combine elements to make the world")],
    "space": [("Eyes on the Solar System", "https://eyes.nasa.gov/apps/solar-system/", "NASA's 3D solar system"),
              ("Astronomy Picture of the Day", "https://apod.nasa.gov/apod/astropix.html", "today's NASA space photo"),
              ("Stellarium Web", "https://stellarium-web.org", "the night sky above you right now")],
    "earth": [("Earth Nullschool", "https://earth.nullschool.net", "live winds swirling around the planet"),
              ("Windy", "https://www.windy.com", "weather radar for the whole world"),
              ("Google Arts and Culture", "https://artsandculture.google.com", "museums and art from everywhere")],
    "relax": [("A Soft Murmur", "https://asoftmurmur.com", "mix rain, waves and fire sounds"),
              ("myNoise", "https://mynoise.net", "background sound generators")],
    "comic": [("a random xkcd", "https://c.xkcd.com/random/comic/", "a random xkcd comic")],
    "article": [("a random Wikipedia article", "https://en.wikipedia.org/wiki/Special:Random",
                 "a random Wikipedia article")],
}
KIND_WORDS = {"website": "any", "site": "any", "page": "any", "web page": "any", "webpage": "any", "game": "fun",
              "toy": "fun", "wikipedia": "article", "wikipedia article": "article", "wiki": "article",
              "fact": "article", "article": "article", "xkcd": "comic", "music": "relax", "sound": "relax",
              "nature": "earth", "weather": "earth", "planet": "space", "stars": "space", "nasa": "space"}


def site_key(spoken: str) -> str | None:
    t = re.sub(r"\b(the|website|site|dot com|\.com|app)\b", " ", (spoken or "").lower()).strip()
    t = SITE_ALIASES.get(t, t)
    if t in SEARCH:
        return t
    hit = fuzzy.best(t, list(SEARCH) + list(SITE_ALIASES), cutoff=0.82) if t else None
    return SITE_ALIASES.get(hit, hit) if hit else None


def _vocabulary(ctx) -> list[str]:
    hints = ((ctx.cfg.get("stt") or {}).get("hints") or "") if ctx else ""
    return [h.strip() for h in re.split(r"[,;]", hints) if h.strip()]


def _open(ctx, url: str, new_tab: bool = True):
    if _linked(ctx):
        _call(ctx, "open", {"url": url, "newTab": new_tab})
    else:
        webbrowser.open(url)


@tool("Search a particular website: YouTube, Amazon, Daraz, Reddit, Wikipedia, GitHub, Instagram, Facebook, X, "
      "LinkedIn, TikTok, Google Images/News/Maps, IMDb, Spotify... (Google if no site is named).",
      {"query": {"type": "string"}, "site": {"type": "string", "description": "e.g. youtube, amazon, images"}},
      required=["query"], enabled=chrome_enabled, triggers=("search", "look up", "find", "look for", "google"))
def site_search(query: str, site: str = "google", ctx=None):
    key = site_key(site or "google")
    if key is None:
        raise ToolFailed(f"I don't know how to search {site}. I can search: {', '.join(list(SEARCH)[:12])}...")
    q = fuzzy.fix_names(query.strip(), _vocabulary(ctx))
    if not q:
        raise ToolFailed("What should I search for?")
    template = SEARCH[key]
    in_query = "?" in template and template.index("{}") > template.index("?")
    _open(ctx, template.format(urllib.parse.quote_plus(q) if in_query else urllib.parse.quote(q)))
    where = {"google": "the web", "x": "X", "youtube": "YouTube", "github": "GitHub", "linkedin": "LinkedIn",
             "tiktok": "TikTok", "imdb": "IMDb", "duckduckgo": "DuckDuckGo", "chatgpt": "ChatGPT",
             "aliexpress": "AliExpress"}.get(key, key.title())
    return Spoken(f"Searching {where} for {q}.")


@tool("Number the links and buttons on the current Chrome page so the user can say 'click 7', click one by its "
      "number, or hide the numbers.",
      {"action": {"type": "string", "enum": ["show", "click", "hide"]},
       "number": {"type": "integer", "description": "For click: the number shown on the page"}},
      required=["action"], enabled=chrome_enabled, triggers=TAB_WORDS + ("link", "links", "number", "click", "button"))
def chrome_links(action: str, number: int = 0, ctx=None):
    if not _linked(ctx):
        raise ToolFailed(SETUP_HINT)
    if action == "hide":
        _call(ctx, "run", {"fn": "hints", "args": {"action": "hide"}})
        return Spoken("Numbers hidden.")
    if action == "show":
        r = _call(ctx, "run", {"fn": "hints", "args": {"action": "show"}}) or {}
        n = r.get("count", 0)
        if not n:
            return Spoken("I can't see anything to click on this part of the page.")
        return Spoken(f"I've numbered {n} links and buttons. Say click and a number.")
    if number < 1:
        raise ToolFailed("Which number?")
    r = _call(ctx, "run", {"fn": "hints", "args": {"action": "label", "n": number}}) or {}
    if not r.get("ok"):
        raise ToolFailed(f"I couldn't: {r.get('reason')}.")
    label = r.get("label", "")
    confirm = ctx.context.get("confirm") if ctx else None
    if RISKY_CLICK.search(label) and (not confirm or not confirm(f"That's {label}. Shall I click it?")):
        return Spoken("Okay, I didn't click it.")
    r = _call(ctx, "run", {"fn": "hints", "args": {"action": "click", "n": number}}) or {}
    if not r.get("ok"):
        raise ToolFailed(f"I couldn't: {r.get('reason')}.")
    return Spoken(f"Clicked {r.get('label') or 'it'}.")


@tool("Search results on the current Chrome page (Google, YouTube, Bing, Amazon, Reddit...): read the top titles, "
      "or open result number N.",
      {"action": {"type": "string", "enum": ["read", "open"]},
       "number": {"type": "integer", "description": "For open: 1 = first result, -1 = last"}},
      required=["action"], enabled=chrome_enabled, triggers=("result", "results", "first", "second", "third"))
def chrome_results(action: str, number: int = 1, ctx=None):
    if not _linked(ctx):
        raise ToolFailed(SETUP_HINT)
    if action == "read" or number == -1:
        r = _call(ctx, "run", {"fn": "results", "args": {"action": "list"}}) or {}
        found = r.get("results") or []
        if not found:
            raise ToolFailed("I can't see any results on this page.")
        if action == "read":
            return Spoken("Top results: " + " ".join(f"{i}: {x['title']}." for i, x in enumerate(found[:5], 1)))
        number = len(found)
    r = _call(ctx, "run", {"fn": "results", "args": {"action": "open", "n": number}}) or {}
    if not r.get("ok"):
        raise ToolFailed(f"I couldn't: {r.get('reason')}.")
    return Spoken(f"Opening {r.get('title') or 'it'}.")


@tool("Find words on the current Chrome page and scroll to them.",
      {"text": {"type": "string"}}, enabled=chrome_enabled, triggers=("find", "where", "look for", "search this page"))
def chrome_find(text: str, ctx=None):
    if not _linked(ctx):
        raise ToolFailed(SETUP_HINT)
    r = _call(ctx, "run", {"fn": "find", "args": {"text": text}}) or {}
    return Spoken(f"Found {text}." if r.get("found") else f"I can't find {text} on this page.")


@tool("Open a random interesting website: something fun, space, earth, relaxing sounds, a random Wikipedia "
      "article or a random comic.",
      {"kind": {"type": "string", "enum": ["any", "fun", "space", "earth", "relax", "article", "comic"]}},
      required=[], enabled=chrome_enabled, triggers=("random", "surprise", "bored", "something interesting"))
def random_site(kind: str = "any", ctx=None):
    kind = KIND_WORDS.get(kind, kind) if kind not in RANDOM_SITES else kind
    pool = [s for k, v in RANDOM_SITES.items() for s in v] if kind not in RANDOM_SITES else RANDOM_SITES[kind]
    name, url, blurb = random.choice(pool)
    _open(ctx, url)
    return Spoken(f"Here's {name}: {blurb}." if name != blurb else f"Here's {name}.")


def route_web(t: str, has) -> list[tuple[str, dict]] | None:
    if has("chrome_links"):
        if re.fullmatch(r"(?:show|number|label|give)(?: me)? (?:the |all )?(?:links|buttons|numbers|hints|clickable "
                        r"(?:things|stuff))(?: on (?:the|this) page)?", t):
            return [("chrome_links", {"action": "show"})]
        if re.fullmatch(r"(?:hide|remove|clear|close) (?:the )?(?:links|numbers|hints)", t):
            return [("chrome_links", {"action": "hide"})]
    if has("chrome_results"):
        m = re.fullmatch(r"(?:open|click|go to|play|show me)(?: on)? (?:the )?(?P<n>\w+)(?: search)? (?:result|link|video)", t) \
            or re.fullmatch(r"(?:open|click|go to|play) (?:search )?result (?:number )?(?P<n>\w+(?: \w+)?)", t)
        if m and fuzzy.number(m.group("n")) is not None:
            return [("chrome_results", {"action": "open", "number": fuzzy.number(m.group("n"))})]
        if re.fullmatch(r"(?:read|what are|tell me)(?: me)? (?:the )?(?:top |search )?results", t):
            return [("chrome_results", {"action": "read"})]
    if has("chrome_links"):
        m = re.fullmatch(r"(?:click|press|tap|select|hit)(?: on)? (?:link |number |button |item )?(?P<n>\w+(?: \w+)?)", t) \
            or re.fullmatch(r"(?:open|follow) (?:link|number) (?P<n>\w+(?: \w+)?)", t)
        if m and fuzzy.number(m.group("n")) not in (None, -1):
            return [("chrome_links", {"action": "click", "number": fuzzy.number(m.group("n"))})]
    if has("chrome_find"):
        m = re.fullmatch(r"(?:find|look for|search for|where is|where's) (?P<q>.+?) on (?:this|the) page", t)
        if m:
            return [("chrome_find", {"text": m.group("q")})]
    if has("random_site"):
        m = re.fullmatch(r"(?:open|take me to|show me|go to|visit|find)(?: me)? (?:a |some )?random "
                         r"(?P<k>website|site|page|web ?page|wikipedia article|wiki|article|comic|xkcd|game|fact)", t)
        if m:
            return [("random_site", {"kind": KIND_WORDS.get(m.group("k"), "any")})]
        if re.fullmatch(r"surprise me(?: with (?:a |some )?(?:website|site|something))?|take me somewhere random", t):
            return [("random_site", {"kind": "any"})]
    if has("site_search"):
        m = re.fullmatch(r"(?:search|look up|find)(?: for)? (?P<q>.+?) on (?P<s>[a-z .]+)", t) \
            or re.fullmatch(r"search (?P<s>[a-z .]+?) for (?P<q>.+)", t) \
            or re.fullmatch(r"(?P<s>[a-z ]+?) search(?: for)? (?P<q>.+)", t)
        if m and m.group("s").strip() not in ("this page", "the page", "my computer", "my pc", "the computer"):
            key = site_key(m.group("s"))
            if key:
                return [("site_search", {"query": m.group("q"), "site": key})]
    return None
