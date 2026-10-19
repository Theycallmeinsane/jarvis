"""Bookmarks, history, YouTube buttons, hands-free scrolling and site search, against a fake Chrome link."""
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from jarvis import chrome_bridge
from jarvis.config import Section, load_config
from jarvis.router import route
from jarvis.tools import browse, chrome, social
from jarvis.tools.builtin import ToolFailed

NOW = int(time.time() * 1000)
DAY = 86400000

BOOKMARKS = [
    {"title": "Online Banking | HBL", "url": "https://www.hbl.com/personal", "folder": "Money", "added": 1},
    {"title": "GitHub - Theycallmeinsane/jarvis: Local voice assistant", "url": "https://github.com/Theycallmeinsane/jarvis",
     "folder": "Bookmarks bar", "added": 3},
    {"title": "Ollama Library", "url": "https://ollama.com/library", "folder": "Bookmarks bar", "added": 2},
]
HISTORY = [
    {"title": "rust ownership - Google Search", "url": "https://www.google.com/search?q=rust+ownership",
     "last": NOW - 1000, "visits": 1},
    {"title": "What is Ownership? - The Rust Programming Language", "url": "https://doc.rust-lang.org/book/ch04-01.html",
     "last": NOW - 5000, "visits": 2},
    {"title": "Sourdough starter guide", "url": "https://www.kingarthurbaking.com/sourdough", "last": NOW - DAY,
     "visits": 1},
    {"title": "YouTube", "url": "https://www.youtube.com/", "last": NOW - 2000, "visits": 9},
]


class FakeChrome:
    def __init__(self, url="https://www.youtube.com/watch?v=x", page=None, bookmarks=None, history=None):
        self.calls, self.url, self.page = [], url, page or {}
        self.bookmarks = list(BOOKMARKS if bookmarks is None else bookmarks)
        self.history = list(HISTORY if history is None else history)

    def __call__(self, ctx, cmd, args=None, timeout=15):
        args = args or {}
        self.calls.append((cmd, args))
        if cmd == "tabs":
            return [{"id": 1, "windowId": 1, "active": True, "url": self.url, "title": "x"},
                    {"id": 2, "windowId": 1, "active": False, "url": "https://news.example/long", "title": "y"}]
        if cmd == "bookmarks":
            if args.get("action") == "add":
                return {"added": True, "title": "This page", "url": self.url}
            return self.bookmarks
        if cmd == "history":
            words = (args.get("text") or "").lower().split()
            return [h for h in self.history if all(w in (h["title"] + h["url"]).lower() for w in words)]
        if cmd == "run":
            answer = self.page.get((args["fn"], (args.get("args") or {}).get("action")))
            return answer(args) if callable(answer) else answer
        return {"id": 3}

    def opened(self):
        return [a["url"] for c, a in self.calls if c == "open"]

    def ran(self, fn):
        return [a.get("args") for c, a in self.calls if c == "run" and a["fn"] == fn]


def ctx(answer=None, asked=None):
    def confirm(q):
        if asked is not None:
            asked.append(q)
        return bool(answer)
    return SimpleNamespace(cfg=Section(load_config()), context={"confirm": confirm})


class Linked(unittest.TestCase):
    chrome = None

    def link(self, fake):
        self.chrome = fake
        for mod in (browse, chrome, social):
            for p in (mock.patch.object(mod, "_call", fake), mock.patch.object(mod, "_linked", return_value=True)):
                p.start()
                self.addCleanup(p.stop)


class BookmarkTests(Linked):
    def setUp(self):
        self.link(FakeChrome())

    def test_open_by_rough_name(self):
        self.assertEqual(browse.chrome_bookmarks("open", "bank", ctx=ctx()), "Opening Online Banking | HBL.")
        self.assertEqual(browse.chrome_bookmarks("open", "jarvis repo", ctx=ctx()).split(" -")[0], "Opening GitHub")
        self.assertEqual(browse.chrome_bookmarks("open", "olama", ctx=ctx()), "Opening Ollama Library.")   # misheard
        self.assertEqual(self.chrome.opened(), ["https://www.hbl.com/personal", "https://github.com/Theycallmeinsane/jarvis",
                                                "https://ollama.com/library"])

    def test_unknown_name_says_what_is_close(self):
        with self.assertRaises(ToolFailed) as e:
            browse.chrome_bookmarks("open", "netflix", ctx=ctx())
        self.assertIn("I don't see a bookmark for netflix", str(e.exception))
        self.assertEqual(self.chrome.opened(), [])

    def test_add_list_and_manager(self):
        self.assertEqual(browse.chrome_bookmarks("add", ctx=ctx()), "Bookmarked This page.")
        said = browse.chrome_bookmarks("list", ctx=ctx())
        self.assertTrue(said.startswith("You have 3 bookmarks. The newest: GitHub"), said)
        browse.chrome_bookmarks("manager", ctx=ctx())
        self.assertEqual(self.chrome.opened(), ["chrome://bookmarks"])

    def test_no_bookmarks(self):
        self.link(FakeChrome(bookmarks=[]))
        self.assertIn("don't have any bookmarks", browse.chrome_bookmarks("open", "bank", ctx=ctx()))


