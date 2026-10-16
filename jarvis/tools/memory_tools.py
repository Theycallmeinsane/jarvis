"""Long-term memory tools: remember / forget / list."""
from __future__ import annotations

from .registry import tool


def _mem(ctx):
    mem = ctx.context.get("memory")
    if mem is None:
        raise RuntimeError("memory is turned off")
    return mem


@tool("Remember a fact about the user for future conversations (preferences, names, plans). "
      "Use when the user says 'remember ...' or shares something clearly worth keeping.",
      {"fact": {"type": "string", "description": "Short fact about the user, e.g. 'favourite colour is blue'"}},
      enabled=lambda cfg: (cfg.get("memory") or {}).get("enabled", True))
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
