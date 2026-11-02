"""Reading the web by voice, without a browser: read a page or a search result, dig deeper into a question across
several sources, and open a result from the last search in the browser.

web_lookup (builtin.py) does the quick search; these build on the results it keeps.
"""
from __future__ import annotations

import webbrowser

from ..webtext import WebError
from ..websearch import SearchError, shared
from .builtin import ToolFailed
from .registry import tool


def _web(ctx):
    return shared(((ctx.cfg.get("web") if ctx else None) or {}))


def _web_on(cfg) -> bool:
    return (cfg.get("web") or {}).get("enabled", True) and cfg.tools.get("web_lookup", True)


@tool("Read a web page and answer from it, without opening the browser: a result number from the last web search "
      "('read the second result'), a web address ('read bbc.com/news'), or a page name. Pass the user's question "
      "to get the parts of the page that answer it.",
      {"page": {"type": "string", "description": "Result number (1, 2, 3), web address, or what the page is"},
       "question": {"type": "string", "description": "What the user wants to know from the page, if anything"}},
      required=["page"], enabled=_web_on,
      triggers=("read", "result", "page", "article", "website", "site", "link", "says", "say", "what does",
                "summar", "open", "dot com", ".com", ".org", "http", "www"))
def read_webpage(page: str, question: str = "", ctx=None):
    try:
        return _web(ctx).read_page(page, question)
    except (WebError, SearchError) as e:
        raise ToolFailed(str(e)[:1].upper() + str(e)[1:] + ("" if str(e).endswith(".") else "."))


@tool("Research a question properly: search it from a couple of angles, read several pages and bring back the "
      "passages that answer it, with their sources. For questions that need more than one quick search: "
      "comparisons, how-tos, 'what's the best', 'find out everything about'. Slower than web_lookup.",
      {"question": {"type": "string", "description": "The question, in the user's words"}},
      enabled=_web_on,
      triggers=("research", "dig", "deep", "find out", "everything about", "compare", "comparison", "versus", " vs",
                "best", "pros and cons", "difference between", "how do i", "how to", "investigate", "in depth",
                "detail", "explain", "review", "reviews", "worth"))
def deep_research(question: str, ctx=None):
    notify = (ctx.context.get("speak") or ctx.context.get("notify")) if ctx else None
    if notify:
        try:
            notify("Researching that, it takes a few seconds.")
        except Exception:
            pass
    try:
        return _web(ctx).research(question, pages=int(((ctx.cfg.get("web") or {}) if ctx else {})
                                                      .get("research_pages", 4)))
    except SearchError as e:
        raise ToolFailed(f"I couldn't search the web: {e}.")


@tool("Open one of the results from the last web search in the browser ('open the second one', 'open that "
      "link', 'show me the first result').",
      {"number": {"type": "integer", "description": "Which result, 1 = first"}},
      enabled=_web_on, triggers=("open", "show", "go to", "take me", "pull up", "bring up", "visit", "click"))
def open_search_result(number: int = 1, ctx=None):
    try:
        r = _web(ctx).result(int(number or 1))
    except (SearchError, ValueError) as e:
        raise ToolFailed(f"I can't open that: {e}.")
    webbrowser.open(r.url)
    return f"Opened {r.title} from {r.host}."
