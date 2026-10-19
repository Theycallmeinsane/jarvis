from __future__ import annotations

from ..config import Section
from .base import LLMProvider, ToolCall, ToolResult, Turn

PROVIDERS = ("ollama", "lmstudio", "openai_compat", "claude")

ALIASES = {
    "local": "ollama", "ollama": "ollama",
    "lm studio": "lmstudio", "lmstudio": "lmstudio",
    "openai": "openai_compat", "openai_compat": "openai_compat", "groq": "openai_compat",
    "claude": "claude", "anthropic": "claude", "cloud": "claude",
}


def create_provider(name: str, llm_cfg: Section, system_prompt: str, tools: list[dict],
                    model: str | None = None) -> LLMProvider:
    """model: use this model instead of the configured one (e.g. an Ollama model picked by voice)."""
    name = ALIASES.get(name.lower().strip(), name)
    opts = dict((llm_cfg.get("providers") or {}).get(name) or {})
    if model:
        opts["model"] = model
    common = dict(system_prompt=system_prompt, tools=tools,
                  temperature=llm_cfg.get("temperature", 0.4),
                  max_history=llm_cfg.get("max_history_messages", 20))
    if name == "ollama":
        from .ollama import OllamaProvider

        return OllamaProvider(**opts, **common)
    if name == "lmstudio":
        from .openai_compat import LMStudioProvider

        return LMStudioProvider(**opts, **common)
    if name == "openai_compat":
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(**opts, **common)
    if name == "claude":
        from .anthropic_claude import ClaudeProvider

        return ClaudeProvider(**opts, **common)
    raise ValueError(f"Unknown LLM provider {name!r}. Choose from: {', '.join(PROVIDERS)}")


__all__ = ["create_provider", "LLMProvider", "ToolCall", "ToolResult", "Turn", "PROVIDERS", "ALIASES"]
