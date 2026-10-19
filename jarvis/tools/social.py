"""Social sites in Chrome by voice.

Instagram and Facebook: open a part of the site, like / save / read the post in view, next and previous post, reel
or story, chats (open one by a fuzzy name, read it, send a message), comments, follow.
WhatsApp Web: open a chat by a fuzzy name (searching WhatsApp when it isn't in view), read it, who messaged you,
send a message.
Reddit: open a subreddit; read, upvote, downvote or save the post in view; next and previous post.
X (Twitter): open home, explore, notifications, messages, bookmarks, a profile or a search; read, like or bookmark
the post in view; next and previous post; repost.

Page actions go through the Jarvis Control extension (extension/chrome: PAGE.social for Instagram/Facebook,
PAGE.whatsapp, PAGE.reddit and PAGE.x). Anything that speaks for you (a message, a comment, a follow, a repost) is
read back and needs a spoken yes first.
"""
from __future__ import annotations

import re
import urllib.parse
import webbrowser

from .. import fuzzy
from .builtin import ToolFailed
from .chrome import SETUP_HINT, _call, _linked, chrome_enabled
from .registry import Spoken, tool

SITES = {"instagram": "https://www.instagram.com", "facebook": "https://www.facebook.com",
         "whatsapp": "https://web.whatsapp.com", "reddit": "https://www.reddit.com", "x": "https://x.com"}
LABEL = {"instagram": "Instagram", "facebook": "Facebook", "whatsapp": "WhatsApp", "reddit": "Reddit", "x": "X"}
SAY_OPEN = {"whatsapp": "open WhatsApp web", "x": "open twitter"}     # a bare "open whatsapp" may start the app
SITE_WORDS = {"instagram": "instagram", "insta": "instagram", "ig": "instagram", "gram": "instagram",
              "facebook": "facebook", "fb": "facebook", "face book": "facebook", "messenger": "facebook",
              "whatsapp": "whatsapp", "whats app": "whatsapp", "what's app": "whatsapp", "whatsapp web": "whatsapp",
              "reddit": "reddit", "red it": "reddit", "read it": "reddit",
              "x": "x", "ex": "x", "twitter": "x", "x.com": "x"}
PAGES = {
    "instagram": {"home": "/", "feed": "/", "reels": "/reels/", "explore": "/explore/", "messages": "/direct/inbox/",
                  "notifications": "/notifications/", "stories": "/"},
    "facebook": {"home": "/", "feed": "/", "reels": "/reel/", "watch": "/watch/", "videos": "/watch/",
                 "marketplace": "/marketplace/", "notifications": "/notifications/", "messages": "/messages/",
                 "groups": "/groups/feed/", "friends": "/friends/", "memories": "/onthisday/", "saved": "/saved/",
                 "profile": "/me/", "events": "/events/", "stories": "/stories/"},
    "whatsapp": {"home": "/", "messages": "/"},
    "reddit": {"home": "/", "feed": "/", "popular": "/r/popular/", "all": "/r/all/", "notifications": "/notifications/",
               "profile": "/user/me/", "saved": "/user/me/saved/"},
    "x": {"home": "/home", "feed": "/home", "explore": "/explore", "notifications": "/notifications",
          "messages": "/messages", "bookmarks": "/i/bookmarks", "saved": "/i/bookmarks", "profile": "/home"},
}
PAGE_WORDS = {"dms": "messages", "dm": "messages", "inbox": "messages", "chats": "messages", "messenger": "messages",
              "message": "messages", "reel": "reels", "shorts": "reels", "video": "videos", "news feed": "feed",
              "timeline": "feed", "story": "stories", "notification": "notifications", "alerts": "notifications",
              "market place": "marketplace", "market": "marketplace", "group": "groups", "my profile": "profile",
              "account": "profile", "discover": "explore", "bookmark": "bookmarks", "trending": "explore",
              "front page": "home", "for you": "home"}
ACTIONS = ["open", "profile", "search", "subreddit", "stories", "like", "unlike", "save", "unsave", "read", "next",
           "previous", "pause", "play", "upvote", "downvote", "unvote", "repost", "open_chat", "read_messages",
           "send_message", "unread", "comment", "follow", "unfollow"]
