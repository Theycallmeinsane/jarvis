"""Any OpenAI-compatible /chat/completions server: LM Studio, llama.cpp, vLLM, Groq, OpenRouter, OpenAI."""
from __future__ import annotations

import json
import os

import requests

from .base import LLMProvider, ToolCall, ToolResult, Turn


class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"

    def __init__(self, *, base_url: str, model: str, api_key_env: str | None = None, **kw):
        super().__init__(**kw)
        self.base_url = base_url.rstrip("/")
        self.model = model
        key = os.environ.get(api_key_env) if api_key_env else None
        self.headers = {"Authorization": f"Bearer {key or 'not-needed'}"}
        self._tool_defs = [{"type": "function", "function": t} for t in self.tools]

    def check(self) -> str | None:
        try:
            r = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=5)
            r.raise_for_status()
        except requests.RequestException as e:
            return f"Can't reach {self.base_url} ({e.__class__.__name__}). Is the server running?"
        return None

    def step(self, use_tools: bool = True) -> Turn:
        self._trim()
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system_prompt}] + self.messages,
            "temperature": self.temperature,
        }
        if use_tools and self._tool_defs:
            body["tools"] = self._tool_defs
        r = requests.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers, timeout=180)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        clean = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            clean["tool_calls"] = msg["tool_calls"]
        self.messages.append(clean)

        calls = []
        for tc in msg.get("tool_calls") or []:
            raw = tc["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                args = {}
            calls.append(ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=args))
        return Turn(text=clean["content"], tool_calls=calls)

    def add_tool_results(self, results: list[ToolResult]):
        for res in results:
            self.messages.append({"role": "tool", "tool_call_id": res.call.id, "content": res.content})


class LMStudioProvider(OpenAICompatProvider):
    name = "lmstudio"
