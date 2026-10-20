"""Provider interface. Each provider keeps its own conversation in its native message format,
so switching brains is just swapping one object for another."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Turn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolResult:
    call: ToolCall
    content: str
    is_error: bool = False
    spoken: str | None = None    # if set, this is the honest sentence to say for this call


class LLMProvider:
    name = "base"

    def __init__(self, system_prompt: str, tools: list[dict], temperature: float = 0.4, max_history: int = 20,
                 max_tool_rounds: int | None = None):
        self.system_prompt = system_prompt
        self.tools = tools              # neutral schema: {"name", "description", "parameters"}
        self.temperature = temperature
        self.max_history = max_history
        self.max_tool_rounds = max_tool_rounds   # None = assistant.max_tool_steps
        self.messages: list[dict] = []

    @property
    def label(self) -> str:
        return f"{self.name}:{getattr(self, 'model', '?')}"

    def reset(self):
        self.messages = []

    def set_tools(self, tools: list[dict]):
        """Change the tools offered from the next step on (a local brain gets only the ones that fit the request)."""
        self.tools = tools
        self._tool_defs = self.tool_defs(tools)

    def warn_if_full(self, prompt_tokens: int | None):
        """A prompt that fills the context window was cut at the start (Jarvis's instructions go first): say so."""
        num_ctx = getattr(self, "num_ctx", 0) or 0
        if isinstance(prompt_tokens, int) and num_ctx and prompt_tokens >= 0.95 * num_ctx:
            log.warning("the prompt filled the %d-token context (%d tokens): the model may have lost its "
                        "instructions; raise num_ctx or lower llm.max_tools", num_ctx, prompt_tokens)

    def tool_defs(self, tools: list[dict]) -> list[dict]:
        return [{"type": "function", "function": t} for t in tools]

    def add_user(self, text: str):
        self.messages.append({"role": "user", "content": text})

    def add_assistant_text(self, text: str):
        """Record a reply Jarvis gave without the model (fast path / direct tool result)."""
        self.messages.append({"role": "assistant", "content": text})

    def step(self, use_tools: bool = True, on_text: Callable[[str], None] | None = None) -> Turn:
        """Send the conversation, append the assistant reply to history, return it.
        If on_text is given, providers that can stream call it with each text fragment as it arrives."""
        raise NotImplementedError

    def add_tool_results(self, results: list[ToolResult]):
        raise NotImplementedError

    def check(self) -> str | None:
        """Return None if reachable, else a human-readable problem."""
        return None

    def _trim(self):
        """Drop oldest turns (never splitting a tool call from its result)."""
        if len(self.messages) <= self.max_history:
            return
        cut = len(self.messages) - self.max_history
        while cut < len(self.messages) and self.messages[cut]["role"] != "user":
            cut += 1
        self.messages = self.messages[cut:]
