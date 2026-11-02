"""Searching and reading the web without a browser: spoken requests become clean queries, engines fall back to one
another, the top pages are read for the passages that answer the question, and results stay around for "open the
second one". Everything here is offline: engines and pages are faked."""
import time
import unittest
from unittest import mock

from jarvis import router, websearch, webtext
from jarvis.config import load_config
from jarvis.tools import ToolBox
from jarvis.websearch import Result, SearchError, WebSearch

ARTICLE = """<html><head><title>Ignored</title><meta property="og:title" content="GPU prices fall"></head><body>
<nav><a href="/">Home</a> | <a href="/about">About</a></nav>
<div class="cookie-banner">We use cookies to improve your experience on this website please accept them</div>
<script>var tracking = "ignore me";</script>
<article><h1>GPU prices fall in November</h1>
<p>Graphics card prices dropped sharply this month as new stock arrived in stores across the country.</p>
<p>The RTX 5070 now sells for 499 dollars, down from 599 dollars at launch, according to retailers.</p>
<p>Analysts expect prices to stay low until the holiday season, when demand usually rises again.</p>
<p>Read more about <a href="https://example.com/ram">RAM prices</a> in our other article on memory.</p>
</article><footer>Copyright 2026 Example News. All rights reserved. Contact us any time you like.</footer>
</body></html>"""


class ExtractTests(unittest.TestCase):
    def test_article_text_without_the_clutter(self):
        page = webtext.extract(ARTICLE, "https://news.example.com/gpu")
        self.assertEqual(page.title, "GPU prices fall")
        text = page.text
        self.assertIn("RTX 5070 now sells for 499 dollars", text)
        for junk in ("cookies", "tracking", "Copyright", "Home"):
            self.assertNotIn(junk, text)
        self.assertIn(("RAM prices", "https://example.com/ram"), page.links)

    def test_passages_answer_the_question(self):
        page = webtext.extract(ARTICLE)
        best = webtext.passages(page.blocks, "how much does the rtx 5070 cost", k=1, size=20)
        self.assertEqual(len(best), 1)
        self.assertIn("499 dollars", best[0])

    def test_summary_is_marked_as_untrusted(self):
        out = webtext.summary_text(webtext.extract(ARTICLE, "https://news.example.com/gpu"), "price")
        self.assertTrue(out.startswith(webtext.UNTRUSTED))
        self.assertIn("news.example.com", out)

    def test_local_network_and_odd_schemes_are_refused(self):
        for url in ("http://127.0.0.1:11434/api/tags", "localhost:8080", "file:///C:/Windows/win.ini",
                    "http://192.168.1.1/admin"):
            with self.assertRaises(webtext.WebError, msg=url):
                webtext.fetch(url)

    def test_redirect_to_local_network_is_refused(self):
        class Reply:
            url, status_code, headers, encoding = "http://127.0.0.1/secret", 200, {"Content-Type": "text/html"}, "utf-8"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("jarvis.webtext._check_host", side_effect=[None, webtext.WebError("local")]), \
                mock.patch("requests.get", return_value=Reply()):
            with self.assertRaises(webtext.WebError):
                webtext.fetch("https://example.com")

    def test_spoken_addresses(self):
        self.assertEqual(webtext.normalize_url("bbc dot com"), "https://bbc.com")
        self.assertEqual(webtext.normalize_url("www.bbc.co.uk/news"), "https://www.bbc.co.uk/news")


class QueryTests(unittest.TestCase):
    def test_clean_queries(self):
        cases = {"hey jarvis can you google the best budget gpu for me please": "the best budget gpu",
                 "search the web for cheap flights to rome": "cheap flights to rome",
                 "look up who won the match last night": "who won the match last night",
                 "could you find out the population of lahore online": "the population of lahore"}
        for spoken, query in cases.items():
            self.assertEqual(websearch.clean_query(spoken), query, spoken)

    def test_freshness(self):
        self.assertEqual(websearch.timelimit("who won the match yesterday"), "d")
        self.assertEqual(websearch.timelimit("latest iphone price"), "w")
        self.assertIsNone(websearch.timelimit("how tall is mount everest"))


def fake_engines(**engines):
    """Replace the real engines; unnamed ones are 'not set up'."""
    table = {name: (lambda *a: None) for name in websearch.ENGINES}
    table.update(engines)
    return mock.patch.dict(websearch.ENGINES, table)


RESULTS = [Result("GPU prices fall", "https://news.example.com/gpu?utm=1", "Prices dropped this month."),
           Result("GPU prices fall", "https://news.example.com/gpu", "Duplicate."),
           Result("Spam", "https://spam.example/gpu", "Buy now."),
           Result("RTX 5070 review", "https://reviews.example.org/5070", "Fast and quiet.")]


