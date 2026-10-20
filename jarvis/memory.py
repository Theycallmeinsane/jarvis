"""Persistent memory that survives restarts.

* facts   (data/memory.json)    - things the user asked Jarvis to remember; the ones a request is about are
                                  attached to it as a short note for the brain
* history (data/history.jsonl)  - every exchange (user text + spoken reply); the last few are replayed
                                  into a fresh conversation so Jarvis picks up where it left off
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from .config import Section, resolve_path
from .wordmatch import alike, content_words, tokens

log = logging.getLogger(__name__)

# "what do you know about me", "what have I told you": every fact, not just the matching ones
ASK_ALL = re.compile(r"\b(know|remember|remembered|saved|stored|kept|have|got) (on|about|of) (me|myself)\b|"
                     r"\bwhat have i told you\b|\btell me about (me|myself)\b|\byour memor(y|ies)\b|"
                     r"\b(do|did) you remember (anything|everything)\b|\bwhat (do|did|have) you (remember|remembered)$")
# words too common in facts to tie a request to one ("favourite", "name", "called")
FACT_STOP = {"favourite", "favorite", "like", "likes", "love", "loves", "name", "named", "called", "call",
             "remember", "forget", "know", "told", "really", "today", "always", "usually"}


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
        self._lock = threading.Lock()
        self.facts: list[dict] = self._load_facts()

    # ------------------------------------------------------------ facts

    def _load_facts(self) -> list[dict]:
        try:
            return json.loads(self.facts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _save_facts(self):
        self.facts_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.facts_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.facts, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.facts_path)

    def remember(self, fact: str) -> str:
        fact = fact.strip().rstrip(".")
        with self._lock:
            if any(f["fact"].lower() == fact.lower() for f in self.facts):
                return "I already know that."
            self.facts.append({"fact": fact, "added": time.strftime("%Y-%m-%d")})
            self.facts = self.facts[-self.max_facts:]
            self._save_facts()
        return f"I'll remember that {fact}."

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
        scored = []
        for age, fact in enumerate(reversed(facts)):           # age 0 = newest
            words = [w for w in content_words(fact) if len(w) >= 3 and w not in FACT_STOP]
            hits = sum(1 for s in said if any(alike(s, w) for w in words))
            if hits:
                scored.append((-hits, age, fact))
        return [fact for *_, fact in sorted(scored)[:self.relevant_facts_max]]

    def facts_note(self, request: str) -> str:
        """A short note to append to the user's message for the brain (never logged as what the user said)."""
        facts = self.relevant_facts(request)
        return f"\n\n[Memory: {'; '.join(facts)}]" if facts else ""

    # ------------------------------------------------------------ history

    def log_exchange(self, user: str, assistant: str, brain: str):
        if not self.enabled:
            return
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"t": time.time(), "user": user, "assistant": assistant, "brain": brain}
        with self._lock, open(self.history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

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
