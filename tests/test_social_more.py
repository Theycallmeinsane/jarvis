"""WhatsApp Web, Reddit and X by voice, against a fake Chrome link (no browser needed)."""
import unittest
from types import SimpleNamespace
from unittest import mock

from jarvis.config import Section, load_config
from jarvis.router import route
from jarvis.tools import chrome, social
from jarvis.tools.builtin import ToolFailed

WA = "https://web.whatsapp.com/"
REDDIT = "https://www.reddit.com/r/python/"
X = "https://x.com/home"
IG = "https://www.instagram.com/"
CHATS = ["Ahmad Khan", "Sara Malik", "Family"]


class FakeChrome:
    """Records what Jarvis asks the extension to do and answers like it would. `urls` are the open tabs; the
    first one is active."""

    def __init__(self, *urls, page=None):
        self.calls, self.urls, self.page = [], list(urls), page or {}
        self.active = 0

    def __call__(self, ctx, cmd, args=None, timeout=15):
        args = args or {}
        self.calls.append((cmd, args))
        if cmd == "tabs":
            return [{"id": i + 1, "windowId": 1, "active": i == self.active, "url": u, "title": "t"}
                    for i, u in enumerate(self.urls)]
        if cmd == "activate":
            self.active = args["tabId"] - 1
            return {"id": args["tabId"]}
        if cmd == "run":
            answer = self.page.get((args["fn"], (args.get("args") or {}).get("action")))
            return answer(args["args"]) if callable(answer) else answer
        return {"id": 9}

    def ran(self, fn):
        return [a.get("args") for c, a in self.calls if c == "run" and a["fn"] == fn]

    def actions(self, fn):
        return [a["action"] for a in self.ran(fn)]

    def opened(self):
        return [a["url"] for c, a in self.calls if c == "open"]


def ctx(answer=None, asked=None):
    def confirm(q):
        if asked is not None:
            asked.append(q)
        return bool(answer)
    return SimpleNamespace(cfg=Section(load_config()), context={"confirm": confirm})


