"""Searching the web for answers (not just opening a results page).

A spoken request ("hey jarvis can you google who won the match last night") is cleaned into a search query, sent
to the first search engine that works, and the best results are then read: the passages on those pages that answer
the question go back to the brain with their sources, so the answer comes from the pages and not from a two-line
snippet. Results are kept, so "open the second one" or "read the third result" work afterwards.

Engines, in the order of web.engines (each one is skipped until it's set up):
  searxng  your own SearXNG instance (web.searxng_url): Google, Bing and others combined, private, no key
  brave    Brave Search API (BRAVE_API_KEY in .env)
  google   Google Programmable Search (GOOGLE_API_KEY + GOOGLE_CSE_ID in .env)
  tavily   Tavily, made for AI agents (TAVILY_API_KEY in .env)
  ddgs     DuckDuckGo and friends through the ddgs package (no key, the default)
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from . import webtext

log = logging.getLogger(__name__)

DEFAULT_ENGINES = ["searxng", "brave", "google", "tavily", "ddgs"]

# what people say around a search that isn't part of it
FILLER = re.compile(
    r"^(?:hey |ok |okay )?(?:jarvis[, ]*)?(?:please |can you |could you |would you |will you |i want you to |"
    r"i need you to |go ahead and |quickly |just )*"
    r"(?:search (?:the web |the internet |online |google )?(?:for |about )?|"
    r"google |look up |lookup |find out |find (?:me )?|check (?:online |the web )?|search for |research |"
    r"tell me |do a search (?:for |on )?)?", re.I)
TAIL = re.compile(r"\s*(?:on|from|in) (?:the )?(?:web|internet|google|online)\b|\s*(?:for me|please|online|"
                  r"right now|real quick|thanks|thank you)\s*$", re.I)
FRESH = re.compile(r"\b(latest|today|tonight|yesterday|this (?:week|morning|evening)|right now|currently|"
                   r"current|breaking|news|headlines?|score|scores|won|winner|live|price|prices|stock|"
                   r"weather|update|updates|trending|recent|just announced|release date)\b", re.I)
NEWSY = re.compile(r"\b(news|headlines?|breaking|announced|happening)\b", re.I)


@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""
    date: str = ""
    source: str = ""

    @property
    def host(self) -> str:
        return (urllib.parse.urlsplit(self.url).hostname or "").removeprefix("www.")


def clean_query(spoken: str) -> str:
    """'hey jarvis can you google the best budget gpu for me' -> 'the best budget gpu'."""
    q = FILLER.sub("", spoken.strip().rstrip("?.! ")).strip()
    while True:
        shorter = TAIL.sub("", q).strip(" ,")
        if shorter == q:
            break
        q = shorter
    return q or spoken.strip()


def timelimit(query: str) -> str | None:
    """d / w for questions about what's happening now; None for timeless ones."""
    if re.search(r"\b(today|tonight|yesterday|right now|breaking|live|this morning|this evening)\b", query, re.I):
        return "d"
    if FRESH.search(query):
        return "w"
    return None


# ---------------------------------------------------------------- engines

def _searxng(cfg, q, n, news, fresh):
    import requests

    base = (cfg.get("searxng_url") or "").rstrip("/")
    if not base:
        return None
    params = {"q": q, "format": "json", "categories": "news" if news else "general"}
    if fresh:
        params["time_range"] = {"d": "day", "w": "week"}[fresh]
    r = requests.get(f"{base}/search", params=params, timeout=10)
    r.raise_for_status()
    return [Result(x.get("title", ""), x.get("url", ""), x.get("content", ""), x.get("publishedDate") or "",
                   ",".join(x.get("engines") or [])) for x in r.json().get("results", [])[:n]]


