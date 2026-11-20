"""Long-term memory tools: remember / forget / list."""
from __future__ import annotations

from .registry import tool


def _mem(ctx):
    mem = ctx.context.get("memory")
    if mem is None:
        raise RuntimeError("memory is turned off")
    return mem


# Only on an explicit ask: small models used to "remember" things nobody said (once, the example that used to
# sit in this description), and every remembered fact is repeated in every later conversation.
@tool("Remember something about the user for future conversations, only when they ask you to remember it.",
      {"fact": {"type": "string", "description": "What to remember, in the user's own words"}},
      enabled=lambda cfg: (cfg.get("memory") or {}).get("enabled", True),
      triggers=("remember", "don't forget", "do not forget", "keep in mind"))
def remember(fact: str, ctx=None):
    return _mem(ctx).remember(fact)


@tool("Forget remembered facts that contain the given words.",
      {"query": {"type": "string"}}, enabled=lambda cfg: (cfg.get("memory") or {}).get("enabled", True))
def forget(query: str, ctx=None):
    return _mem(ctx).forget(query)


@tool("List everything Jarvis remembers about the user.",
      enabled=lambda cfg: (cfg.get("memory") or {}).get("enabled", True))
def list_memories(ctx=None):
    facts = _mem(ctx).facts
    return "\n".join(f["fact"] for f in facts) if facts else "I haven't been asked to remember anything yet."
