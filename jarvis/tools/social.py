"""Instagram and Facebook in Chrome by voice: open a part of the site, like / save / read the post in view, next
and previous post, reel or story, chats (open one by a fuzzy name, read it, send a message), comments, follow.

Page actions go through the Jarvis Control extension (extension/chrome, PAGE.social). Anything that speaks for
you (a message, a comment, a follow) is read back and needs a spoken yes first.
"""
from __future__ import annotations

import re
import urllib.parse
import webbrowser

from .. import fuzzy
from .builtin import ToolFailed
from .chrome import SETUP_HINT, _call, _linked, chrome_enabled
from .registry import Spoken, tool

SITES = {"instagram": "https://www.instagram.com", "facebook": "https://www.facebook.com"}
SITE_WORDS = {"instagram": "instagram", "insta": "instagram", "ig": "instagram", "gram": "instagram",
              "facebook": "facebook", "fb": "facebook", "face book": "facebook", "messenger": "facebook"}
PAGES = {
    "instagram": {"home": "/", "feed": "/", "reels": "/reels/", "explore": "/explore/", "messages": "/direct/inbox/",
                  "notifications": "/notifications/", "stories": "/"},
    "facebook": {"home": "/", "feed": "/", "reels": "/reel/", "watch": "/watch/", "videos": "/watch/",
                 "marketplace": "/marketplace/", "notifications": "/notifications/", "messages": "/messages/",
                 "groups": "/groups/feed/", "friends": "/friends/", "memories": "/onthisday/", "saved": "/saved/",
                 "profile": "/me/", "events": "/events/", "stories": "/stories/"},
}
PAGE_WORDS = {"dms": "messages", "dm": "messages", "inbox": "messages", "chats": "messages", "messenger": "messages",
              "message": "messages", "reel": "reels", "shorts": "reels", "video": "videos", "news feed": "feed",
              "timeline": "feed", "story": "stories", "notification": "notifications", "alerts": "notifications",
              "market place": "marketplace", "market": "marketplace", "group": "groups", "my profile": "profile",
              "account": "profile", "discover": "explore"}
ACTIONS = ["open", "profile", "search", "stories", "like", "unlike", "save", "unsave", "read", "next", "previous",
           "pause", "play", "open_chat", "read_messages", "send_message", "comment", "follow", "unfollow"]
SPEAKS_FOR_YOU = ("send_message", "comment", "follow", "unfollow")
TRIGGERS = ("instagram", "insta", "facebook", "fb", "messenger", "post", "reel", "story", "stories", "like", "dm",
            "message", "comment", "follow", "feed", "chat", "marketplace")


def site_name(spoken: str) -> str:
    t = (spoken or "").lower().strip()
    if t in SITE_WORDS:
        return SITE_WORDS[t]
    hit = fuzzy.best(t, list(SITE_WORDS), cutoff=0.8) if t else None
    return SITE_WORDS[hit] if hit else ""


def page_name(site: str, spoken: str) -> str | None:
    t = re.sub(r"\b(my|the|page|tab|section)\b", " ", (spoken or "").lower()).strip() or "home"
    t = PAGE_WORDS.get(t, t)
    pages = PAGES[site]
    if t in pages:
        return t
    hit = fuzzy.best(t, list(pages) + list(PAGE_WORDS), cutoff=0.8)
    hit = PAGE_WORDS.get(hit, hit)
    return hit if hit in pages else None


def _which(url: str) -> str:
    host = urllib.parse.urlparse(url or "").netloc
    return "instagram" if "instagram.com" in host else "facebook" if ("facebook.com" in host or
                                                                       "messenger.com" in host) else ""


def _social_tab(ctx, site: str) -> tuple[str, dict]:
    """The Instagram/Facebook tab to act on: the active one if it's social, else an open tab of `site`."""
    tabs = _call(ctx, "tabs")
    active = next((t for t in tabs if t.get("active")), None)
    if active and _which(active.get("url")) and (not site or _which(active["url"]) == site):
        return _which(active["url"]), active
    for t in tabs:
        if _which(t.get("url")) and (not site or _which(t["url"]) == site):
            _call(ctx, "activate", {"tabId": t["id"]})
            return _which(t["url"]), t
    raise ToolFailed(f"{(site or 'Instagram or Facebook').title()} isn't open in Chrome. Say open {site or 'instagram'} first.")


def _open(ctx, url: str, site: str, label: str):
    if not _linked(ctx):
        webbrowser.open(url)
        return Spoken(f"Opened {label}.")
    tabs = _call(ctx, "tabs")
    active = next((t for t in tabs if t.get("active")), None)
    same = active if active and _which(active.get("url")) == site else None
    _call(ctx, "open", {"url": url, "newTab": same is None, "wait": True}, timeout=25)
    return Spoken(f"Opened {label}.")