def _brave(cfg, q, n, news, fresh):
    import requests

    key = os.environ.get(cfg.get("brave_key_env", "BRAVE_API_KEY") or "")
    if not key:
        return None
    kind = "news" if news else "web"
    params = {"q": q, "count": n}
    if fresh:
        params["freshness"] = {"d": "pd", "w": "pw"}[fresh]
    r = requests.get(f"https://api.search.brave.com/res/v1/{kind}/search", params=params, timeout=10,
                     headers={"X-Subscription-Token": key, "Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    rows = data.get("results") if news else (data.get("web") or {}).get("results", [])
    return [Result(x.get("title", ""), x.get("url", ""), re.sub(r"<[^>]+>", "", x.get("description", "")),
                   x.get("age") or "", "brave") for x in (rows or [])[:n]]


def _google(cfg, q, n, news, fresh):
    import requests

    key = os.environ.get(cfg.get("google_key_env", "GOOGLE_API_KEY") or "")
    cx = os.environ.get(cfg.get("google_cx_env", "GOOGLE_CSE_ID") or "")
    if not key or not cx:
        return None
    params = {"key": key, "cx": cx, "q": q, "num": min(n, 10)}
    if fresh:
        params["dateRestrict"] = {"d": "d1", "w": "w1"}[fresh]
    r = requests.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=10)
    r.raise_for_status()
    return [Result(x.get("title", ""), x.get("link", ""), x.get("snippet", ""), "", "google")
            for x in r.json().get("items", [])[:n]]


def _tavily(cfg, q, n, news, fresh):
    import requests

    key = os.environ.get(cfg.get("tavily_key_env", "TAVILY_API_KEY") or "")
    if not key:
        return None
    body = {"query": q, "max_results": n, "topic": "news" if news else "general"}
    if fresh:
        body["time_range"] = {"d": "day", "w": "week"}[fresh]
    r = requests.post("https://api.tavily.com/search", json=body, timeout=15,
                      headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    return [Result(x.get("title", ""), x.get("url", ""), x.get("content", ""), x.get("published_date") or "",
                   "tavily") for x in r.json().get("results", [])[:n]]


def _ddgs(cfg, q, n, news, fresh):
    from ddgs import DDGS

    with DDGS() as d:
        if news:
            rows = d.news(q, max_results=n, timelimit=fresh)
            return [Result(x.get("title", ""), x.get("url", ""), x.get("body", ""), (x.get("date") or "")[:10],
                           x.get("source", "")) for x in rows]
        rows = d.text(q, max_results=n, timelimit=fresh)
        return [Result(x.get("title", ""), x.get("href", ""), x.get("body", ""), "", "ddgs") for x in rows]


ENGINES = {"searxng": _searxng, "brave": _brave, "google": _google, "tavily": _tavily, "ddgs": _ddgs}


class SearchError(Exception):
    pass


# ---------------------------------------------------------------- the searcher

class WebSearch:
    """Search + read, with a short cache and the last results kept for follow-ups."""

    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})
        self.engines = [e for e in (self.cfg.get("engines") or DEFAULT_ENGINES) if e in ENGINES]
        self.read_top = int(self.cfg.get("read_top", 2))           # pages read for each lookup
        self.max_chars = int(self.cfg.get("max_chars", 3500))      # what goes back to the brain
        self.cache_s = float(self.cfg.get("cache_minutes", 10)) * 60
        self.page_timeout = float(self.cfg.get("page_timeout", 8))
        self.allow_private = bool(self.cfg.get("allow_private_addresses", False))
        self.blocked = {d.lower().removeprefix("www.") for d in self.cfg.get("blocked_sites") or []}
        self.last: list[Result] = []
        self.last_query = ""
        self.last_time = 0.0
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- search
    def _ask_engines(self, q: str, n: int, news: bool, fresh: str | None) -> tuple[list[Result], list[str]]:
        """The first engine that is set up and finds something wins; the rest are fallbacks."""
        errors = []
        for name in self.engines:
            try:
                got = ENGINES[name](self.cfg, q, n + 2, news, fresh)
            except Exception as e:               # engine down, quota used up, no network
                errors.append(f"{name}: {e.__class__.__name__}")
                log.info("search engine %s failed: %s", name, e)
                continue
            if got:                              # None = not set up, [] = nothing found: try the next one
                return got, errors
        return [], errors

    def search(self, query: str, n: int = 6, news: bool | None = None) -> list[Result]:
        q = clean_query(query)
        news = bool(NEWSY.search(q)) if news is None else news
        fresh = timelimit(query)
        key = ("s", q.lower(), n, news, fresh)
        results = self._cached(key)
        if results is None:
            results, errors = self._ask_engines(q, n, news, fresh)
            if not results and fresh:            # nothing this week: try without the time limit
                results, more = self._ask_engines(q, n, news, None)
                errors += more
            if not results and errors:
                raise SearchError("the search engines didn't answer (" + ", ".join(dict.fromkeys(errors)) + ")")
            results = self._tidy(results)[:n]
            if results:
                self._remember(key, results)
        with self._lock:
            self.last, self.last_query, self.last_time = list(results), q, time.time()
        return results

    def _tidy(self, results: list[Result]) -> list[Result]:
        """No duplicates (same page with tracking bits), nothing from blocked sites, no empty rows."""
        seen, out = set(), []
        for r in results:
            if not r.url or not r.title:
                continue
            key = re.sub(r"[?#].*$", "", r.url.lower()).rstrip("/").removeprefix("https://").removeprefix("http://")
            if key in seen or r.host in self.blocked:
                continue
            seen.add(key)
            r.title = re.sub(r"\s+", " ", r.title).strip()
            r.snippet = re.sub(r"\s+", " ", r.snippet or "").strip()
            out.append(r)
        return out

    def _cached(self, key):
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_s:
            return hit[1]
        return None

    def _remember(self, key, value):
        with self._lock:
            self._cache[key] = (time.time(), value)
            if len(self._cache) > 200:
                oldest = sorted(self._cache, key=lambda k: self._cache[k][0])[:50]
                for k in oldest:
                    self._cache.pop(k, None)

    # -- read
    def read(self, url: str) -> webtext.Page:
        key = ("p", url)
        hit = self._cached(key)
        if hit is not None:
            return hit
        page = webtext.fetch(url, timeout=self.page_timeout, allow_private=self.allow_private)
        self._remember(key, page)
        return page

    def result(self, number: int) -> Result:
        with self._lock:
            last = list(self.last)
        if not last:
            raise SearchError("I haven't searched for anything yet")
        if not 1 <= number <= len(last):
            raise SearchError(f"there were only {len(last)} results")
        return last[number - 1]

    def _read_many(self, results: list[Result], question: str, per_page: int) -> list[tuple[Result, list[str]]]:
        found: dict[str, list[str]] = {}
        if not results:
            return []
        pool = ThreadPoolExecutor(max_workers=min(4, len(results)))
        jobs = {pool.submit(self.read, r.url): r for r in results}
        try:
            for job in as_completed(jobs, timeout=self.page_timeout + 4):
                r = jobs[job]
                try:
                    found[r.url] = webtext.passages(job.result().blocks, question, k=per_page)
                except Exception as e:
                    log.info("couldn't read %s: %s", r.url, e)
        except TimeoutError:                     # slow sites: go with the pages that came back
            log.info("stopped waiting for %d slow pages", len(jobs) - len(found))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return [(r, found[r.url]) for r in results if found.get(r.url)]

    # -- what the tools return
    def lookup(self, query: str, news: bool | None = None, read: int | None = None) -> str:
        """Numbered results with snippets, then the passages from the top pages that answer the question."""
        results = self.search(query, n=6, news=news)
        if not results:
            return f"Nothing found for {clean_query(query)}."
        q = clean_query(query)
        lines = [f"Search results for: {q}"]
        for i, r in enumerate(results, 1):
            date = f" ({r.date})" if r.date else ""
            lines.append(f"{i}. {r.title} [{r.host}]{date}: {r.snippet[:220]}")
        read = self.read_top if read is None else read
        if read > 0:
            pages = self._read_many(results[:read + 1], q, per_page=2)[:read]
            if pages:
                lines.append("\n" + webtext.UNTRUSTED)
                for r, found in pages:
                    lines.append(f"From {r.host}: " + " ... ".join(found))
        lines.append("\nAnswer from these results and name the source site. The user can say 'open the second "
                     "one' or 'read the first result'.")
        return _cap("\n".join(lines), self.max_chars)

    def read_page(self, target: str, question: str = "") -> str:
        """A result number ("2"), a URL or a site name, read as text."""
        target = (target or "").strip()
        n = _number(target)
        if n:
            target = self.result(n).url
        elif not re.search(r"\.[a-z]{2,}|\bdot\b|^https?://", target, re.I):
            # a name rather than an address ("the bbc article"): the best search result for it
            results = self.search(target, n=3)
            if not results:
                raise SearchError(f"I couldn't find a page for {target}")
            target = results[0].url
        page = self.read(target)
        return webtext.summary_text(page, question, max_chars=self.max_chars)

    def research(self, question: str, pages: int = 4) -> str:
        """A deeper look: a few searches from different angles, several pages read, the best passages from each."""
        q = clean_query(question)
        variants = [q]
        if timelimit(question):
            variants.append(q + " news")
        elif not re.search(r"\b(vs|versus|compare|best|review)\b", q, re.I):
            variants.append(q + " explained")
        seen, pool = set(), []
        for v in variants:
            try:
                for r in self.search(v, n=6, news=False if v == q else None):
                    if r.url not in seen:
                        seen.add(r.url)
                        pool.append(r)
            except SearchError:
                continue
        if not pool:
            return f"I couldn't find anything about {q}."
        with self._lock:
            self.last, self.last_query, self.last_time = pool[:8], q, time.time()
        read = self._read_many(pool[:pages + 2], q, per_page=3)[:pages]
        lines = [f"Research notes for: {q}", webtext.UNTRUSTED]
        for i, (r, found) in enumerate(read, 1):
            lines.append(f"[{i}] {r.title} ({r.host})\n" + "\n".join(f"- {p}" for p in found))
        if not read:
            lines += [f"[{i}] {r.title} ({r.host}): {r.snippet}" for i, r in enumerate(pool[:6], 1)]
        lines.append("\nGive a short spoken answer that combines these sources, says where they disagree, and "
                     "names the sites.")
        return _cap("\n".join(lines), int(self.max_chars * 1.6))


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


ORDINALS = {"first": 1, "one": 1, "second": 2, "two": 2, "third": 3, "three": 3, "fourth": 4, "four": 4,
            "fifth": 5, "five": 5, "sixth": 6, "six": 6, "last": -1, "top": 1}


def _number(target: str) -> int | None:
    t = target.lower().strip()
    if re.fullmatch(r"#?\d{1,2}", t):
        return int(t.lstrip("#"))
    m = re.fullmatch(r"(?:the )?(\w+)(?: one| result| link| page| article)?", t)
    if m and m.group(1) in ORDINALS and ORDINALS[m.group(1)] > 0:
        return ORDINALS[m.group(1)]
    return None


_shared: WebSearch | None = None


def shared(cfg=None) -> WebSearch:
    """One searcher per process, so follow-ups ("open the second one") see the last results."""
    global _shared
    if _shared is None:
        _shared = WebSearch(cfg)
    return _shared


def reset_shared():
    global _shared
    _shared = None