SPEAKS_FOR_YOU = ("send_message", "comment", "follow", "unfollow", "repost")
TRIGGERS = ("instagram", "insta", "facebook", "fb", "messenger", "post", "reel", "story", "stories", "like", "dm",
            "message", "comment", "follow", "feed", "chat", "marketplace", "whatsapp", "whats app", "what's app",
            "reddit", "subreddit", "twitter", "tweet", "x", "upvote", "up vote", "downvote", "down vote", "vote",
            "repost", "retweet", "bookmark", "texted", "unread")

# what each site's page action can do; Reddit's "like" is an upvote
_POSTS = ("like", "unlike", "save", "unsave", "read", "next", "previous", "pause", "play")
_META = _POSTS + ("stories", "open_chat", "read_messages", "send_message", "comment", "follow", "unfollow")
CAN = {"instagram": _META, "facebook": _META,
       "whatsapp": ("open_chat", "read_messages", "send_message", "unread"),
       "reddit": ("save", "unsave", "read", "next", "previous", "pause", "play", "upvote", "downvote", "unvote"),
       "x": _POSTS + ("repost",)}
ALIAS = {"reddit": {"like": "upvote", "unlike": "unvote"}}
PAGE_FN = {"whatsapp": "whatsapp", "reddit": "reddit", "x": "x"}       # Instagram and Facebook: PAGE.social
VERBS = {"open_chat": "open chats", "read_messages": "read chats", "send_message": "send messages",
         "unread": "check for new messages", "comment": "comment", "follow": "follow accounts",
         "unfollow": "unfollow accounts", "stories": "play stories", "upvote": "upvote", "downvote": "downvote",
         "unvote": "remove votes", "repost": "repost", "read": "read posts", "next": "move between posts",
         "previous": "move between posts", "like": "like posts", "unlike": "unlike posts", "save": "save posts",
         "unsave": "unsave posts", "pause": "pause videos", "play": "play videos"}
POPULAR_SUBS = ("AskReddit", "worldnews", "news", "funny", "gaming", "pics", "science", "movies", "aww",
                "todayilearned", "Music", "videos", "memes", "technology", "explainlikeimfive", "Showerthoughts",
                "LifeProTips", "mildlyinteresting", "pcmasterrace", "buildapc", "programming", "Python",
                "learnpython", "javascript", "webdev", "MachineLearning", "LocalLLaMA", "ChatGPT", "ClaudeAI",
                "OpenAI", "artificial", "singularity", "StableDiffusion", "space", "Futurology", "dataisbeautiful",
                "nba", "soccer", "Cricket", "formula1", "pakistan", "india", "personalfinance", "Fitness", "food",
                "Cooking", "travel", "books", "television", "anime", "NoStupidQuestions", "CasualConversation",
                "UpliftingNews", "Android", "apple", "iphone", "linux", "windows", "pcgaming", "Steam", "PS5",
                "NintendoSwitch", "Minecraft", "leagueoflegends", "DIY", "gadgets", "photography", "Art", "history",
                "philosophy", "Documentaries", "Jokes", "dadjokes", "ProgrammerHumor", "cscareerquestions",
                "selfhosted", "homelab", "gifs", "OldSchoolCool", "interestingasfuck", "Damnthatsinteresting")


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


def subreddit_name(spoken: str) -> str:
    """'python', 'r slash machine learning', 'the ask reddit subreddit' -> 'Python', 'MachineLearning', 'AskReddit'.
    A popular subreddit's spelling wins when it's a near-certain match; anything else is just squashed together."""
    t = (spoken or "").strip().lower()
    t = re.sub(r"^(?:the |r slash |r/ ?|subreddit |sub reddit )+|(?: subreddit| sub reddit)$", "", t).strip()
    squashed = re.sub(r"[^a-z0-9_]", "", t)
    hit = fuzzy.best(t, POPULAR_SUBS, cutoff=0.9) if squashed else None
    if hit and abs(len(squashed) - len(hit)) <= 2:
        return hit
    return squashed


