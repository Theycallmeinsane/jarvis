"""Misheard names, Instagram/Facebook and web surfing, against a fake Chrome link (no browser needed)."""
import unittest
from types import SimpleNamespace
from unittest import mock

from jarvis import fuzzy
from jarvis.config import Section, load_config
from jarvis.router import route
from jarvis.tools import ToolBox
from jarvis.tools import builtin, chrome, social, web
from jarvis.tools.builtin import ToolFailed


class AllTools:
    def get(self, name):
        return object()


class FakeChrome:
    """Records what Jarvis asks the extension to do and answers like it would."""

    def __init__(self, url="https://www.instagram.com/", page=None):
        self.calls, self.url, self.page = [], url, page or {}

    def __call__(self, ctx, cmd, args=None, timeout=15):
        self.calls.append((cmd, args or {}))
        if cmd == "tabs":
            return [{"id": 1, "windowId": 1, "active": True, "url": self.url, "title": "x"}]
        if cmd == "run":
            answer = self.page.get((args["fn"], (args.get("args") or {}).get("action")))
            return answer(args["args"]) if callable(answer) else answer
        return {"id": 2}

    def ran(self, fn):
        return [a.get("args") for c, a in self.calls if c == "run" and a["fn"] == fn]


def ctx(yes=None):
    return SimpleNamespace(cfg=Section(load_config()), context={"confirm": (lambda q: yes.append(q) or True)
                                                                if yes is not None else (lambda q: False)})


class FuzzyTests(unittest.TestCase):
    def test_spacing_and_sound_alikes(self):
        for spoken, real in [("insta gram", "instagram"), ("you tube", "youtube"), ("red it", "reddit"),
                             ("git hub", "github"), ("v s code", "vs code"), ("ahmed", "Ahmad Khan"),
                             ("mohammed", "Muhammad Ali"), ("sarah", "Sara Malik"), ("spotty", "spotify")]:
            self.assertGreaterEqual(fuzzy.score(spoken, real), 0.75, (spoken, real))
        for spoken, real in [("netflix", "notepad"), ("instagram", "facebook"), ("zebra", "Sara Malik")]:
            self.assertLess(fuzzy.score(spoken, real), 0.6, (spoken, real))

    def test_best_picks_or_refuses(self):
        self.assertEqual(fuzzy.best("face book", ["instagram", "facebook", "linkedin"]), "facebook")
        self.assertIsNone(fuzzy.best("netflix", ["notepad", "calculator"]))
        self.assertEqual(fuzzy.close("good mourning", ["good morning", "movie mode"]), ["good morning"])

    def test_numbers(self):
        self.assertEqual([fuzzy.number(x) for x in ("7", "seven", "twenty one", "number 12", "the third", "last")],
                         [7, 7, 21, 12, 3, -1])
        self.assertIsNone(fuzzy.number("hello"))

    def test_known_names_fixed_in_sentences(self):
        self.assertEqual(fuzzy.fix_names("songs by jo gee", ["Joji"]), "songs by Joji")
        self.assertEqual(fuzzy.fix_names("message ahmed about lunch", ["Ahmad"]), "message Ahmad about lunch")
        self.assertEqual(fuzzy.fix_names("open the red cup", ["Reddit"]), "open the red cup")      # ordinary words stay

    def test_sites_tabs_and_apps(self):
        self.assertEqual(chrome._url("insta gram"), "https://instagram.com")
        self.assertTrue(chrome._url("weather in lahore").startswith("https://www.google.com/search"))
        tabs = [{"id": 1, "title": "Inbox - Gmail", "url": "https://mail.google.com/"},
                {"id": 2, "title": "lofi - YouTube", "url": "https://www.youtube.com/watch?v=x"}]
        self.assertEqual(chrome.find_tab(tabs, "you tube")["id"], 2)
        apps = [{"Name": "Spotify", "AppID": "s"}, {"Name": "Visual Studio Code", "AppID": "v"}]
        with mock.patch.object(builtin, "_apps", return_value=apps):
            self.assertEqual(builtin._match_app("spotty")["AppID"], "s")
            with mock.patch.object(builtin.webbrowser, "open") as opened:
                self.assertEqual(builtin.open_app("insta gram"), "Opened Instagram in the browser.")
            self.assertEqual(opened.call_args[0][0], "https://instagram.com")


