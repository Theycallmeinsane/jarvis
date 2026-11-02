"""Long-term memory tools: remember / forget / list, and recalling earlier conversations."""
from __future__ import annotations

import datetime as dt

from .registry import tool


def _mem(ctx):
    mem = ctx.context.get("memory")
    if mem is None:
        raise RuntimeError("memory is turned off")
    return mem


def _memory_on(cfg) -> bool:
    return (cfg.get("memory") or {}).get("enabled", True)


# Only on an explicit ask: small models used to "remember" things nobody said (once, the example that used to
# sit in this description), and every remembered fact is repeated in every later conversation.
@tool("Remember something about the user for future conversations, only when they ask you to remember it.",
      {"fact": {"type": "string", "description": "What to remember, in the user's own words"}},
      enabled=_memory_on, triggers=("remember", "don't forget", "do not forget", "keep in mind"))
def remember(fact: str, ctx=None):
    return _mem(ctx).remember(fact)


@tool("Forget remembered facts that contain the given words.",
      {"query": {"type": "string"}}, enabled=_memory_on)
def forget(query: str, ctx=None):
    return _mem(ctx).forget(query)


@tool("List everything Jarvis remembers about the user.", enabled=_memory_on)
def list_memories(ctx=None):
    facts = _mem(ctx).facts
    if not facts:
        return "I haven't been asked to remember anything yet."
    return "\n".join(f["fact"] + (" (you mentioned it)" if f.get("source") == "noticed" else "") for f in facts)


def _when(t: float) -> str:
    d = dt.datetime.fromtimestamp(t)
    today = dt.date.today()
    day = ("today" if d.date() == today else "yesterday" if d.date() == today - dt.timedelta(days=1)
           else f"on {d:%A} {d.day} {d:%B}")
    return f"{day} at {d:%H:%M}"


@tool("Look back through earlier conversations with the user: what was said about something, or what you talked "
      "about at some time (yesterday, this morning, last week). Use for 'what did we talk about', 'what was that "
      "song you mentioned', 'what did I ask you yesterday'.",
      {"query": {"type": "string", "description": "What or when to look for, in the user's words"}},
      enabled=_memory_on,
      triggers=("we talk", "we talked", "we discuss", "did i ask", "did i say", "did i tell", "you said",
                "you told", "you mention", "you recommend", "you suggest", "did you say", "earlier", "yesterday",
                "last time", "last week", "conversation", "we chat", "remind me what", "what was that",
                "this morning", "ago"))
def recall_conversation(query: str, ctx=None):
    # the request being answered is not in the log yet, so nothing to skip
    rows = _mem(ctx).search_history(query, limit=5)
    if not rows:
        return "I found nothing about that in our earlier conversations."
    return "Earlier conversations (newest first):\n" + "\n".join(
        f"- {_when(r['t'])}, the user said: {r['user'][:200]} | you answered: {r['assistant'][:300]}" for r in rows)
