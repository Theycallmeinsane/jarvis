"""Provider interface. Each provider keeps its own conversation in its native message format,
so switching brains is just swapping one object for another."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


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

    def add_user(self, text: str):
        self.messages.append({"role": "user", "content": text})

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