def _which(url: str) -> str:
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    on = lambda *names: any(host == n or host.endswith("." + n) for n in names)  # noqa: E731
    return ("instagram" if on("instagram.com") else "facebook" if on("facebook.com", "messenger.com") else
            "whatsapp" if on("web.whatsapp.com") else "reddit" if on("reddit.com") else
            "x" if on("x.com", "twitter.com") else "")


def _page_action(site: str, action: str) -> str | None:
    """The page action `site` uses for `action` ("like" on Reddit is an upvote), or None if it can't."""
    a = ALIAS.get(site, {}).get(action, action)
    return a if a in CAN.get(site, ()) else None


def _cannot(site: str, action: str) -> str:
    return f"I can't {VERBS.get(action, action.replace('_', ' '))} on {LABEL[site]}."


def _social_tab(ctx, site: str, action: str = "", stay: bool = False) -> tuple[str, dict]:
    """The tab to act on: the active one if it's a social site that can do `action`, else an open tab of `site`
    (or of any site that can). With `stay`, a social tab in front that can't do it is an error, not a reason to
    act on another tab ("comment saying ..." while looking at Reddit must not comment on Instagram)."""
    can = [site] if site else [s for s in SITES if not action or _page_action(s, action)]
    tabs = _call(ctx, "tabs")
    active = next((t for t in tabs if t.get("active")), None)
    here = _which(active.get("url")) if active else ""
    if here and here in can:
        return here, active
    if here and stay:
        raise ToolFailed(_cannot(here, action))
    for t in tabs:
        if _which(t.get("url")) in can:
            _call(ctx, "activate", {"tabId": t["id"]})
            return _which(t["url"]), t
    names = [LABEL[s] for s in can]
    who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    raise ToolFailed(f"{who} isn't open in Chrome. Say {SAY_OPEN.get(can[0], 'open ' + can[0])} first.")


def _open(ctx, url: str, site: str, label: str):
    if not _linked(ctx):
        webbrowser.open(url)
        return Spoken(f"Opened {label}.")
    tabs = _call(ctx, "tabs")
    active = next((t for t in tabs if t.get("active")), None)
    if site == "whatsapp":                  # WhatsApp Web runs in one tab at a time: go back to it
        there = next((t for t in tabs if _which(t.get("url")) == "whatsapp"), None)
        if there:
            _call(ctx, "activate", {"tabId": there["id"]})
            return Spoken(f"Opened {label}.")
    same = active if active and _which(active.get("url")) == site else None
    _call(ctx, "open", {"url": url, "newTab": same is None, "wait": True}, timeout=25)
    return Spoken(f"Opened {label}.")


def _run(ctx, tab: dict, args: dict, timeout: float = 15) -> dict:
    fn = PAGE_FN.get(_which(tab.get("url")), "social")
    r = _call(ctx, "run", {"fn": fn, "tabId": tab["id"], "args": args}, timeout=timeout) or {}
    if not r.get("ok"):
        raise ToolFailed(f"I couldn't: {r.get('reason', 'the page did not respond')}.")
    return r


def _yes(ctx, question: str) -> bool:
    confirm = ctx.context.get("confirm") if ctx else None
    return bool(confirm and confirm(question))


def _open_chat(ctx, tab: dict, who: str) -> str:
    names = _run(ctx, tab, {"action": "chats"}).get("names", [])
    hit = fuzzy.best(who, list(enumerate(names)), key=lambda p: p[1], cutoff=0.72)
    whatsapp = _which(tab.get("url")) == "whatsapp"
    if hit is None and whatsapp:            # not in view: let WhatsApp's own search find them
        names = _run(ctx, tab, {"action": "search", "text": who}, timeout=20).get("names", [])
        hit = fuzzy.best(who, list(enumerate(names)), key=lambda p: p[1], cutoff=0.72)
    if hit is None:
        shown = ", ".join(n for n in names[:6] if n)
        raise ToolFailed(f"I can't find a chat with {who}." + (f" I can see: {shown}." if shown else
                                                                  " Open your messages first."))
    args = {"action": "open_chat", "name": str(hit[0])}
    if whatsapp:
        args["want"] = hit[1]               # the list can reorder when a message arrives: find it by name first
    _run(ctx, tab, args)
    return hit[1]