def _run(ctx, tab: dict, args: dict, timeout: float = 15) -> dict:
    r = _call(ctx, "run", {"fn": "social", "tabId": tab["id"], "args": args}, timeout=timeout) or {}
    if not r.get("ok"):
        raise ToolFailed(f"I couldn't: {r.get('reason', 'the page did not respond')}.")
    return r


def _yes(ctx, question: str) -> bool:
    confirm = ctx.context.get("confirm") if ctx else None
    return bool(confirm and confirm(question))


def _open_chat(ctx, tab: dict, who: str) -> str:
    names = _run(ctx, tab, {"action": "chats"}).get("names", [])
    hit = fuzzy.best(who, list(enumerate(names)), key=lambda p: p[1], cutoff=0.72)
    if hit is None:
        shown = ", ".join(n for n in names[:6] if n)
        raise ToolFailed(f"I can't find a chat with {who}." + (f" I can see: {shown}." if shown else
                                                                  " Open your messages first."))
    _run(ctx, tab, {"action": "open_chat", "name": str(hit[0])})
    return hit[1]


@tool("Instagram and Facebook in Chrome: open a part of the site (feed, reels, explore, messages, notifications, "
      "marketplace, watch, groups, friends, stories, profile), open someone's profile, search, open stories; "
      "like / unlike / save / read the post in view; next or previous post, reel or story; pause or play; open a "
      "chat with someone, read it, send a message; comment on the post; follow or unfollow.",
      {"action": {"type": "string", "enum": ACTIONS},
       "site": {"type": "string", "enum": ["instagram", "facebook", ""],
                "description": "Default: the Instagram/Facebook tab that's open"},
       "page": {"type": "string", "description": "For open: feed, reels, explore, messages, notifications, "
                                                 "marketplace, watch, groups, friends, stories, profile"},
       "target": {"type": "string", "description": "A person or account (profile, open_chat, send_message), "
                                                   "or what to search for"},
       "text": {"type": "string", "description": "The message or comment, in the user's words"}},
      required=["action"], enabled=chrome_enabled, triggers=TRIGGERS)
def social(action: str, site: str = "", page: str = "", target: str = "", text: str = "", ctx=None):
    site = site_name(site)
    if action == "open":
        site = site or "instagram"
        key = page_name(site, page)
        if key is None:
            raise ToolFailed(f"{site.title()} has no {page} page I know of. Try: {', '.join(PAGES[site])}.")
        r = _open(ctx, SITES[site] + PAGES[site][key], site, f"{site.title()} {'' if key == 'home' else key}".strip())
        if key == "stories" and site == "instagram" and _linked(ctx):
            try:
                _run(ctx, _social_tab(ctx, site)[1], {"action": "stories"})
            except ToolFailed:
                return Spoken("Opened Instagram, but I can't see any stories right now.")
            return Spoken("Playing Instagram stories.")
        return r
    if action == "profile":
        site = site or "instagram"
        handle = re.sub(r"[^a-z0-9._]", "", target.lower().replace(" ", ""))
        if not handle:
            raise ToolFailed("Whose profile?")
        return _open(ctx, f"{SITES[site]}/{handle}/", site, f"{target}'s {site.title()}")
    if action == "search":
        site = site or "instagram"
        q = urllib.parse.quote(target.strip().lstrip("#"))
        url = (f"{SITES['instagram']}/explore/tags/{q}/" if target.strip().startswith("#") else
               f"{SITES['instagram']}/explore/search/keyword/?q={q}") if site == "instagram" else \
            f"{SITES['facebook']}/search/top/?q={q}"
        return _open(ctx, url, site, f"{site.title()} results for {target}")
    if not _linked(ctx):
        raise ToolFailed(SETUP_HINT)
    site, tab = _social_tab(ctx, site)

    if action == "open_chat":
        name = _open_chat(ctx, tab, target)
        return Spoken(f"Opened your chat with {name}.")
    if action == "read_messages":
        if target:
            _open_chat(ctx, tab, target)
        said = _run(ctx, tab, {"action": "read_messages"}).get("text", "")
        return Spoken("The latest messages: " + said if said else "This chat is empty.")
    if action in SPEAKS_FOR_YOU:
        if action in ("send_message", "comment") and not text.strip():
            raise ToolFailed("What should it say?")
        who = target
        question = {"send_message": f"Shall I send: {text}?",
                    "comment": f"Shall I comment: {text}?",
                    "follow": "Shall I follow this account?",
                    "unfollow": "Shall I unfollow this account?"}[action]
        if action == "send_message" and target:
            who = _open_chat(ctx, tab, target)
            question = f"Shall I send {who}: {text}?"
        if not _yes(ctx, question):
            return Spoken("Okay, I didn't send anything." if action in ("send_message", "comment") else "Okay, I left it.")
        _run(ctx, tab, {"action": action, "text": text}, timeout=20)
        return Spoken({"send_message": "Sent.", "comment": "Comment posted.", "follow": "Followed.",
                       "unfollow": "Unfollowed."}[action])

    r = _run(ctx, tab, {"action": action})
    if action == "read":
        who = f"{r['author']}: " if r.get("author") else ""
        return Spoken(f"{who}{r.get('text') or 'This post has no text.'}")
    if r.get("already"):
        return Spoken({"like": "You've already liked it.", "unlike": "You hadn't liked it."}.get(action, "Done."))
    if action in ("next", "previous"):
        return Spoken(f"{action.title()} {r.get('did') if r.get('did') in ('post', 'reel', 'story') else 'one'}.")
    return Spoken({"like": "Liked.", "unlike": "Unliked.", "save": "Saved.", "unsave": "Removed from saved.",
                   "pause": "Paused.", "play": "Playing."}.get(action, "Done."))


