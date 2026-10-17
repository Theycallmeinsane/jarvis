"""Any OpenAI-compatible /chat/completions server: LM Studio, llama.cpp, vLLM, Groq, OpenRouter, OpenAI."""
from __future__ import annotations

import json
import os
from typing import Callable

import requests

from ..config import local_url

from .base import LLMProvider, ToolCall, ToolResult, Turn


class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"

    def __init__(self, *, base_url: str, model: str, api_key_env: str | None = None, **kw):
        super().__init__(**kw)
        self.base_url = local_url(base_url).rstrip("/")
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

    def _stream(self, body: dict, on_text: Callable[[str], None]) -> dict:
        """Server-sent events; tool calls arrive as fragments keyed by index."""
        body = {**body, "stream": True}
        content: list[str] = []
        calls: dict[int, dict] = {}
        with requests.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers,
                           timeout=180, stream=True) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                choices = json.loads(data).get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                    on_text(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {
                        "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    slot["function"]["arguments"] += fn.get("arguments") or ""
        msg = {"content": "".join(content)}
        if calls:
            msg["tool_calls"] = [c | {"id": c["id"] or f"call_{i}"} for i, c in sorted(calls.items())]
        return msg

    def step(self, use_tools: bool = True, on_text: Callable[[str], None] | None = None) -> Turn:
        self._trim()
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system_prompt}] + self.messages,
            "temperature": self.temperature,
        }
        if use_tools and self._tool_defs:
            body["tools"] = self._tool_defs
        if on_text is not None:
            msg = self._stream(body, on_text)
        else:
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