def _end(s: str) -> str:
    s = s.strip()
    return s if not s or re.search(r"[.!?…]$", s) else s + "."


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0] + "..."


def _said(messages: list[dict]) -> str:
    """WhatsApp messages as speech, the sender named once per run: 'Ahmad Khan: hi. are you coming? You: maybe.'"""
    out, last = [], None
    for m in messages:
        line = _end(m.get("text") or "")
        who = m.get("who") or "Someone"
        out.append(line if who == last else f"{who}: {line}")
        last = who
    return " ".join(out)


def _unread(chats: list[dict]) -> str:
    if not chats:
        return "No new WhatsApp messages."
    parts = [f"{c.get('count') or 1} from {c.get('name') or 'someone'}" for c in chats[:6]]
    if len(chats) > 6:
        parts.append(f"{len(chats) - 6} more chat{'s' * (len(chats) > 7)}")
    return "New on WhatsApp: " + (parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]) + "."


def _read_text(site: str, r: dict) -> str:
    if site == "reddit":
        score = str(r.get("score") or "").strip()
        head = ", ".join(p for p in (r.get("subreddit"), f"by {r['author']}" if r.get("author") else "",
                                     f"{score} point{'s' * (score != '1')}" if score else "") if p)
        title = _end(r.get("title") or "This post has no title")
        body = _short(r.get("body") or "", 300)
        return (f"{head}. " if head else "") + title + (f" {_end(body)}" if body else "")
    if site == "x":
        who = r.get("author") or r.get("handle") or "Someone"
        said = r.get("text") or ("A photo or video." if r.get("media") else "This post has no text.")
        return (f"{_end(r['context'])} " if r.get("context") else "") + f"{who}: {_short(said, 600)}"
    who = f"{r['author']}: " if r.get("author") else ""
    return f"{who}{r.get('text') or 'This post has no text.'}"


ALREADY = {"like": "You've already liked it.", "unlike": "You hadn't liked it.", "upvote": "You've already upvoted it.",
           "downvote": "You've already downvoted it.", "unvote": "You hadn't voted on it.",
           "save": "It's already saved.", "unsave": "It isn't saved."}
DONE = {"like": "Liked.", "unlike": "Unliked.", "save": "Saved.", "unsave": "Removed from saved.", "pause": "Paused.",
        "play": "Playing.", "upvote": "Upvoted.", "downvote": "Downvoted.", "unvote": "Removed your vote."}
DONE_X = {"save": "Bookmarked.", "unsave": "Removed the bookmark."}


@tool("Social sites in Chrome. Instagram and Facebook: open a part of the site (feed, reels, explore, messages, "
      "notifications, marketplace, watch, groups, friends, stories, profile), open someone's profile, search, open "
      "stories; like / unlike / save / read the post in view; next or previous post, reel or story; pause or play; "
      "open a chat with someone, read it, send a message; comment on the post; follow or unfollow. WhatsApp Web: "
      "open a chat, read it, say who sent new messages (unread), send a message. Reddit: open a subreddit; read / "
      "upvote / downvote / unvote / save the post in view; next or previous post. X (Twitter): open home, explore, "
      "notifications, messages, bookmarks, a profile or a search; read / like / save (bookmark) the post in view; "
      "next or previous post; repost.",
      {"action": {"type": "string", "enum": ACTIONS},
       "site": {"type": "string", "enum": ["instagram", "facebook", "whatsapp", "reddit", "x", ""],
                "description": "Default: the social tab that's open (x is Twitter)"},
       "page": {"type": "string", "description": "For open: feed, reels, explore, messages, notifications, "
                                                 "marketplace, watch, groups, friends, stories, profile, popular, "
                                                 "bookmarks"},
       "target": {"type": "string", "description": "A person or account (profile, open_chat, send_message), a "
                                                   "subreddit, or what to search for"},
       "text": {"type": "string", "description": "The message or comment, in the user's words"}},
      required=["action"], enabled=chrome_enabled, triggers=TRIGGERS)
