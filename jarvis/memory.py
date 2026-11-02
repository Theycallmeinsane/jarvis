"""Persistent memory that survives restarts.

* facts   (data/memory.json)    - things the user asked Jarvis to remember (or plainly told it about themselves,
                                  "I live in Lahore"); the ones a request is about are attached to it as a short
                                  note for the brain. A new fact about the same thing replaces the old one
                                  ("favourite colour is green" replaces "...is blue"), "tomorrow" is stored as
                                  the real date, and a dated fact is dropped once that day has passed.
* history (data/history.jsonl)  - every exchange (user text + spoken reply); the last few are replayed into a fresh
                                  conversation so Jarvis picks up where it left off, and "what did we talk about
                                  yesterday?" searches it (recall_conversation)
* vectors (data/memory_vectors.json, optional) - with memory.semantic on, facts are also matched by meaning through
                                  a local Ollama embedding model, for requests that share no words with the fact
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import threading
import time
from pathlib import Path

from .config import Section, local_url, resolve_path
from .wordmatch import alike, content_words, tokens

log = logging.getLogger(__name__)

# "what do you know about me", "what have I told you": every fact, not just the matching ones
ASK_ALL = re.compile(r"\b(know|remember|remembered|saved|stored|kept|have|got) (on|about|of) (me|myself)\b|"
                     r"\bwhat have i told you\b|\btell me about (me|myself)\b|\byour memor(y|ies)\b|"
                     r"\b(do|did) you remember (anything|everything)\b|\bwhat (do|did|have) you (remember|remembered)$")
# words too common in facts to tie a request to one ("favourite", "name", "called")
FACT_STOP = {"favourite", "favorite", "like", "likes", "love", "loves", "name", "named", "called", "call",
             "remember", "forget", "know", "told", "really", "today", "always", "usually"}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# what the subject of a fact is ("favourite colour" in "favourite colour is blue"), so a newer fact about the same
# thing replaces the older one instead of both being kept
COPULA = re.compile(r"^(?P<subj>.+?)\s+(?:is|are|was|were|=)\s+(?P<val>.+)$", re.I)
SUBJECT_SKIP = {"my", "the", "a", "an", "our", "his", "her", "their", "user", "users"}
SUBJECT_VERB_ALIASES = {"called": "name", "named": "name", "name": "name"}

# plain statements about the user that are worth keeping without "remember": (pattern, how the fact is written)
I_AM = r"\bi(?: am|'m|’m|m)"
NOTICE = [
    (re.compile(r"\bi (?:live|(?:am|'m|m) living) in (?P<v>[\w' -]{2,40})", re.I), "lives in {v}"),
    (re.compile(r"\bi (?:just )?moved to (?P<v>[\w' -]{2,40})", re.I), "lives in {v}"),
    (re.compile(r"\bmy birthday is (?:on )?(?P<v>[\w' -]{3,30})", re.I), "birthday is {v}"),
    (re.compile(I_AM + r" allergic to (?P<v>[\w' -]{2,40})", re.I), "allergic to {v}"),
    (re.compile(r"\bi (?:work|(?:am|'m|m) working) (?P<p>at|for|as) (?P<v>[\w' &-]{2,40})", re.I), "works {p} {v}"),
    (re.compile(I_AM + r" (?:a )?(?P<v>vegetarian|vegan|pescatarian|left handed|left-handed)\b", re.I), "is {v}"),
]
# a statement inside one of these is something else: a request to type, search, send or note something
NOT_ABOUT_ME = re.compile(r"^\W*(?:hey |ok |okay )?(?:jarvis[, ]*)?(?:please )?(?:type|dictate|write|note|jot|search|"
                          r"google|look up|email|send|text|message|reply|translate|say|tell|draft|post|tweet|"
                          r"what if|pretend|imagine)\b", re.I)
# ...and one that follows these is not a fact: "if I lived in Paris", "my friend said I work at Google"
HYPOTHETICAL = re.compile(r"\b(if|when|would|could|wish|used to|suppose|imagine|pretend|said|says|thinks)\b", re.I)
VALUE_END = re.compile(r"\s*(?:[,.;!?]|\band\b|\bbut\b|\bso\b|\bbecause\b|\bwhat\b|\bcan\b|\bplease\b).*$", re.I)

# when a history search is about a time, not a topic
HISTORY_TIME_WORDS = {"yesterday", "today", "earlier", "morning", "afternoon", "evening", "tonight", "week",
                      "last", "ago", "recently", "before", "time", "talk", "talked", "chat", "chatted", "said",
                      "say", "ask", "asked", "tell", "told", "conversation", "discuss", "discussed", "mention",
                      "mentioned", "about", "we", "did", "you", "what", "was", "that", "thing", "the"}


def _day(d: dt.date) -> str:
    return f"{d:%A} {d.day} {d:%B}"


def absolute_dates(fact: str, today: dt.date | None = None) -> tuple[str, dt.date | None]:
    """"dentist tomorrow" -> ("dentist on Tuesday 3 November", 3 November): a remembered "tomorrow" would be wrong
    the next day. Returns the fact and the day it is about (None when it names no upcoming day)."""
    today = today or dt.date.today()
    day = None

    def at(d: dt.date, words: str = "on") -> str:
        nonlocal day
        day = d if day is None or d > day else day
        return f"{words} {_day(d)}"

    out = re.sub(r"\b(?:on )?tonight\b", lambda m: at(today, "on the evening of"), fact, flags=re.I)
    out = re.sub(r"\b(?:on )?today\b", lambda m: at(today), out, flags=re.I)
    out = re.sub(r"\b(?:on )?tomorrow\b", lambda m: at(today + dt.timedelta(days=1)), out, flags=re.I)
    out = re.sub(r"\b(?:on )?yesterday\b", lambda m: f"on {_day(today - dt.timedelta(days=1))}", out, flags=re.I)

    def weekday(m):
        target = WEEKDAYS.index(m.group(2).lower())
        ahead = (target - today.weekday()) % 7
        if m.group(1).lower() == "next" and ahead == 0 or ahead == 0 and m.group(1).lower() != "this":
            ahead = 7
        return at(today + dt.timedelta(days=ahead))

    out = re.sub(r"\b(on|this|next) (" + "|".join(WEEKDAYS) + r")\b(?! \d)", weekday, out, flags=re.I)
    return out, day


def subject_of(fact: str) -> list[str]:
    """The words a fact is about ("favourite colour is blue" -> favourite, colour; "sister is called Sara" ->
    sister, name). Empty when the fact isn't an "X is Y" statement."""
    m = COPULA.match(fact.strip())
    if not m:
        return []
    words = [w for w in tokens(m.group("subj")) if w not in SUBJECT_SKIP]
    if not words or len(words) > 5:
        return []
    first = (tokens(m.group("val")) or [""])[0]
    if first in SUBJECT_VERB_ALIASES:
        words.append(SUBJECT_VERB_ALIASES[first])
    elif first.endswith("ed") and len(first) > 4:      # "car is parked ..." is not "car is red"
        words.append(first)
    return words


