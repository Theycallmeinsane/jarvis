"""Forgiving name matching for things Whisper mishears: "insta gram", "red it", "you tube", "Ahmed" for "Ahmad",
"quen" for "Qwen", "chapter two" for "chapter 2".

score() compares a spoken name with a real one in several ways (spacing, a rough sound-alike key, per-word
matches, plain similarity) and keeps the best; best() picks the most likely candidate or None when nothing is
close enough. close() is a drop-in for difflib.get_close_matches(q, options, n=1, cutoff).
"""
from __future__ import annotations

import difflib
import re
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")

NUMBERS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
           "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
           "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
           "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
            "ninth": 9, "tenth": 10, "last": -1}
FILLER = {"the", "a", "an", "my", "of", "on", "in", "to", "app", "please"}


def number(text: str) -> int | None:
    """'7', 'seven', 'twenty one', 'number 12', 'the third' -> int (None if it isn't a number)."""
    t = re.sub(r"\b(number|no\.?|the|link|result|item|#)\b", " ", text.lower()).strip()
    if re.fullmatch(r"\d{1,4}", t):
        return int(t)
    if t in ORDINALS:
        return ORDINALS[t]
    words = t.replace("-", " ").split()
    if words and all(w in NUMBERS for w in words) and len(words) <= 2:
        total = sum(NUMBERS[w] for w in words)
        return total if len(words) == 1 or (NUMBERS[words[0]] >= 20 and NUMBERS[words[1]] < 10) else None
    return None


def normalize(text: str) -> str:
    """Lower case, no punctuation, number words as digits, spelled-out letters joined ('v s code' -> 'vs code')."""
    t = text.lower().replace("&", " and ").replace("'s ", "s ").replace("’", "'")
    t = re.sub(r"[^\w\s]", " ", t)
    words = [str(NUMBERS[w]) if w in NUMBERS and NUMBERS[w] < 20 else w for w in t.split()]
    out: list[str] = []
    spelling = False                        # inside a run of single letters
    for w in words:
        letter = len(w) == 1 and w.isalpha() and w not in ("a", "i")
        if letter and spelling:
            out[-1] += w
        else:
            out.append(w)
        spelling = letter
    return " ".join(out)


def squash(text: str) -> str:
    return normalize(text).replace(" ", "")


def sound(word: str) -> str:
    """A rough sound-alike key: Ahmed/Ahmad, Mohammed/Muhammad, Qwen/quen, Reddit/red it all collide."""
    w = re.sub(r"[^a-z0-9]", "", word.lower())
    if not w:
        return ""
    for a, b in (("ph", "f"), ("ck", "k"), ("kh", "k"), ("gh", "g"), ("th", "t"), ("sh", "s"), ("ch", "c"),
                 ("q", "k"), ("x", "ks"), ("z", "s"), ("v", "f"), ("w", ""), ("y", ""), ("h", "")):
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eiy])", "s", w).replace("c", "k")
    w = re.sub(r"g(?=[ei])", "j", w)                     # soft g: "jo gee" ~ "Joji"
    head, rest = w[:1], re.sub(r"[aeiou]", "", w[1:])
    key = re.sub(r"(.)\1+", r"\1", head + rest)
    return key


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _word_score(a: str, b: str) -> float:
    if a == b:
        return 1.0
    s = _ratio(a, b)
    if len(a) >= 3 and len(b) >= 3 and sound(a) == sound(b):
        s = max(s, 0.9)
    return s


def score(spoken: str, real: str) -> float:
    """0..1: how likely `real` is what was meant by `spoken`."""
    a, b = normalize(spoken), normalize(real)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sa, sb = a.replace(" ", ""), b.replace(" ", "")
    if sa == sb:
        return 0.98
    best = _ratio(sa, sb)
    if len(sa) >= 4 and sound(sa) == sound(sb):
        best = max(best, 0.92)
    aw = [w for w in a.split() if w not in FILLER] or a.split()
    bw = [w for w in b.split() if w not in FILLER] or b.split()
    if aw and bw:
        # every spoken word against its best real word (a shorter spoken name is fine: "ahmed" for "Ahmad Khan")
        per = [max(_word_score(x, y) for y in bw) for x in aw]
        words = sum(per) / len(per)
        if len(aw) < len(bw):
            words *= 0.97
        best = max(best, words)
        if f" {sa} " in f" {' '.join(bw)} " or (len(sa) >= 4 and sa in sb):
            best = max(best, 0.9)
    return round(best, 3)


def ranked(spoken: str, items: Iterable[T], key: Callable[[T], str] = str) -> list[tuple[float, T]]:
    return sorted(((score(spoken, key(i)), i) for i in items), key=lambda p: -p[0])


def best(spoken: str, items: Iterable[T], key: Callable[[T], str] = str, cutoff: float = 0.75) -> T | None:
    top = ranked(spoken, items, key)
    return top[0][1] if top and top[0][0] >= cutoff else None


def close(spoken: str, options: Iterable[str], cutoff: float = 0.75) -> list[str]:
    """Like difflib.get_close_matches(spoken, options, n=1, cutoff)."""
    hit = best(spoken, list(options), cutoff=cutoff)
    return [hit] if hit is not None else []


def fix_names(text: str, names: Iterable[str]) -> str:
    """Swap a misheard name in a sentence for the known spelling ('play glimpse of us by jo gee' with 'Joji'
    known). Only near-certain matches, and never ordinary words, are replaced."""
    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        return text
    words = text.split()
    out, i = [], 0
    while i < len(words):
        done = False
        for size in (3, 2, 1):
            chunk = " ".join(words[i:i + size])
            if len(words[i:i + size]) < size or len(squash(chunk)) < 3:
                continue
            hit = max(names, key=lambda n: score(chunk, n))
            s = score(chunk, hit)
            if s >= 0.9 and squash(chunk) != squash(hit) and (size > 1 or sound(chunk) == sound(hit)) \
                    and abs(len(squash(chunk)) - len(squash(hit))) <= 3:
                out.append(hit)
                i += size
                done = True
                break
        if not done:
            out.append(words[i])
            i += 1
    return " ".join(out)