class HistoryTests(Linked):
    def setUp(self):
        self.link(FakeChrome())

    def test_opens_the_page_not_the_search(self):
        said = browse.chrome_history("open", "rust ownership", ctx=ctx())
        self.assertEqual(self.chrome.opened(), ["https://doc.rust-lang.org/book/ch04-01.html"])
        self.assertTrue(said.startswith("Opening What is Ownership?"), said)
        self.assertTrue(said.endswith("earlier today."), said)

    def test_misheard_words_still_match(self):
        browse.chrome_history("open", "sour dough", ctx=ctx())          # Chrome's own search finds nothing
        self.assertEqual(self.chrome.opened(), ["https://www.kingarthurbaking.com/sourdough"])

    def test_nothing_close(self):
        with self.assertRaises(ToolFailed):
            browse.chrome_history("open", "quantum chromodynamics", ctx=ctx())
        self.assertEqual(self.chrome.opened(), [])

    def test_recent_summary(self):
        said = browse.chrome_history("recent", when="today", ctx=ctx())
        self.assertTrue(said.startswith("Today you visited"), said)
        self.assertIn("youtube.com", said)
        since, until, _ = browse._window("yesterday")
        self.assertEqual(until - since, DAY)


class YouTubeTests(Linked):
    def test_buttons(self):
        self.link(FakeChrome(page={("youtube", "skip_ad"): {"ok": True},
                                   ("youtube", "captions_on"): {"ok": True, "on": True},
                                   ("youtube", "captions_off"): {"ok": True, "already": True},
                                   ("youtube", "like"): {"ok": True}, ("youtube", "theater"): {"ok": True, "on": True}}))
        self.assertEqual(chrome.chrome_video("skip_ad", ctx=ctx()), "Skipped the ad.")
        self.assertEqual(chrome.chrome_video("captions_on", ctx=ctx()), "Captions on.")
        self.assertEqual(chrome.chrome_video("captions_off", ctx=ctx()), "Captions are already off.")
        self.assertEqual(chrome.chrome_video("like", ctx=ctx()), "Liked.")
        self.assertEqual(chrome.chrome_video("theater", ctx=ctx()), "Theater mode on.")

    def test_unskippable_ad(self):
        self.link(FakeChrome(page={("youtube", "skip_ad"): {"ok": False, "reason": "this ad can't be skipped yet"}}))
        with self.assertRaises(ToolFailed) as e:
            chrome.chrome_video("skip_ad", ctx=ctx())
        self.assertIn("can't be skipped yet", str(e.exception))

    def test_subscribe_asks_first(self):
        self.link(FakeChrome(page={("youtube", "channel"): {"ok": True, "channel": "Lofi Girl"},
                                   ("youtube", "subscribe"): {"ok": True}}))
        asked = []
        self.assertEqual(chrome.chrome_video("subscribe", ctx=ctx(False, asked)), "Okay, I didn't subscribe.")
        self.assertEqual(asked, ["Shall I subscribe to Lofi Girl?"])
        self.assertEqual([a["action"] for a in self.chrome.ran("youtube")], ["channel"])
        self.assertEqual(chrome.chrome_video("subscribe", ctx=ctx(True)), "Subscribed.")
        self.chrome.page[("youtube", "channel")] = {"ok": True, "channel": "Lofi Girl", "subscribed": True}
        asked.clear()
        self.assertEqual(chrome.chrome_video("subscribe", ctx=ctx(True, asked)), "You're already subscribed.")
        self.assertEqual(asked, [])

    def test_like_on_a_reel_goes_to_instagram(self):
        self.link(FakeChrome(url="https://www.instagram.com/reels/abc/", page={
            ("youtube", "like"): {"ok": False, "reason": "that only works on YouTube", "host": "www.instagram.com"},
            ("social", "like"): {"ok": True}}))
        self.assertEqual(chrome.chrome_video("like", ctx=ctx()), "Liked.")
        self.assertEqual(self.chrome.ran("social"), [{"action": "like"}])