class SearchTests(unittest.TestCase):
    def searcher(self, **cfg):
        ws = WebSearch({"blocked_sites": ["spam.example"], **cfg})
        page = webtext.extract(ARTICLE, "https://news.example.com/gpu")
        ws.read = mock.Mock(return_value=page)
        return ws

    def test_lookup_reads_the_top_pages(self):
        ws = self.searcher()
        with fake_engines(ddgs=lambda *a: list(RESULTS)):
            out = ws.lookup("hey jarvis how much does the rtx 5070 cost")
        self.assertIn("1. GPU prices fall [news.example.com]", out)
        self.assertIn("2. RTX 5070 review [reviews.example.org]", out)
        self.assertNotIn("spam", out.lower())                       # blocked site
        self.assertEqual(out.count("GPU prices fall ["), 1)          # duplicate dropped
        self.assertIn("499 dollars", out)                           # read from the page, not the snippet
        self.assertIn(webtext.UNTRUSTED, out)
        self.assertEqual(len(ws.last), 2)

    def test_falls_back_to_the_next_engine(self):
        ws = self.searcher(read_top=0)
        boom = mock.Mock(side_effect=ConnectionError("down"))
        with fake_engines(searxng=boom, ddgs=lambda *a: list(RESULTS)):
            ws.engines = ["searxng", "ddgs"]
            self.assertIn("GPU prices fall", ws.lookup("gpu prices"))
        boom.assert_called_once()

    def test_every_engine_failing_is_reported(self):
        ws = self.searcher()
        with fake_engines(ddgs=mock.Mock(side_effect=ConnectionError("no network"))):
            with self.assertRaises(SearchError):
                ws.search("gpu prices")

    def test_nothing_recent_tries_without_the_time_limit(self):
        ws = self.searcher(read_top=0)
        engine = mock.Mock(side_effect=lambda cfg, q, n, news, fresh: [] if fresh else list(RESULTS))
        with fake_engines(ddgs=engine):
            self.assertTrue(ws.search("latest gpu prices"))
        self.assertEqual([c.args[4] for c in engine.call_args_list], ["w", None])

    def test_results_are_cached(self):
        ws = self.searcher(read_top=0)
        engine = mock.Mock(return_value=list(RESULTS))
        with fake_engines(ddgs=engine):
            ws.search("gpu prices")
            ws.search("gpu prices")
        engine.assert_called_once()

    def test_follow_ups_use_the_last_results(self):
        ws = self.searcher(read_top=0)
        with fake_engines(ddgs=lambda *a: list(RESULTS)):
            ws.search("gpu prices")
        self.assertEqual(ws.result(2).host, "reviews.example.org")
        ws.read_page("second", "price")
        ws.read.assert_called_with("https://reviews.example.org/5070")
        with self.assertRaises(SearchError):
            ws.result(9)

    def test_research_combines_several_pages(self):
        ws = self.searcher()
        with fake_engines(ddgs=lambda *a: list(RESULTS)):
            out = ws.research("rtx 5070 vs rtx 4070")
        self.assertIn("Research notes for: rtx 5070 vs rtx 4070", out)
        self.assertIn("[1]", out)
        self.assertIn("499 dollars", out)


class ToolTests(unittest.TestCase):
    def setUp(self):
        websearch.reset_shared()
        self.box = ToolBox(load_config(), {})
        self.ws = websearch.shared()
        self.ws.read = mock.Mock(return_value=webtext.extract(ARTICLE, "https://news.example.com/gpu"))

    def tearDown(self):
        websearch.reset_shared()

    def test_web_lookup_and_open_a_result(self):
        with fake_engines(ddgs=lambda *a: list(RESULTS)):
            out = self.box.run("web_lookup", {"query": "rtx 5070 price"})
        self.assertIn("499 dollars", out)
        with mock.patch("webbrowser.open") as browser:
            self.assertEqual(self.box.run("open_search_result", {"number": 3}),     # duplicate dropped
                             "Opened RTX 5070 review from reviews.example.org.")
        browser.assert_called_once_with("https://reviews.example.org/5070")

    def test_router_sends_open_the_second_one_to_the_results(self):
        self.assertIsNone(router.route("open the second one", self.box))     # no search yet: not an app called "second"
        with fake_engines(ddgs=lambda *a: list(RESULTS)):
            self.ws.search("gpu prices")
        self.assertEqual(router.route("open the second one", self.box), [("open_search_result", {"number": 2})])
        self.ws.last_time = time.time() - 3600                                 # an old search doesn't count
        self.assertIsNone(router.route("open the second one", self.box))

    def test_read_webpage_errors_are_spoken(self):
        from jarvis.tools.builtin import ToolFailed

        with self.assertRaises(ToolFailed) as e:
            self.box.run("read_webpage", {"page": "3"})
        self.assertIn("haven't searched", str(e.exception))


if __name__ == "__main__":
    unittest.main()