class SocialTests(unittest.TestCase):
    def setUp(self):
        self.chrome = FakeChrome(page={
            ("social", "chats"): {"ok": True, "names": ["Ahmad Khan", "Sara Malik", "Hamza Ali"]},
            ("social", "open_chat"): lambda a: {"ok": True, "name": ["Ahmad Khan", "Sara Malik", "Hamza Ali"][int(a["name"])]},
            ("social", "send_message"): {"ok": True}, ("social", "like"): {"ok": True},
            ("social", "read"): {"ok": True, "author": "sara_k", "text": "Sunset at the beach."},
            ("social", "next"): {"ok": True, "did": "reel"}})
        for p in (mock.patch.object(social, "_call", self.chrome), mock.patch.object(social, "_linked", return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def test_names(self):
        self.assertEqual(social.site_name("insta"), "instagram")
        self.assertEqual(social.site_name("face book"), "facebook")
        self.assertEqual(social.page_name("instagram", "my dms"), "messages")
        self.assertEqual(social.page_name("facebook", "market place"), "marketplace")

    def test_open_pages_profiles_and_search(self):
        social.social("open", "facebook", "marketplace", ctx=ctx())
        social.social("profile", "instagram", target="nasa", ctx=ctx())
        social.social("search", "instagram", target="#sunset", ctx=ctx())
        urls = [a["url"] for c, a in self.chrome.calls if c == "open"]
        self.assertEqual(urls, ["https://www.facebook.com/marketplace/", "https://www.instagram.com/nasa/",
                                "https://www.instagram.com/explore/tags/sunset/"])

    def test_post_actions(self):
        self.assertEqual(social.social("like", ctx=ctx()), "Liked.")
        self.assertEqual(social.social("read", ctx=ctx()), "sara_k: Sunset at the beach.")
        self.assertEqual(social.social("next", ctx=ctx()), "Next reel.")

    def test_chat_by_misheard_name_and_confirmed_send(self):
        asked = []
        self.assertEqual(social.social("send_message", target="ahmed", text="see you at 5", ctx=ctx(asked)), "Sent.")
        self.assertEqual(asked, ["Shall I send Ahmad Khan: see you at 5?"])
        self.assertEqual(self.chrome.ran("social")[-2], {"action": "open_chat", "name": "0"})
        self.assertEqual(self.chrome.ran("social")[-1], {"action": "send_message", "text": "see you at 5"})

    def test_nothing_is_sent_without_a_yes(self):
        self.assertEqual(social.social("send_message", text="hi", ctx=ctx()), "Okay, I didn't send anything.")
        self.assertNotIn({"action": "send_message", "text": "hi"}, self.chrome.ran("social"))
        with self.assertRaisesRegex(ToolFailed, "can't find a chat with zebra"):
            social.social("open_chat", target="zebra", ctx=ctx())

    def test_needs_a_social_tab(self):
        self.chrome.url = "https://github.com/"
        with self.assertRaisesRegex(ToolFailed, "isn't open in Chrome"):
            social.social("like", ctx=ctx())


class WebTests(unittest.TestCase):
    def setUp(self):
        self.chrome = FakeChrome(url="https://www.google.com/search?q=lofi", page={
            ("results", "list"): {"ok": True, "results": [{"title": "Lofi Girl", "url": "u1"}, {"title": "Wiki", "url": "u2"}]},
            ("results", "open"): lambda a: {"ok": True, "title": ["Lofi Girl", "Wiki"][a["n"] - 1]},
            ("hints", "show"): {"count": 12}, ("hints", "label"): {"ok": True, "label": "Buy now"},
            ("hints", "click"): {"ok": True, "label": "Buy now"}})
        for p in (mock.patch.object(web, "_call", self.chrome), mock.patch.object(web, "_linked", return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def opened(self):
        return [a["url"] for c, a in self.chrome.calls if c == "open"]

    def test_site_search(self):
        self.assertEqual(web.site_search("lofi beats", "you tube", ctx=ctx()), "Searching YouTube for lofi beats.")
        web.site_search("usb c cable", "amazon dot com", ctx=ctx())
        web.site_search("liberty market lahore", "maps", ctx=ctx())
        self.assertEqual(self.opened(), ["https://www.youtube.com/results?search_query=lofi+beats",
                                         "https://www.amazon.com/s?k=usb+c+cable",
                                         "https://www.google.com/maps/search/liberty%20market%20lahore"])
        with self.assertRaisesRegex(ToolFailed, "don't know how to search"):
            web.site_search("x", "my fridge", ctx=ctx())

    def test_search_fixes_known_names(self):
        box = ctx()
        box.cfg = Section({**load_config(), "stt": {"hints": "Joji, Spotify"}})
        self.assertEqual(web.site_search("jo gee songs", "youtube", ctx=box), "Searching YouTube for Joji songs.")

    def test_results_and_numbered_links(self):
        self.assertEqual(web.chrome_results("read", ctx=ctx()), "Top results: 1: Lofi Girl. 2: Wiki.")
        self.assertEqual(web.chrome_results("open", 2, ctx=ctx()), "Opening Wiki.")
        self.assertEqual(web.chrome_results("open", -1, ctx=ctx()), "Opening Wiki.")
        self.assertIn("numbered 12", web.chrome_links("show", ctx=ctx()))
        self.assertEqual(web.chrome_links("click", 4, ctx=ctx()), "Okay, I didn't click it.")    # "Buy now" asks
        self.assertNotIn({"action": "click", "n": 4}, self.chrome.ran("hints"))

    def test_random_site(self):
        self.assertEqual(web.random_site("article", ctx=ctx()), "Here's a random Wikipedia article.")
        self.assertEqual(self.opened(), ["https://en.wikipedia.org/wiki/Special:Random"])
        web.random_site("game", ctx=ctx())
        self.assertIn(self.opened()[-1], [u for _, u, _ in web.RANDOM_SITES["fun"]])


class RouteTests(unittest.TestCase):
    def r(self, text):
        return route(text, AllTools())

    def test_social_phrases(self):
        self.assertEqual(self.r("open instagram reels"), [("social", {"action": "open", "site": "instagram", "page": "reels"})])
        self.assertEqual(self.r("show my facebook marketplace"),
                         [("social", {"action": "open", "site": "facebook", "page": "marketplace"})])
        self.assertEqual(self.r("like this post"), [("social", {"action": "like"})])
        self.assertEqual(self.r("next reel"), [("social", {"action": "next"})])
        self.assertEqual(self.r("open my chat with ahmed"), [("social", {"action": "open_chat", "target": "ahmed"})])
        self.assertEqual(self.r("dm sara saying hi there"),
                         [("social", {"action": "send_message", "target": "sara", "text": "hi there"})])
        self.assertEqual(self.r("comment saying great shot"), [("social", {"action": "comment", "text": "great shot"})])
        self.assertIsNone(self.r("send an email to bob saying hi"))           # email stays with the brain

    def test_naming_the_site(self):
        self.assertEqual(self.r("like this post on instagram"), [("social", {"action": "like", "site": "instagram"})])
        self.assertEqual(self.r("skip to the next video on facebook"),
                         [("social", {"action": "next", "site": "facebook"})])
        self.assertEqual(self.r("play the reel on insta"), [("social", {"action": "play", "site": "insta"})])
        self.assertEqual(self.r("follow this account on instagram"),
                         [("social", {"action": "follow", "site": "instagram"})])
        # without a site, videos and songs stay with the media keys and Spotify
        self.assertIsNone(self.r("next video"))
        self.assertEqual(self.r("pause the video")[0][0], "media_control")
        self.assertEqual(self.r("play the story"), [("play_song", {"song": "the story"})])

    def test_surfing_phrases(self):
        self.assertEqual(self.r("show links"), [("chrome_links", {"action": "show"})])
        self.assertEqual(self.r("click seven"), [("chrome_links", {"action": "click", "number": 7})])
        self.assertEqual(self.r("open the second result"), [("chrome_results", {"action": "open", "number": 2})])
        self.assertEqual(self.r("read the results"), [("chrome_results", {"action": "read"})])
        self.assertEqual(self.r("find pricing on this page"), [("chrome_find", {"text": "pricing"})])
        self.assertEqual(self.r("surprise me"), [("random_site", {"kind": "any"})])
        self.assertEqual(self.r("search for lofi beats on you tube"),
                         [("site_search", {"query": "lofi beats", "site": "youtube"})])
        self.assertEqual(self.r("search amazon for usb c cable"),
                         [("site_search", {"query": "usb c cable", "site": "amazon"})])
        self.assertEqual(self.r("next song")[0][0], "media_control")

    def test_tools_are_registered(self):
        box = ToolBox(load_config())
        for name in ("social", "site_search", "chrome_links", "chrome_results", "chrome_find", "random_site"):
            self.assertIsNotNone(box.get(name), name)


if __name__ == "__main__":
    unittest.main()