def social(action: str, site: str = "", page: str = "", target: str = "", text: str = "", ctx=None):
    site = site_name(site)
    if action == "open":
        site = site or "instagram"
        key = page_name(site, page)
        if key is None:
            raise ToolFailed(f"{LABEL[site]} has no {page} page I know of. Try: {', '.join(PAGES[site])}.")
        label = LABEL[site] if site == "whatsapp" else f"{LABEL[site]} {'' if key == 'home' else key}".strip()
        r = _open(ctx, SITES[site] + PAGES[site][key], site, label)
        if key == "stories" and site == "instagram" and _linked(ctx):
            try:
                _run(ctx, _social_tab(ctx, site)[1], {"action": "stories"})
            except ToolFailed:
                return Spoken("Opened Instagram, but I can't see any stories right now.")
            return Spoken("Playing Instagram stories.")
        if key == "profile" and site == "x" and _linked(ctx):        # X has no /me: follow the profile link
            try:
                _run(ctx, _social_tab(ctx, site)[1], {"action": "my_profile"})
            except ToolFailed:
                return Spoken("Opened X, but I can't find your profile link. Are you signed in?")
            return Spoken("Opened your X profile.")
        return r
    if action == "subreddit":
        name = subreddit_name(target or page)
        if not name:
            raise ToolFailed("Which subreddit?")
        return _open(ctx, f"{SITES['reddit']}/r/{name}/", "reddit", f"the {name} subreddit")
    if action in ("profile", "search") and site == "whatsapp":
        action = "open_chat"                # a person on WhatsApp is a chat
    if action == "profile":
        site = site or "instagram"
        handle = re.sub(r"[^a-z0-9._]", "", target.lower().replace(" ", ""))
        if not handle:
            raise ToolFailed("Whose profile?")
        if site == "reddit":
            return _open(ctx, f"{SITES['reddit']}/user/{handle}/", site, f"{target}'s Reddit profile")
        if site == "x":
            return _open(ctx, f"{SITES['x']}/{handle.replace('.', '')}", site, f"{target}'s X profile")
        return _open(ctx, f"{SITES[site]}/{handle}/", site, f"{target}'s {site.title()}")
    if action == "search":
        site = site or "instagram"
        q = urllib.parse.quote(target.strip().lstrip("#"))
        if site in ("reddit", "x"):
            url = (f"{SITES['reddit']}/search/?q=" if site == "reddit" else f"{SITES['x']}/search?q=") + \
                urllib.parse.quote_plus(target.strip())
            return _open(ctx, url, site, f"{LABEL[site]} results for {target}")
        url = (f"{SITES['instagram']}/explore/tags/{q}/" if target.strip().startswith("#") else
               f"{SITES['instagram']}/explore/search/keyword/?q={q}") if site == "instagram" else \
            f"{SITES['facebook']}/search/top/?q={q}"
        return _open(ctx, url, site, f"{site.title()} results for {target}")
    if not _linked(ctx):
        raise ToolFailed(SETUP_HINT)
    if site and not _page_action(site, action):
        raise ToolFailed(_cannot(site, action))
    # speaking for you with nothing named (no site, no person): only on the tab you're looking at
    stay = not site and action in SPEAKS_FOR_YOU and not (action == "send_message" and target)
    site, tab = _social_tab(ctx, site, action, stay)
    action = _page_action(site, action) or action

    if action == "open_chat":
        name = _open_chat(ctx, tab, target)
        return Spoken(f"Opened your chat with {name}.")
    if action == "unread":
        return Spoken(_unread(_run(ctx, tab, {"action": "unread"}).get("chats") or []))
    if action == "read_messages":
        if target:
            _open_chat(ctx, tab, target)
        r = _run(ctx, tab, {"action": "read_messages"})
        if site == "whatsapp":
            if r.get("noChat"):
                return Spoken("No chat is open. " + _unread(_run(ctx, tab, {"action": "unread"}).get("chats") or []))
            said = _said(r.get("messages") or [])
        else:
            said = r.get("text", "")
        return Spoken("The latest messages: " + said if said else "This chat is empty.")
    if action == "repost":
        about = _run(ctx, tab, {"action": "read"})
        if about.get("reposted"):
            return Spoken("You've already reposted it.")
        whose = f"{about['author']}'s post" if about.get("author") else "this post"
        words = _short(about.get("text") or "", 90).rstrip(".!?")
        if not _yes(ctx, f"Shall I repost {whose}" + (f": {words}?" if words else "?")):
            return Spoken("Okay, I didn't repost it.")
        r = _run(ctx, tab, {"action": "repost"})
        return Spoken("You've already reposted it." if r.get("already") else "Reposted.")
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
        elif action == "send_message" and site == "whatsapp":      # "reply saying...": say who it goes to
            who = _run(ctx, tab, {"action": "chat_name"}).get("chat") or ""
            if who:
                question = f"Shall I send {who}: {text}?"
        if not _yes(ctx, question):
            return Spoken("Okay, I didn't send anything." if action in ("send_message", "comment") else "Okay, I left it.")
        r = _run(ctx, tab, {"action": action, "text": text}, timeout=20)
        if r.get("sent") is False:
            return Spoken("I typed it, but it didn't go. It's waiting in the message box.")
        return Spoken({"send_message": "Sent.", "comment": "Comment posted.", "follow": "Followed.",
                       "unfollow": "Unfollowed."}[action])

    r = _run(ctx, tab, {"action": action})
    if action == "read":
        return Spoken(_read_text(site, r))
    if r.get("already"):
        if site == "x" and action in ("save", "unsave"):
            return Spoken("It's already bookmarked." if action == "save" else "It isn't bookmarked.")
        return Spoken(ALREADY.get(action, "Done."))
    if action in ("next", "previous"):
        return Spoken(f"{action.title()} {r.get('did') if r.get('did') in ('post', 'reel', 'story') else 'one'}.")
    if site == "x" and action in DONE_X:
        return Spoken(DONE_X[action])
    return Spoken(DONE.get(action, "Done."))


