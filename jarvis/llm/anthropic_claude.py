"""Claude via the official Anthropic SDK (Messages API + tool use)."""
from __future__ import annotations

import os

from .base import LLMProvider, ToolCall, ToolResult, Turn


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self, *, model: str = "claude-opus-5-5", effort: str = "low", max_tokens: int = 16000,
                 api_key_env: str = "ANTHROPIC_API_KEY", **kw):
        super().__init__(**kw)
        import anthropic

        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        key = os.environ.get(api_key_env) if api_key_env else None
        # with no key set, the SDK falls back to ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile
        self.client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self._tool_defs = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in self.tools
        ]
        # Haiku has no server-side fallback; the others re-run a declined request on another model
        self._fallback = not model.startswith("claude-haiku")

    def check(self) -> str | None:
        if not any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")):
            return "No ANTHROPIC_API_KEY found. Add it to the .env file."
        return None

    def _create(self, **kwargs):
        if self._fallback:
            return self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        return self.client.messages.create(**kwargs)

    def _stream(self, on_text, **kwargs):
        # Tool inputs here are tiny (a city, a timer length), so they're left buffered
        # (no eager_input_streaming); only the spoken text needs to arrive early.
        manager = (self.client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"],
                                                    fallbacks="default", **kwargs)
                   if self._fallback else self.client.messages.stream(**kwargs))
        with manager as stream:
            for text in stream.text_stream:
                on_text(text)
            return stream.get_final_message()

    def step(self, use_tools: bool = True, on_text=None) -> Turn:
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system_prompt,
            messages=self.messages,
            # keep the tool list identical across turns (history has tool_use blocks); just forbid new calls
            tools=self._tool_defs,
            tool_choice={"type": "auto"} if use_tools else {"type": "none"},
            output_config={"effort": self.effort},
        )
        response = self._stream(on_text, **kwargs) if on_text else self._create(**kwargs)

        # keep the full content (thinking blocks included) so the history stays valid
        self.messages.append({
            "role": "assistant",
            "content": [b.model_dump(exclude_none=True) for b in response.content],
        })
        if response.stop_reason == "refusal":
            return Turn(text="I'm afraid I can't help with that one.")

        text = " ".join(b.text for b in response.content if b.type == "text")
        calls = [ToolCall(id=b.id, name=b.name, arguments=dict(b.input or {}))
                 for b in response.content if b.type == "tool_use"]
        return Turn(text=text, tool_calls=calls)

    def add_tool_results(self, results: list[ToolResult]):
        # all results for one assistant turn go back in a single user message
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.call.id, "content": r.content, "is_error": r.is_error}
            for r in results
        ]})

    def ask_once(self, question: str) -> str:
        """One-shot question with no tools (used by the 'ask Claude' tool from a local brain)."""
        kwargs = dict(model=self.model, max_tokens=self.max_tokens, system=self.system_prompt,
                      messages=[{"role": "user", "content": question}],
                      output_config={"effort": self.effort})
        response = self._create(**kwargs)
        return " ".join(b.text for b in response.content if b.type == "text")