def route_social(t: str, has) -> list[tuple[str, dict]] | None:
    if not has("social"):
        return None
    site = r"(?P<site>instagram|insta|facebook|fb|messenger)"
    pages = (r"(?P<page>feed|news feed|reels?|explore|messages|dms|inbox|chats|notifications|marketplace|watch|"
             r"videos|groups|friends|stories|story|profile|saved|memories|events)")
    m = re.fullmatch(r"(?:open|show|go to|check|take me to)(?: me)? (?:my )?" + site + r"(?:'s)? " + pages, t) or \
        re.fullmatch(r"(?:open|show|go to|check)(?: me)? (?:my |the )?" + pages + r" (?:on|in) " + site, t)
    if m:
        return [("social", {"action": "open", "site": m.group("site"), "page": m.group("page")})]
    # "like this post on instagram": the same commands, aimed at that site's tab
    on = re.fullmatch(r"(?P<rest>.+?) (?:on|in) " + site, t)
    there = {"site": on.group("site")} if on else {}
    rest = on.group("rest") if on else t
    video = "|video" if on else ""          # a bare "next video" / "pause the video" stays with the media keys
    m = re.fullmatch(r"(?P<a>like|unlike|save|unsave) (?:this|the|that) (?:post|reel|photo|picture|pic"
                     + video + ")", rest)
    if m:
        return [("social", {"action": m.group("a"), **there})]
    m = re.fullmatch(r"(?:go to |show me |skip to )?(?:the )?(?P<d>next|previous|last) (?:post|reel|story"
                     + video + ")", rest)
    if m:
        return [("social", {"action": "previous" if m.group("d") in ("previous", "last") else "next", **there})]
    if re.fullmatch(r"(?:read|what does) (?:this|the) (?:post|caption)(?: say)?", rest):
        return [("social", {"action": "read", **there})]
    m = re.fullmatch(r"(?P<a>pause|play|resume) (?:the |this )?(?:story|reel" + video + ")", rest)
    if m:
        return [("social", {"action": "pause" if m.group("a") == "pause" else "play", **there})]
    m = re.fullmatch(r"(?P<a>follow|unfollow) (?:this|them|him|her|this account|this page|this person)", rest)
    if m:
        return [("social", {"action": m.group("a"), **there})]
    m = re.fullmatch(r"(?:open|show)(?: me)? (?:my |the )?(?:chat|conversation|messages|dms) with (?P<n>.+)", t)
    if m:
        return [("social", {"action": "open_chat", "target": m.group("n")})]
    if re.fullmatch(r"read (?:this|the) (?:chat|conversation)", t):
        return [("social", {"action": "read_messages"})]
    m = re.fullmatch(r"(?:send|reply)(?: a)?(?: message| reply)? (?:saying|that) (?P<m>.+)", t)
    if m:
        return [("social", {"action": "send_message", "text": m.group("m")})]
    m = re.fullmatch(r"(?:send|dm|message) (?P<n>[\w .'-]+?) (?:a message )?on (?:instagram|insta|facebook|messenger) "
                     r"(?:saying|that) (?P<m>.+)", t) or re.fullmatch(r"dm (?P<n>[\w .'-]+?) (?:saying|that) (?P<m>.+)", t)
    if m:
        return [("social", {"action": "send_message", "target": m.group("n"), "text": m.group("m")})]
    m = re.fullmatch(r"(?:post a |write a |leave a )?comment (?:saying|that) (?P<m>.+)", t)
    if m:
        return [("social", {"action": "comment", "text": m.group("m")})]
    return None