# ------------------------------------------------------------------ fast-path phrases

SITE_RE = r"instagram|insta|facebook|fb|messenger|whats ?app|what's app|reddit|red it|twitter|x"
WA = r"(?:whats ?app|what's app)"
REDDIT = r"(?:reddit|red it|read it)"
NOT_A_NAME = re.compile(r"^(?:the|my|this|that|a|an|it|me|some)\b|\b(?:post|reel|story|stories|video|tweet|chat|"
                        r"messages?|page|tab|link|notifications?|feed|search|results?|app)\b")


def route_social(t: str, has) -> list[tuple[str, dict]] | None:
    if not has("social"):
        return None
    # ---- WhatsApp: who messaged me, read my messages, send one, open WhatsApp Web
    if re.fullmatch(r"(?:who|anyone|anybody) (?:has |have )?(?:messaged|texted|written to|wrote to|sent) me"
                    r"(?: anything| something| a message| messages)? (?:on|in) " + WA +
                    r"|(?:any|are there any|do i have any|did i get any|got any|have i got any)(?: new| unread)? " + WA +
                    r" (?:messages|texts|chats)|(?:any|are there any|do i have any|did i get any|got any|have i got any)"
                    r"(?: new| unread)? (?:messages|texts|chats) (?:on|in) " + WA +
                    r"|(?:check|read|show)(?: me)? (?:my |the )?(?:new|unread) " + WA + r"(?: messages| chats| texts)?"
                    r"|check (?:my )?" + WA + r"(?: messages| chats)?|what's new on " + WA, t):
        return [("social", {"action": "unread", "site": "whatsapp"})]
    if re.fullmatch(r"read(?: me)? (?:my |the )?(?:latest |last |recent )?" + WA + r"(?: messages| chat| texts)?|"
                    r"read(?: me)? (?:my |the )?(?:latest |last |recent )?(?:messages|chat|texts) (?:on|in|from) " + WA, t):
        return [("social", {"action": "read_messages", "site": "whatsapp"})]
    m = re.fullmatch(WA + r"(?: to)? (?P<n>[\w .'-]+?) (?:saying|that) (?P<m>.+)", t) \
        or re.fullmatch(r"send (?:a )?" + WA + r"(?: message| text)? to (?P<n>[\w .'-]+?) (?:saying|that) (?P<m>.+)", t) \
        or re.fullmatch(r"send (?P<n>[\w .'-]+?) an? " + WA + r"(?: message| text)? (?:saying|that) (?P<m>.+)", t)
    if m:
        return [("social", {"action": "send_message", "site": "whatsapp", "target": m.group("n"), "text": m.group("m")})]
    m = re.fullmatch(r"(?:send|reply)(?: a)?(?: message| reply)? on " + WA + r" (?:saying|that) (?P<m>.+)", t)
    if m:
        return [("social", {"action": "send_message", "site": "whatsapp", "text": m.group("m")})]
    if re.fullmatch(r"(?:open|go to|launch|start|load) (?:the )?(?:web " + WA + "|" + WA + r" (?:web|on (?:the )?"
                    r"(?:web|chrome|browser)|in (?:chrome|the browser)))", t):
        return [("social", {"action": "open", "site": "whatsapp"})]

    site = r"(?P<site>" + SITE_RE + ")"
    pages = (r"(?P<page>feed|news feed|home|reels?|explore|messages|dms|inbox|chats|notifications|marketplace|watch|"
             r"videos|groups|friends|stories|story|profile|saved|memories|events|popular|all|bookmarks|trending)")
    m = re.fullmatch(r"(?:open|show|go to|check|take me to)(?: me)? (?:my )?" + site + r"(?:'s)? " + pages, t) or \
        re.fullmatch(r"(?:open|show|go to|check)(?: me)? (?:my |the )?" + pages + r" (?:on|in) " + site, t)
    if m:
        return [("social", {"action": "open", "site": m.group("site"), "page": m.group("page")})]

    # ---- Reddit: "open r slash python", "open the python subreddit", "open python on reddit"
    go = r"(?:open|go to|show me|take me to|visit)(?: me)?"
    m = re.fullmatch(go + r" (?:the )?(?:r slash|subreddit|sub reddit) (?P<sub>[\w' ]+?)(?: subreddit)?"
                     r"(?: (?:on|in) " + REDDIT + ")?", t) \
        or re.fullmatch(go + r" (?:the )?(?P<sub>[\w' ]+?) (?:subreddit|sub reddit)(?: (?:on|in) " + REDDIT + ")?", t)
    if m and m.group("sub") not in ("the", "this", "that", "my", "a", "reddit"):
        return [("social", {"action": "subreddit", "target": m.group("sub")})]
    m = re.fullmatch(go + r" (?:the )?r (?P<sub>\w+(?: \w+)?)", t)    # "r/python" arrives as "r python"
    if m and subreddit_name(m.group("sub")) in POPULAR_SUBS:
        return [("social", {"action": "subreddit", "target": m.group("sub")})]

    # "like this post on instagram": the same commands, aimed at that site's tab
    on = re.fullmatch(r"(?P<rest>.+?) (?:on|in) (?P<site>" + SITE_RE + r"|read it|ex)", t)
    there = {"site": on.group("site")} if on else {}
    named = site_name(on.group("site")) if on else ""
    rest = on.group("rest") if on else t
    video = "|video" if on else ""          # a bare "next video" / "pause the video" stays with the media keys
    loose = r"|this|that|it" if on else ""  # "like this on reddit"

    def aim(noun: str | None) -> dict:      # "this tweet" means X when no site was named
        return there or ({"site": "x"} if noun == "tweet" else {})

    m = re.fullmatch(r"(?P<a>like|unlike|save|unsave|bookmark|unbookmark) (?:(?:this|the|that) (?P<n>post|reel|photo|"
                     r"picture|pic|tweet" + video + ")" + loose + ")", rest)
    if m:
        a = {"bookmark": "save", "unbookmark": "unsave"}.get(m.group("a"), m.group("a"))
        if named == "reddit":
            a = {"like": "upvote", "unlike": "unvote"}.get(a, a)
        return [("social", {"action": a, **aim(m.group("n"))})]
    m = re.fullmatch(r"(?:go to |show me |skip to |scroll to |move to )?(?:the )?(?P<d>next|previous|last) "
                     r"(?P<n>post|reel|story|tweet" + video + ")", rest)
    if m:
        return [("social", {"action": "previous" if m.group("d") in ("previous", "last") else "next", **aim(m.group("n"))})]
    m = re.fullmatch(r"(?:read(?: me| out)?|what does) (?:this|the|that) (?P<n>post|caption|tweet)(?: say)?", rest)
    if m:
        return [("social", {"action": "read", **aim(m.group("n"))})]
    m = re.fullmatch(r"(?P<a>pause|play|resume) (?:the |this )?(?:story|reel" + video + ")", rest)
    if m:
        return [("social", {"action": "pause" if m.group("a") == "pause" else "play", **there})]
    m = re.fullmatch(r"(?P<a>follow|unfollow) (?:this|them|him|her|this account|this page|this person)", rest)
    if m:
        return [("social", {"action": m.group("a"), **there})]
    # ---- Reddit votes and X reposts
    m = re.fullmatch(r"(?P<a>up ?vote|down ?vote)(?: (?:this|that|it))?(?: post| one)?", rest)
    if m:
        return [("social", {"action": "upvote" if m.group("a").startswith("up") else "downvote", **there})]
    if re.fullmatch(r"(?:remove|undo|take back|clear) (?:my |the )?(?:up ?|down ?)?vote(?: on (?:this|that|it))?"
                    r"(?: post)?|unvote(?: this| that| it)?(?: post)?", rest):
        return [("social", {"action": "unvote", **there})]
    if re.fullmatch(r"(?:repost|retweet|re tweet)(?: (?:this|that|it))?(?: post| tweet)?", rest):
        return [("social", {"action": "repost", **there})]
    # ---- chats
    m = re.fullmatch(r"(?:open|show)(?: me)? (?:my |the )?(?P<wa>" + WA + r" )?(?:chat|conversation|messages|dms) "
                     r"with (?P<n>.+)", rest)
    if m:
        return [("social", {"action": "open_chat", "target": m.group("n"),
                            **(there or ({"site": "whatsapp"} if m.group("wa") else {}))})]
    if re.fullmatch(r"read (?:this|the) (?:chat|conversation)", rest):
        return [("social", {"action": "read_messages", **there})]
    m = re.fullmatch(r"read (?:my |the )?(?:chat|conversation) with (?P<n>.+)", rest) or \
        (re.fullmatch(r"read (?:my |the )?messages (?:with|from) (?P<n>.+)", rest) if on else None)
    if m:
        return [("social", {"action": "read_messages", "target": m.group("n"), **there})]
    m = re.fullmatch(r"(?:send|reply)(?: a)?(?: message| reply)? (?:saying|that) (?P<m>.+)", t)
    if m:
        return [("social", {"action": "send_message", "text": m.group("m")})]
    m = re.fullmatch(r"(?:send|dm|message|text|tell) (?P<n>[\w .'-]+?) (?:a message )?on (?P<s>instagram|insta|facebook|"
                     r"messenger|whats ?app|what's app) (?:saying|that) (?P<m>.+)", t)
    if m:
        return [("social", {"action": "send_message", "target": m.group("n"), "text": m.group("m"), "site": m.group("s")})]
    m = re.fullmatch(r"dm (?P<n>[\w .'-]+?) (?:saying|that) (?P<m>.+)", t)
    if m:
        return [("social", {"action": "send_message", "target": m.group("n"), "text": m.group("m")})]
    m = re.fullmatch(r"(?:post a |write a |leave a )?comment (?:saying|that) (?P<m>.+)", t)
    if m:
        return [("social", {"action": "comment", "text": m.group("m")})]
    # ---- "open nasa on instagram" (a profile), "open python on reddit", "open sara on whatsapp" (her chat)
    m = re.fullmatch(r"(?:open|go to) (?P<n>[\w.' -]+?)(?:'s)?(?: profile| account| page)? (?:on|in) " + site, t)
    if m and not NOT_A_NAME.search(m.group("n")):
        where = site_name(m.group("site"))
        if where == "reddit":
            return [("social", {"action": "subreddit", "target": m.group("n")})]
        if where == "whatsapp":
            return [("social", {"action": "open_chat", "site": "whatsapp", "target": m.group("n")})]
        return [("social", {"action": "profile", "site": m.group("site"), "target": m.group("n")})]
    return None
