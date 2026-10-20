"""Loose word matching for spoken requests: word endings ("timers" ~ "timer", "parked" ~ "park") and the slips
speech-to-text makes ("whether" for weather, "valume", "spotfy", "tymer", "minits", "favorite color").

Used to pick the tools a request needs (tool_select) and the remembered facts that matter to it (memory).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from functools import lru_cache

# small words that say nothing about what the request is for
STOP = frozenset("""
a about actually after again all also am an and any anything are as at awesome be been before being bit but by
bye can cant cool could did didnt do does dont for from get give go going gonna good got great had has have he
hello her here hers hey hi him his how i if ill im in into is isnt it its ive jarvis just kind know let like
little lot make maybe me might mine more most much my need nice no not now of off oh ok okay on one or our out
over please really right same say set she should so some something tell than thank thanks that the their them
then there these they thing things this those to too up us use very wanna want was we well were what whats
when where which who why will with would yeah yes yet you your youre yours
""".split())

KEEP = frozenset({"news", "series", "status", "this", "is", "was", "has", "does", "yes", "plus", "bus", "gas",
                  "previous", "always", "sometimes"})


def tokens(text: str) -> list[str]:
    """Lower-case words, apostrophes dropped ("what's" -> "what", "can't" -> "cant"); a dotted name stays one
    word ("github.com", "notes.txt")."""
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"'s\b", "", t).replace("'", "")
    return re.findall(r"[a-z0-9]+(?:\.[a-z0-9]+)*", t)


@lru_cache(maxsize=8192)
def stem(w: str) -> str:
    """A light stemmer, the same for both sides of a comparison: files/file -> fil, parked/park -> park,
    movies/movie -> movi, batteries/battery -> batteri, running/run -> run."""
    if len(w) <= 3 or w in KEEP:
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-2]
    elif w.endswith(("sses", "shes", "ches", "xes", "zes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    if w.endswith("ied") and len(w) > 4:
        w = w[:-2]
    elif w.endswith("ing") and len(w) > 5:
        w = _undouble(w[:-3])
    elif w.endswith("ed") and len(w) > 4:
        w = _undouble(w[:-2])
    if len(w) > 3 and w[-1] == "y" and w[-2] not in "aeiou":
        w = w[:-1] + "i"
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    return w


def _undouble(w: str) -> str:
    return w[:-1] if len(w) > 2 and w[-1] == w[-2] and w[-1] in "bdgmnprt" else w


_CODE = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"), **dict.fromkeys("dt", "3"), "l": "4",
         **dict.fromkeys("mn", "5"), "r": "6"}


@lru_cache(maxsize=8192)
def sound(w: str) -> str:
    """A Soundex-like key: words that sound alike share it ("whether"/"weather", "valume"/"volume")."""
    w = re.sub(r"[^a-z]", "", w.lower())
    w = re.sub(r"^wh", "w", w).replace("ph", "f").replace("ck", "k")
    if not w:
        return ""
    out, prev = [w[0]], _CODE.get(w[0], "")
    for ch in w[1:]:
        code = _CODE.get(ch, "")
        if code and code != prev:
            out.append(code)
        if ch not in "hw":
            prev = code
    return "".join(out)


def likeness(a: str, b: str) -> float:
    """0..1: how sure we are that b is what was meant by a (1 = same word)."""
    if a == b:
        return 1.0
    if min(len(a), len(b)) < 4 or abs(len(a) - len(b)) > 2 or not a.isalpha() or not b.isalpha():
        return 0.0
    m = SequenceMatcher(None, a, b)
    if m.real_quick_ratio() < 0.7 or m.quick_ratio() < 0.7:
        return 0.0
    r = m.ratio()
    if r >= 0.85 and min(len(a), len(b)) >= 5:
        return r
    if r >= 0.75 and sound(a) == sound(b):
        return r
    return 0.0


def alike(a: str, b: str) -> bool:
    """The same word, give or take an ending or a speech-to-text slip."""
    return a == b or stem(a) == stem(b) or likeness(a, b) > 0


def content_words(text: str) -> list[str]:
    """The words of a request that carry meaning (no 'the', 'please', 'what')."""
    return [w for w in tokens(text) if w not in STOP and not w.isdigit()]
