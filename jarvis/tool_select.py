"""Which tools a local brain sees for a request.

Sending every tool definition with every request costs about 10,000 tokens, more than a local model's
context window (llm.providers.ollama.num_ctx, 8192 by default). Ollama then quietly cuts the start of the
prompt, Jarvis's own instructions included, and the model loses track of who it is and starts making things
up. A small model also picks better from a short list than from a hundred. So a local brain gets only the
tools that fit the request (plus the ones from the turn before, so follow-ups like "and louder" still work);
Claude, with a far bigger context, still gets all of them.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "at", "by", "with", "from", "is", "it",
        "its", "be", "are", "was", "this", "that", "these", "those", "my", "me", "i", "you", "your", "can",
        "could", "would", "please", "jarvis", "hey", "what", "whats", "how", "do", "does", "did", "some",
        "any", "all", "just", "now", "up", "down", "out", "about", "into", "get", "set", "let", "make", "use",
        "when", "where", "which", "who", "why", "there", "here", "then", "than", "them", "they", "have", "has",
        "had", "not", "no", "yes", "ok", "okay", "will", "want", "need", "like", "tell", "give", "show", "so"}

# words people say for tools whose name and description don't contain them
SAID = {
    "get_datetime": "time date day today clock month year",
    "get_weather": "weather rain raining sunny forecast temperature hot cold umbrella outside",
    "volume": "volume louder quieter mute unmute sound loud quiet loudness",
    "set_timer": "timer alarm remind reminder minutes seconds hours wake",
    "list_timers": "timers alarms reminders left remaining",
    "cancel_timer": "cancel timer alarm reminder",
    "take_note": "note notes write jot",
    "read_notes": "note notes",
    "system_status": "cpu ram memory battery disk storage usage status",
    "system_stats": "cpu gpu ram temperature usage fans hot",
    "top_processes": "processes slow hogging laggy",
    "find_files": "find file files document documents pdf docx resume cv spreadsheet",
    "open_path": "folder file path",
    "read_clipboard": "clipboard copied",
    "copy_to_clipboard": "copy clipboard",
    "wikipedia_summary": "wikipedia explain history biography",
    "run_routine": "routine morning workflow",
    "switch_model": "switch brain model claude local",
    "remember": "remember",
    "forget": "forget",
    "list_memories": "remember remembered know memories",
}
GENERIC_NAME_WORDS = {"get", "set", "list", "read", "take", "run", "open", "find", "chrome", "action", "control",
                      "status", "info"}


def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) < 2 or w in STOP:
            continue
        out.add(w)
        for end in ("ing", "ed", "es", "s"):         # "timers" ~ "timer", "playing" ~ "play"
            if len(w) > len(end) + 2 and w.endswith(end):
                out.add(w[: -len(end)])
                break
    return out


def score(tool, request: str, said: set[str]) -> float:
    s = 0.0
    if tool.triggers and tool.allowed_for(request):
        s += 3
    name = set(tool.name.split("_")) - GENERIC_NAME_WORDS
    s += 2 * len(name & said)
    s += 2.5 * min(2, len(_words(SAID.get(tool.name, "")) & said))
    desc = tool.description if isinstance(tool.description, str) else ""
    s += 0.5 * min(4, len({w for w in _words(desc) if len(w) >= 4} & said))
    return s


def pick(toolbox, request: str, previous: list[str] | tuple[str, ...] = (), max_tools: int = 16) -> list[str]:
    """Names of the tools worth offering for this request, best first."""
    said = _words(request)
    ranked = []
    for name, tool in toolbox.tools.items():
        s = score(tool, request, said)
        if name in previous:
            s += 2.5
        if s >= 1.5:
            ranked.append((s, name))
    ranked.sort(key=lambda p: -p[0])
    return [n for _, n in ranked[:max_tools]]


def estimate_tokens(*parts) -> int:
    """Rough token count of prompt pieces (JSON-ish text runs ~3.3 characters a token)."""
    chars = sum(len(p) if isinstance(p, str) else len(json.dumps(p, ensure_ascii=False)) for p in parts)
    return int(chars / 3.3)


def fit(provider, schemas: list[dict], num_ctx: int, reserve: int = 1024) -> list[dict]:
    """Drop the least relevant tools, then the oldest messages, until the prompt fits the context window with
    room left for the answer. Returns the tools to send."""
    if not num_ctx:
        return schemas
    limit = num_ctx - reserve
    tools = list(schemas)

    def size():
        return estimate_tokens(provider.system_prompt, [{"type": "function", "function": t} for t in tools],
                               provider.messages)

    def trim(keep: int):        # oldest exchanges first, never starting mid tool-call
        while len(provider.messages) > keep and size() > limit:
            provider.messages = provider.messages[1:]
            while provider.messages and provider.messages[0].get("role") != "user":
                provider.messages = provider.messages[1:]

    trim(4)                                  # old conversation goes before the tools this request needs
    while tools and size() > limit:
        tools.pop()
    trim(1)
    if len(tools) < len(schemas):
        log.warning("prompt too big for the %d-token context: dropped %d tools", num_ctx, len(schemas) - len(tools))
    return tools