def same_subject(a: list[str], b: list[str]) -> bool:
    return bool(a) and len(a) == len(b) and all(alike(x, y) for x, y in zip(a, b))


class Memory:
    def __init__(self, cfg: Section):
        m = cfg.get("memory") or {}
        self.enabled = m.get("enabled", True)
        self.facts_path: Path = resolve_path(m.get("facts_file", "data/memory.json"))
        self.history_path: Path = resolve_path(m.get("history_file", "data/history.jsonl"))
        self.restore_turns = int(m.get("restore_turns", 2))
        self.restore_minutes = float(m.get("restore_within_minutes", 30))
        self.max_facts = int(m.get("max_facts", 100))
        self.relevant_facts_max = int(m.get("facts_per_request", 5))     # facts attached to one request
        self.all_facts = int(m.get("facts_when_asked", 20))              # ...when asked "what do you know about me"
        self.auto_remember = bool(m.get("auto_remember", True))          # "I live in Lahore" is kept
        self.dated_keep_days = int(m.get("keep_dated_facts_days", 1))    # "dentist tomorrow" goes after that day
        self.history_max_bytes = int(float(m.get("history_max_mb", 20)) * 1024 * 1024)
        # meaning-based matching through a local embedding model (optional; needs `ollama pull nomic-embed-text`)
        self.semantic = bool(m.get("semantic", False))
        self.embed_model = m.get("embed_model", "nomic-embed-text")
        self.semantic_threshold = float(m.get("semantic_threshold", 0.6))
        ollama = ((cfg.get("llm") or {}).get("providers") or {}).get("ollama") or {}
        self.embed_url = local_url(m.get("embed_url") or ollama.get("base_url") or "http://127.0.0.1:11434")
        self.vectors_path: Path = resolve_path(m.get("vectors_file", "data/memory_vectors.json"))
        self._vectors: dict[str, list[float]] | None = None
        self._lock = threading.Lock()
        self.facts: list[dict] = self._load_facts()
        self._drop_expired()

    # ------------------------------------------------------------ facts

    def _load_facts(self) -> list[dict]:
        try:
            data = json.loads(self.facts_path.read_text(encoding="utf-8"))
            return [f for f in data if isinstance(f, dict) and f.get("fact")]
        except (OSError, ValueError):
            return []

    def _save_facts(self):
        self.facts_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.facts_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.facts, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.facts_path)

    def _drop_expired(self):
        today = dt.date.today().isoformat()
        with self._lock:
            keep = [f for f in self.facts if not f.get("expires") or f["expires"] >= today]
            if len(keep) != len(self.facts):
                log.info("memory: dropped %d facts whose day has passed", len(self.facts) - len(keep))
                self.facts = keep
                self._save_facts()

    def remember(self, fact: str, source: str = "asked") -> str:
        fact = fact.strip().rstrip(".")
        if not fact:
            return "There was nothing to remember."
        fact, day = absolute_dates(fact)
        subject = subject_of(fact)
        with self._lock:
            if any(f["fact"].lower() == fact.lower() for f in self.facts):
                return "I already know that."
            replaced = [f for f in self.facts if same_subject(subject, subject_of(f["fact"]))]
            self.facts = [f for f in self.facts if f not in replaced]
            row = {"fact": fact, "added": time.strftime("%Y-%m-%d")}
            if source != "asked":
                row["source"] = source
            if day:
                row["expires"] = (day + dt.timedelta(days=self.dated_keep_days)).isoformat()
            self.facts.append(row)
            self._evict()
            self._save_facts()
        if replaced:
            return f"Updated: I'll remember that {fact}, instead of {replaced[-1]['fact']}."
        return f"I'll remember that {fact}."

    def _evict(self):
        """Over the limit: drop the facts that have been least useful (never brought up, oldest), not just the
        oldest ones; the newest fact always stays."""
        while len(self.facts) > self.max_facts:
            candidates = self.facts[:-1]
            worst = min(candidates, key=lambda f: (f.get("uses", 0) > 0, f.get("last_used") or f.get("added", ""),
                                                   f.get("added", "")))
            self.facts.remove(worst)

    def notice(self, said: str) -> str | None:
        """Keep a plain statement the user made about themselves ("I live in Lahore", "I'm allergic to cats")
        without them having to say "remember". Returns the fact kept, or None."""
        if not self.auto_remember or not said or "?" in said or NOT_ABOUT_ME.search(said):
            return None
        for pattern, template in NOTICE:
            m = pattern.search(said)
            if not m or HYPOTHETICAL.search(said[:m.start()]):
                continue
            groups = {k: VALUE_END.sub("", v).strip(" '-") for k, v in m.groupdict().items()}
            value = groups.get("v", "")
            if not value or len(value.split()) > 5:
                continue
            fact = template.format(**groups)
            reply = self.remember(fact, source="noticed")
            if reply.startswith(("I'll remember", "Updated")):
                log.info("memory: noticed %r", fact)
                return fact
        return None

    def forget(self, query: str) -> str:
        """Drop facts that contain the query, or all of its words ("my favourite colour" drops "favourite colour
        is blue")."""
        q = query.lower().strip()
        words = [w for w in re.findall(r"[\w']+", q) if w not in ("my", "the", "a", "an", "about", "that", "is", "me")]

        def matches(fact: str) -> bool:
            f = fact.lower()
            return q in f or bool(words) and all(re.search(rf"\b{re.escape(w)}", f) for w in words)

        with self._lock:
            keep = [f for f in self.facts if not matches(f["fact"])]
            removed = len(self.facts) - len(keep)
            if removed:
                self.facts = keep
                self._save_facts()
        return f"Forgotten {removed} item{'s' * (removed != 1)}." if removed else f"I had nothing about {query}."

    # Facts used to sit in the system prompt, all of them, for every request: a small model then mentioned them
    # out of the blue, and every new fact changed the prompt (so Ollama couldn't reuse its cached start). Now the
    # system prompt only carries this fixed rule, and each request gets just the facts that are about it.
    RULE = ("\n\nA user message may end with a [Memory: ...] note: things the user asked you to remember in earlier "
            "sessions. Use one only when it matters to what they ask now; never bring them up otherwise, and don't "
            "claim to know anything else about them.")

    def facts_rule(self) -> str:
        return self.RULE

    def relevant_facts(self, request: str) -> list[str]:
        """The remembered facts this request is about ("where did I park" -> "car is parked on level 2"), best
        first; all of them (newest first, capped) when the user asks what Jarvis knows about them."""
        with self._lock:
            facts = [f["fact"] for f in self.facts]
        if not facts:
            return []
        if ASK_ALL.search(" ".join(tokens(request))):
            return facts[::-1][:self.all_facts]
        said = [w for w in content_words(request) if len(w) >= 3 and w not in FACT_STOP]
        if not said:
            return []
        # a word most facts share ("is", "car" with ten car facts) says less than one only this fact has
        fact_words = [[w for w in content_words(f) if len(w) >= 3 and w not in FACT_STOP] for f in facts]
        scored = []
        for age, (fact, words) in enumerate(zip(reversed(facts), reversed(fact_words))):     # age 0 = newest
            matched = [s for s in said if any(alike(s, w) for w in words)]
            if matched:
                weight = sum(1 / math.sqrt(sum(1 for fw in fact_words if any(alike(s, w) for w in fw)))
                             for s in matched)
                scored.append((-len(matched), -weight, age, fact))
        found = [fact for *_, fact in sorted(scored)[:self.relevant_facts_max]]
        if not found and self.semantic:
            found = self._semantic_facts(request, facts)
        self._mark_used(found)
        return found

    def _mark_used(self, facts: list[str]):
        if not facts:
            return
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            for f in self.facts:
                if f["fact"] in facts:
                    f["uses"] = f.get("uses", 0) + 1
                    f["last_used"] = today
            try:
                self._save_facts()
            except OSError:
                pass

    def facts_note(self, request: str) -> str:
        """A short note to append to the user's message for the brain (never logged as what the user said)."""
        facts = self.relevant_facts(request)
        return f"\n\n[Memory: {'; '.join(facts)}]" if facts else ""

    # ------------------------------------------------------------ meaning-based matching (optional)

    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        import requests

        try:
            r = requests.post(f"{self.embed_url}/api/embed", json={"model": self.embed_model, "input": texts},
                              timeout=15)
            r.raise_for_status()
            return r.json().get("embeddings")
        except Exception as e:      # model not pulled, Ollama not running: words-only from now on
            log.warning("memory: semantic matching off (%s); run `ollama pull %s` to use it", e, self.embed_model)
            self.semantic = False
            return None

    def _semantic_facts(self, request: str, facts: list[str]) -> list[str]:
        if self._vectors is None:
            try:
                self._vectors = json.loads(self.vectors_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._vectors = {}
        missing = [f for f in facts if f not in self._vectors]
        if missing:
            vecs = self._embed(missing)
            if not vecs:
                return []
            self._vectors.update(zip(missing, vecs))
            self._vectors = {f: v for f, v in self._vectors.items() if f in facts}
            try:
                self.vectors_path.parent.mkdir(parents=True, exist_ok=True)
                self.vectors_path.write_text(json.dumps(self._vectors), encoding="utf-8")
            except OSError:
                pass
        q = self._embed([request])
        if not q:
            return []
        scored = sorted(((cosine(q[0], self._vectors[f]), f) for f in facts if f in self._vectors), reverse=True)
        return [f for s, f in scored if s >= self.semantic_threshold][:min(3, self.relevant_facts_max)]

    # ------------------------------------------------------------ history

    def log_exchange(self, user: str, assistant: str, brain: str):
        if not self.enabled:
            return
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"t": time.time(), "user": user, "assistant": assistant, "brain": brain}
        with self._lock:
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._compact_history()

    def _compact_history(self):
        """Keep the log from growing forever: past history_max_mb, the older half goes."""
        try:
            if self.history_max_bytes <= 0 or self.history_path.stat().st_size <= self.history_max_bytes:
                return
            lines = self.history_path.read_text(encoding="utf-8").splitlines(keepends=True)
            tmp = self.history_path.with_suffix(".tmp")
            tmp.write_text("".join(lines[len(lines) // 2:]), encoding="utf-8")
            tmp.replace(self.history_path)
        except OSError:
            log.warning("couldn't trim %s", self.history_path)

    def _history_rows(self) -> list[dict]:
        if not self.history_path.exists():
            return []
        rows = []
        try:
            with open(self.history_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("user") and row.get("assistant"):
                        rows.append(row)
        except OSError:
            log.warning("couldn't read %s", self.history_path)
        return rows

    def recent_exchanges(self) -> list[dict]:
        """The last few exchanges, if they're recent enough to still be relevant."""
        if not self.enabled or self.restore_turns <= 0 or not self.history_path.exists():
            return []
        cutoff = time.time() - self.restore_minutes * 60
        rows = []
        try:
            with open(self.history_path, encoding="utf-8") as f:
                tail = f.readlines()[-self.restore_turns:]
            for line in tail:
                row = json.loads(line)
                # a question Jarvis asked is never replayed: brought back later, it gets asked all over again
                if (row.get("t", 0) >= cutoff and row.get("user") and row.get("assistant")
                        and not row["assistant"].rstrip().endswith("?")):
                    rows.append(row)
        except (OSError, ValueError):
            log.warning("couldn't read %s", self.history_path)
        return rows

    def seed_messages(self) -> list[dict]:
        """Plain user/assistant text turns, valid for every provider."""
        msgs = []
        for row in self.recent_exchanges():
            msgs += [{"role": "user", "content": row["user"]}, {"role": "assistant", "content": row["assistant"]}]
        return msgs

    def search_history(self, query: str, limit: int = 5, now: float | None = None,
                       skip_last: int = 0) -> list[dict]:
        """Earlier exchanges about something ("that song you recommended") or from some time ("yesterday"),
        newest first. skip_last leaves out the newest rows (the request being answered right now)."""
        now = now or time.time()
        rows = self._history_rows()
        if skip_last:
            rows = rows[:-skip_last] if len(rows) > skip_last else []
        start, end = time_window(query, now)
        rows = [r for r in rows if start <= r.get("t", 0) < end]
        topic = [w for w in content_words(query) if len(w) >= 3 and w not in HISTORY_TIME_WORDS
                 and w not in WEEKDAYS]
        if not topic:
            return rows[::-1][:limit]
        scored = []
        for i, row in enumerate(rows):
            words = content_words(f"{row['user']} {row['assistant']}")
            hits = sum(1 for t in topic if any(alike(t, w) for w in words))
            if hits:
                scored.append((-hits, -i, row))
        return [row for *_, row in sorted(scored, key=lambda s: (s[0], s[1]))[:limit]]


def time_window(query: str, now: float) -> tuple[float, float]:
    """(start, end) epoch seconds a history question is about; the whole log when it names no time."""
    t = " ".join(tokens(query))
    today = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    day = dt.timedelta(days=1)
    if re.search(r"\byesterday\b", t):
        return (today - day).timestamp(), today.timestamp()
    if re.search(r"\b(today|this morning|this afternoon|this evening|earlier)\b", t):
        return today.timestamp(), now + 1
    if re.search(r"\b(last|this|past) week\b", t):
        return (today - 7 * day).timestamp(), now + 1
    m = re.search(r"\b(?:on |last )?(" + "|".join(WEEKDAYS) + r")\b", t)
    if m:
        back = (today.weekday() - WEEKDAYS.index(m.group(1))) % 7 or 7
        start = today - back * day
        return start.timestamp(), (start + day).timestamp()
    m = re.search(r"\b(\d+|a|an|one|two|three|few) (hours?|days?) ago\b", t)
    if m:
        n = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "few": 3}.get(m.group(1)) or int(m.group(1))
        span = n * (3600 if m.group(2).startswith("hour") else 86400)
        return now - span - (3600 if span < 86400 else 86400), now - span + (3600 if span < 86400 else 86400)
    return 0.0, now + 1


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


__all__ = ["Memory", "absolute_dates", "subject_of", "time_window"]