class ScrollAndSearchTests(Linked):
    def test_autoscroll_and_stop_from_another_tab(self):
        fake = FakeChrome(url="https://news.example/long", page={
            ("autoscroll", "start"): {"ok": True, "running": True},
            ("autoscroll", "stop"): lambda a: {"ok": True, "was": a.get("tabId") == 2}})
        self.link(fake)
        self.assertIn("Scrolling", browse.chrome_autoscroll("start", ctx=ctx()))
        fake.url = "https://other.example/"                   # switched tabs since
        fake.calls.clear()
        self.assertEqual(browse.chrome_autoscroll("stop", ctx=ctx()), "Stopped scrolling.")
        self.assertEqual([a.get("tabId") for c, a in fake.calls if c == "run"], [None, 2])

    def test_site_search_and_fallback(self):
        self.link(FakeChrome(page={("sitesearch", None): {"ok": True, "host": "daraz.pk"}}))
        self.assertEqual(browse.search_this_site("usb cable", ctx=ctx()), "Searching daraz.pk for usb cable.")
        self.link(FakeChrome(page={("sitesearch", None): {"ok": True, "host": "www.daraz.pk"}}))
        self.assertEqual(browse.search_this_site("usb cable", ctx=ctx()), "Searching daraz.pk for usb cable.")
        self.link(FakeChrome(page={("sitesearch", None): {"ok": False, "reason": "no search box", "host": "blog.example"}}))
        self.assertIn("searched it on Google", browse.search_this_site("refunds", ctx=ctx()))
        self.assertEqual(self.chrome.opened(), ["https://www.google.com/search?q=site%3Ablog.example+refunds"])


class OldExtensionTests(unittest.TestCase):
    def test_unknown_command_means_reload(self):
        bridge = mock.Mock()
        bridge.call.side_effect = RuntimeError("Unknown command bookmarks")
        with mock.patch.object(chrome, "_bridge", return_value=bridge):
            with self.assertRaises(ToolFailed) as e:
                chrome._call(None, "bookmarks", {"action": "list"})
        self.assertIn("reload", str(e.exception))

    def test_outdated_version(self):
        b = chrome_bridge.Bridge()
        with mock.patch.object(chrome_bridge, "bundled_version", return_value="1.2.0"):
            b.version = "1.1.0"
            self.assertTrue(b.outdated)
            b.version = "1.2.0"
            self.assertFalse(b.outdated)
            b.version = ""
            self.assertFalse(b.outdated)
        self.assertEqual(chrome_bridge.bundled_version(), "1.3.0")


class AllTools:
    def get(self, name):
        return object()


class RouteTests(unittest.TestCase):
    def r(self, text):
        return route(text, AllTools())

    def test_phrases(self):
        self.assertEqual(self.r("skip the ad"), [("chrome_video", {"action": "skip_ad"})])
        self.assertEqual(self.r("turn subtitles off"), [("chrome_video", {"action": "captions_off"})])
        self.assertEqual(self.r("like this video"), [("chrome_video", {"action": "like"})])
        self.assertEqual(self.r("subscribe to this channel"), [("chrome_video", {"action": "subscribe"})])
        self.assertEqual(self.r("bookmark this page"), [("chrome_bookmarks", {"action": "add"})])
        self.assertEqual(self.r("open my bank bookmark"), [("chrome_bookmarks", {"action": "open", "name": "bank"})])
        self.assertEqual(self.r("open the video i was watching yesterday about sourdough"),
                         [("chrome_history", {"action": "open", "query": "sourdough", "when": "yesterday"})])
        self.assertEqual(self.r("what sites did i visit today"), [("chrome_history", {"action": "recent", "when": "today"})])
        self.assertEqual(self.r("keep scrolling"), [("chrome_autoscroll", {"action": "start"})])
        self.assertEqual(self.r("stop scrolling"), [("chrome_autoscroll", {"action": "stop"})])
        self.assertEqual(self.r("search this site for usb cables"), [("search_this_site", {"query": "usb cables"})])
        self.assertEqual(self.r("search this page for pricing"), [("chrome_find", {"text": "pricing"})])
        # still theirs
        self.assertEqual(self.r("save this post"), [("social", {"action": "save"})])
        self.assertEqual(self.r("skip this song")[0][0], "media_control")
        self.assertEqual(self.r("scroll down"), [("chrome_page", {"action": "scroll_down"})])

    def test_registered(self):
        from jarvis.tools import ToolBox

        box = ToolBox(Section(load_config()))
        for name in ("chrome_bookmarks", "chrome_history", "chrome_autoscroll", "search_this_site"):
            self.assertIsNotNone(box.get(name), name)


if __name__ == "__main__":
    unittest.main()