class Linked(unittest.TestCase):
    def link(self, fake):
        self.chrome = fake
        for p in (mock.patch.object(social, "_call", fake), mock.patch.object(social, "_linked", return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        return fake


class SiteTests(unittest.TestCase):
    def test_spoken_site_names(self):
        for spoken, site in [("whatsapp", "whatsapp"), ("whats app", "whatsapp"), ("what's app", "whatsapp"),
                             ("reddit", "reddit"), ("red it", "reddit"), ("read it", "reddit"), ("x", "x"),
                             ("ex", "x"), ("twitter", "x"), ("tweeter", "x"), ("insta", "instagram")]:
            self.assertEqual(social.site_name(spoken), site, spoken)

    def test_tab_urls(self):
        for url, site in [(WA, "whatsapp"), (REDDIT, "reddit"), ("https://old.reddit.com/", "reddit"), (X, "x"),
                          ("https://twitter.com/nasa", "x"), (IG, "instagram"), ("https://www.messenger.com/t/1", "facebook"),
                          ("https://www.netflix.com/browse", ""), ("https://www.whatsapp.com/download", ""),
                          ("https://example.com/x.com", "")]:
            self.assertEqual(social._which(url), site, url)

    def test_subreddit_names(self):
        self.assertEqual(social.subreddit_name("python"), "Python")
        self.assertEqual(social.subreddit_name("r slash machine learning"), "MachineLearning")
        self.assertEqual(social.subreddit_name("the ask reddit subreddit"), "AskReddit")
        self.assertEqual(social.subreddit_name("local llama"), "LocalLLaMA")
        self.assertEqual(social.subreddit_name("tech"), "tech")                  # not stretched to "technology"
        self.assertEqual(social.subreddit_name("rust lang"), "rustlang")


class OpenTests(Linked):
    def test_pages_subreddits_profiles_and_search(self):
        self.link(FakeChrome("https://github.com/"))
        social.social("open", "twitter", "notifications", ctx=ctx())
        social.social("open", "x", "bookmarks", ctx=ctx())
        social.social("open", "reddit", "popular", ctx=ctx())
        self.assertEqual(social.social("subreddit", target="r slash python", ctx=ctx()), "Opened the Python subreddit.")
        social.social("profile", "x", target="elon musk", ctx=ctx())
        social.social("profile", "reddit", target="spez", ctx=ctx())
        social.social("search", "x", target="spacex launch", ctx=ctx())
        social.social("search", "reddit", target="llamas", ctx=ctx())
        self.assertEqual(self.chrome.opened(), [
            "https://x.com/notifications", "https://x.com/i/bookmarks", "https://www.reddit.com/r/popular/",
            "https://www.reddit.com/r/Python/", "https://x.com/elonmusk", "https://www.reddit.com/user/spez/",
            "https://x.com/search?q=spacex+launch", "https://www.reddit.com/search/?q=llamas"])

    def test_my_x_profile_follows_the_link(self):
        self.link(FakeChrome(X, page={("x", "my_profile"): {"ok": True, "url": "https://x.com/me_123"}}))
        self.assertEqual(social.social("open", "x", "profile", ctx=ctx()), "Opened your X profile.")
        self.assertEqual(self.chrome.actions("x"), ["my_profile"])

    def test_whatsapp_is_reused_not_opened_twice(self):
        self.link(FakeChrome(IG, WA))
        self.assertEqual(social.social("open", "whatsapp", ctx=ctx()), "Opened WhatsApp.")
        self.assertEqual(self.chrome.opened(), [])
        self.assertEqual(self.chrome.active, 1)
        self.link(FakeChrome(IG))
        social.social("open", "whatsapp", ctx=ctx())
        self.assertEqual(self.chrome.opened(), ["https://web.whatsapp.com/"])


class WhatsAppTests(Linked):
    def setUp(self):
        self.searched = []

        def search(a):
            self.searched.append(a["text"])
            return {"ok": True, "names": ["Zainab Qureshi", "Zain's Office"]}

        self.link(FakeChrome(WA, page={
            ("whatsapp", "chats"): {"ok": True, "names": CHATS},
            ("whatsapp", "search"): search,
            ("whatsapp", "open_chat"): lambda a: {"ok": True, "name": a.get("want")},
            ("whatsapp", "chat_name"): {"ok": True, "chat": "Sara Malik"},
            ("whatsapp", "send_message"): {"ok": True, "sent": True},
            ("whatsapp", "unread"): {"ok": True, "chats": [{"name": "Ahmad Khan", "count": 3},
                                                           {"name": "Family", "count": 12}]},
            ("whatsapp", "read_messages"): {"ok": True, "chat": "Ahmad Khan", "messages": [
                {"who": "Ahmad Khan", "text": "are you coming to the match?"},
                {"who": "Ahmad Khan", "text": "it starts at 6"},
                {"who": "You", "text": "maybe"}]}}))

    def test_open_a_chat_by_a_misheard_name(self):
        self.assertEqual(social.social("open_chat", "whatsapp", target="ahmed", ctx=ctx()),
                         "Opened your chat with Ahmad Khan.")
        self.assertEqual(self.chrome.ran("whatsapp")[-1], {"action": "open_chat", "name": "0", "want": "Ahmad Khan"})
        self.assertEqual(self.searched, [])

    def test_a_chat_out_of_view_is_searched_for(self):
        self.assertEqual(social.social("open_chat", "whatsapp", target="zainab", ctx=ctx()),
                         "Opened your chat with Zainab Qureshi.")
        self.assertEqual(self.searched, ["zainab"])
        self.assertEqual(self.chrome.ran("whatsapp")[-1], {"action": "open_chat", "name": "0", "want": "Zainab Qureshi"})
        with self.assertRaisesRegex(ToolFailed, "can't find a chat with bilal"):
            social.social("open_chat", "whatsapp", target="bilal", ctx=ctx())

    def test_read_the_open_chat(self):
        self.assertEqual(social.social("read_messages", "whatsapp", ctx=ctx()),
                         "The latest messages: Ahmad Khan: are you coming to the match? it starts at 6. You: maybe.")

    def test_read_with_no_chat_open_lists_new_messages(self):
        self.chrome.page[("whatsapp", "read_messages")] = {"ok": True, "noChat": True}
        self.assertEqual(social.social("read_messages", "whatsapp", ctx=ctx()),
                         "No chat is open. New on WhatsApp: 3 from Ahmad Khan and 12 from Family.")

    def test_who_messaged_me(self):
        self.assertEqual(social.social("unread", ctx=ctx()), "New on WhatsApp: 3 from Ahmad Khan and 12 from Family.")
        self.chrome.page[("whatsapp", "unread")] = {"ok": True, "chats": []}
        self.assertEqual(social.social("unread", "whatsapp", ctx=ctx()), "No new WhatsApp messages.")

    def test_send_needs_a_yes(self):
        asked = []
        self.assertEqual(social.social("send_message", "whatsapp", target="sarah", text="running late",
                                       ctx=ctx(False, asked)), "Okay, I didn't send anything.")
        self.assertEqual(asked, ["Shall I send Sara Malik: running late?"])
        self.assertNotIn("send_message", self.chrome.actions("whatsapp"))
        self.assertEqual(social.social("send_message", "whatsapp", target="sarah", text="running late",
                                       ctx=ctx(True)), "Sent.")
        self.assertEqual(self.chrome.ran("whatsapp")[-1], {"action": "send_message", "text": "running late"})

    def test_reply_names_the_open_chat(self):
        asked = []
        self.assertEqual(social.social("send_message", text="on my way", ctx=ctx(True, asked)), "Sent.")
        self.assertEqual(asked, ["Shall I send Sara Malik: on my way?"])
        self.assertEqual(self.chrome.actions("whatsapp"), ["chat_name", "send_message"])

    def test_a_message_that_did_not_go(self):
        self.chrome.page[("whatsapp", "send_message")] = {"ok": True, "sent": False}
        self.assertIn("waiting in the message box", social.social("send_message", text="hi", ctx=ctx(True)))

    def test_whatsapp_cannot_like_posts(self):
        with self.assertRaisesRegex(ToolFailed, "I can't like posts on WhatsApp"):
            social.social("like", "whatsapp", ctx=ctx())


class RedditTests(Linked):
    def setUp(self):
        self.link(FakeChrome(REDDIT, page={
            ("reddit", "read"): {"ok": True, "title": "Python 3.14 is out", "author": "guido_fan",
                                 "subreddit": "r/Python", "score": "1234", "body": "Free threading by default. " * 30},
            ("reddit", "upvote"): {"ok": True}, ("reddit", "downvote"): {"ok": True, "already": True},
            ("reddit", "unvote"): {"ok": True}, ("reddit", "save"): {"ok": True},
            ("reddit", "next"): {"ok": True, "did": "post"}}))

    def test_read_the_post(self):
        said = social.social("read", ctx=ctx())
        self.assertTrue(said.startswith("r/Python, by guido_fan, 1234 points. Python 3.14 is out. Free threading"), said)
        self.assertLess(len(said), 420)

    def test_votes(self):
        self.assertEqual(social.social("upvote", ctx=ctx()), "Upvoted.")
        self.assertEqual(social.social("like", ctx=ctx()), "Upvoted.")                 # "like this post" on Reddit
        self.assertEqual(social.social("downvote", "reddit", ctx=ctx()), "You've already downvoted it.")
        self.assertEqual(social.social("unvote", ctx=ctx()), "Removed your vote.")
        self.assertEqual(self.chrome.actions("reddit"), ["upvote", "upvote", "downvote", "unvote"])
        self.assertEqual(social.social("save", ctx=ctx()), "Saved.")
        self.assertEqual(social.social("next", ctx=ctx()), "Next post.")

    def test_no_commenting_on_reddit(self):
        with self.assertRaisesRegex(ToolFailed, "I can't comment on Reddit"):
            social.social("comment", "reddit", text="nice", ctx=ctx(True))
        self.assertEqual(self.chrome.ran("reddit"), [])

    def test_upvote_finds_the_reddit_tab(self):
        self.link(FakeChrome(IG, X, REDDIT, page={("reddit", "upvote"): {"ok": True}, ("social", "like"): {"ok": True}}))
        self.assertEqual(social.social("upvote", ctx=ctx()), "Upvoted.")
        self.assertEqual(self.chrome.active, 2)
        self.assertEqual(self.chrome.ran("social"), [])
        self.link(FakeChrome(IG))
        with self.assertRaisesRegex(ToolFailed, "Reddit isn't open in Chrome"):
            social.social("upvote", ctx=ctx())


class XTests(Linked):
    def setUp(self):
        self.link(FakeChrome(X, page={
            ("x", "read"): {"ok": True, "author": "NASA", "handle": "@NASA", "text": "Artemis II is go for launch.",
                            "reposted": False},
            ("x", "like"): {"ok": True}, ("x", "unlike"): {"ok": True, "already": True},
            ("x", "save"): {"ok": True}, ("x", "repost"): {"ok": True},
            ("x", "previous"): {"ok": True, "did": "post"}}))

    def test_read_like_and_bookmark(self):
        self.assertEqual(social.social("read", ctx=ctx()), "NASA: Artemis II is go for launch.")
        self.assertEqual(social.social("like", ctx=ctx()), "Liked.")
        self.assertEqual(social.social("unlike", ctx=ctx()), "You hadn't liked it.")
        self.assertEqual(social.social("save", "twitter", ctx=ctx()), "Bookmarked.")
        self.assertEqual(social.social("previous", ctx=ctx()), "Previous post.")

    def test_reposts_only_after_a_yes(self):
        asked = []
        self.assertEqual(social.social("repost", ctx=ctx(False, asked)), "Okay, I didn't repost it.")
        self.assertEqual(asked, ["Shall I repost NASA's post: Artemis II is go for launch?"])
        self.assertNotIn("repost", self.chrome.actions("x"))
        self.assertEqual(social.social("repost", ctx=ctx(True)), "Reposted.")
        self.assertIn("repost", self.chrome.actions("x"))

    def test_already_reposted_asks_nothing(self):
        self.chrome.page[("x", "read")] = {"ok": True, "author": "NASA", "text": "hi", "reposted": True}
        asked = []
        self.assertEqual(social.social("repost", ctx=ctx(True, asked)), "You've already reposted it.")
        self.assertEqual(asked, [])
        self.assertNotIn("repost", self.chrome.actions("x"))

    def test_no_messages_or_comments_on_x(self):
        with self.assertRaisesRegex(ToolFailed, "I can't comment on X"):
            social.social("comment", "x", text="lol", ctx=ctx(True))
        self.link(FakeChrome(X, IG))           # "reply saying ..." while looking at X: not sent to Instagram
        with self.assertRaisesRegex(ToolFailed, "I can't send messages on X"):
            social.social("send_message", text="lol", ctx=ctx(True))
        with self.assertRaisesRegex(ToolFailed, "I can't comment on X"):
            social.social("comment", text="lol", ctx=ctx(True))
        self.assertEqual(self.chrome.ran("social"), [])
        self.link(FakeChrome("https://github.com/", WA, page={("whatsapp", "chat_name"): {"ok": True, "chat": "Sara"},
                                                              ("whatsapp", "send_message"): {"ok": True}}))
        asked = []                              # from a non-social tab it finds the chat tab, and names who it goes to
        self.assertEqual(social.social("send_message", text="lol", ctx=ctx(True, asked)), "Sent.")
        self.assertEqual(asked, ["Shall I send Sara: lol?"])

    def test_like_this_video_on_x_goes_to_the_post(self):
        fake = FakeChrome(X, page={("youtube", "like"): {"ok": False, "reason": "that only works on YouTube",
                                                         "host": "x.com"}, ("x", "like"): {"ok": True}})
        self.link(fake)
        with mock.patch.object(chrome, "_call", fake):
            self.assertEqual(chrome.chrome_video("like", ctx=ctx()), "Liked.")
        self.assertEqual(fake.actions("x"), ["like"])


class InstagramStillFirst(Linked):
    def test_bare_commands_pick_the_open_social_tab(self):
        self.link(FakeChrome(WA, IG, page={("social", "like"): {"ok": True}, ("social", "chats"): {"ok": True, "names": CHATS},
                                           ("social", "open_chat"): {"ok": True}, ("whatsapp", "chats"): {"ok": True, "names": []},
                                           ("whatsapp", "search"): {"ok": True, "names": []}}))
        self.assertEqual(social.social("like", ctx=ctx()), "Liked.")             # WhatsApp has no posts: Instagram
        self.assertEqual(self.chrome.active, 1)
        self.chrome.active = 0
        with self.assertRaisesRegex(ToolFailed, "can't find a chat with sara"):    # WhatsApp is in front: its chats
            social.social("open_chat", target="sara", ctx=ctx())
        self.assertEqual(self.chrome.actions("whatsapp"), ["chats", "search"])


    def test_naming_the_site_or_person_reaches_a_background_tab(self):
        page = {("whatsapp", "chats"): {"ok": True, "names": CHATS},
                ("whatsapp", "open_chat"): lambda a: {"ok": True, "name": a.get("want")},
                ("whatsapp", "send_message"): {"ok": True, "sent": True},
                ("social", "chats"): {"ok": True, "names": CHATS}, ("social", "open_chat"): {"ok": True},
                ("social", "send_message"): {"ok": True}}
        self.link(FakeChrome(IG, WA, page=dict(page)))
        asked = []
        self.assertEqual(social.social("send_message", "whatsapp", target="ahmed", text="ok", ctx=ctx(True, asked)), "Sent.")
        self.assertEqual((asked, self.chrome.active), (["Shall I send Ahmad Khan: ok?"], 1))
        self.assertEqual(self.chrome.ran("social"), [])
        self.link(FakeChrome(REDDIT, IG, page=dict(page)))       # "DM Sara saying hi" while reading Reddit
        self.assertEqual(social.social("send_message", target="sara", text="hi", ctx=ctx(True)), "Sent.")
        self.assertEqual(self.chrome.actions("social"), ["chats", "open_chat", "send_message"])


class AllTools:
    def get(self, name):
        return object()


class RouteTests(unittest.TestCase):
    def r(self, text):
        return route(text, AllTools())

    def test_whatsapp(self):
        self.assertEqual(self.r("who messaged me on whatsapp"), [("social", {"action": "unread", "site": "whatsapp"})])
        self.assertEqual(self.r("any new whatsapp messages"), [("social", {"action": "unread", "site": "whatsapp"})])
        self.assertEqual(self.r("check my whatsapp"), [("social", {"action": "unread", "site": "whatsapp"})])
        self.assertEqual(self.r("read my whatsapp messages"),
                         [("social", {"action": "read_messages", "site": "whatsapp"})])
        self.assertEqual(self.r("whatsapp sara saying running late"),
                         [("social", {"action": "send_message", "site": "whatsapp", "target": "sara",
                                      "text": "running late"})])
        self.assertEqual(self.r("send a whatsapp to sara saying running late")[0][1]["target"], "sara")
        self.assertEqual(self.r("message ahmed on whatsapp saying see you at 5"),
                         [("social", {"action": "send_message", "target": "ahmed", "text": "see you at 5",
                                      "site": "whatsapp"})])
        self.assertEqual(self.r("open my chat with ahmed on whatsapp"),
                         [("social", {"action": "open_chat", "target": "ahmed", "site": "whatsapp"})])
        self.assertEqual(self.r("open sara on whatsapp"),
                         [("social", {"action": "open_chat", "site": "whatsapp", "target": "sara"})])
        self.assertEqual(self.r("open whatsapp web"), [("social", {"action": "open", "site": "whatsapp"})])
        self.assertEqual(self.r("reply saying on my way"), [("social", {"action": "send_message", "text": "on my way"})])

    def test_reddit(self):
        self.assertEqual(self.r("like this post on reddit"), [("social", {"action": "upvote", "site": "reddit"})])
        self.assertEqual(self.r("upvote this"), [("social", {"action": "upvote"})])
        self.assertEqual(self.r("downvote this post"), [("social", {"action": "downvote"})])
        self.assertEqual(self.r("remove my vote"), [("social", {"action": "unvote"})])
        self.assertEqual(self.r("next post on reddit"), [("social", {"action": "next", "site": "reddit"})])
        for phrase in ("open r slash python", "open r/python", "open the python subreddit", "open python on reddit",
                       "go to r slash python on reddit"):
            self.assertEqual(self.r(phrase), [("social", {"action": "subreddit", "target": "python"})], phrase)
        self.assertEqual(self.r("open popular on reddit"), [("social", {"action": "open", "site": "reddit",
                                                                        "page": "popular"})])

    def test_x(self):
        self.assertEqual(self.r("next tweet"), [("social", {"action": "next", "site": "x"})])
        self.assertEqual(self.r("read this tweet"), [("social", {"action": "read", "site": "x"})])
        self.assertEqual(self.r("like this tweet"), [("social", {"action": "like", "site": "x"})])
        self.assertEqual(self.r("bookmark this tweet"), [("social", {"action": "save", "site": "x"})])
        self.assertEqual(self.r("like this on twitter"), [("social", {"action": "like", "site": "twitter"})])
        self.assertEqual(self.r("repost this"), [("social", {"action": "repost"})])
        self.assertEqual(self.r("open twitter notifications"),
                         [("social", {"action": "open", "site": "twitter", "page": "notifications"})])
        self.assertEqual(self.r("open my x bookmarks"), [("social", {"action": "open", "site": "x", "page": "bookmarks"})])
        self.assertEqual(self.r("open elon musk on twitter"),
                         [("social", {"action": "profile", "site": "twitter", "target": "elon musk"})])
        self.assertEqual(self.r("play the video on twitter"), [("social", {"action": "play", "site": "twitter"})])
        self.assertIsNone(self.r("reply to this tweet saying lol"))             # no replying on X

    def test_instagram_and_facebook_unchanged(self):
        self.assertEqual(self.r("like this post"), [("social", {"action": "like"})])
        self.assertEqual(self.r("next post"), [("social", {"action": "next"})])
        self.assertEqual(self.r("open my chat with ahmed"), [("social", {"action": "open_chat", "target": "ahmed"})])
        self.assertEqual(self.r("like this post on instagram"), [("social", {"action": "like", "site": "instagram"})])
        self.assertEqual(self.r("open nasa on instagram"),
                         [("social", {"action": "profile", "site": "instagram", "target": "nasa"})])
        self.assertEqual(self.r("message sara on instagram saying hi"),
                         [("social", {"action": "send_message", "target": "sara", "text": "hi", "site": "instagram"})])

    def test_other_routes_keep_their_phrases(self):
        self.assertEqual(self.r("next song")[0][0], "media_control")
        self.assertEqual(self.r("pause the video")[0][0], "media_control")
        self.assertEqual(self.r("play the story"), [("play_song", {"song": "the story"})])
        self.assertEqual(self.r("search reddit for llamas"), [("site_search", {"query": "llamas", "site": "reddit"})])
        self.assertEqual(self.r("search for llamas on reddit"), [("site_search", {"query": "llamas", "site": "reddit"})])
        self.assertEqual(self.r("open reddit"), [("open_app", {"name": "reddit"})])
        self.assertEqual(self.r("open whatsapp"), [("open_app", {"name": "whatsapp"})])
        self.assertEqual(self.r("open r studio"), [("open_app", {"name": "r studio"})])
        self.assertEqual(self.r("bookmark this page"), [("chrome_bookmarks", {"action": "add"})])
        self.assertEqual(self.r("bookmark this"), [("chrome_bookmarks", {"action": "add"})])
        for phrase in ("read it", "read it on reddit", "read it to me", "next video", "any new messages",
                       "who messaged me", "like this"):
            self.assertIsNone(self.r(phrase), phrase)


if __name__ == "__main__":
    unittest.main()
