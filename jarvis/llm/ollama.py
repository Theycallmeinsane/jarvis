"""Ollama native /api/chat with tool calling (default local brain)."""
from __future__ import annotations

import json
from typing import Callable

import requests

from ..config import local_url

from .base import LLMProvider, ToolCall, ToolResult, Turn


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, *, base_url: str = "http://127.0.0.1:11434", model: str = "qwen3.5:4b",
                 think: bool = False, keep_alive: str = "30m", num_ctx: int = 8192, **kw):
        super().__init__(**kw)
        self.base_url = local_url(base_url).rstrip("/")
        self.model = model
        self.think = think
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self._tool_defs = [{"type": "function", "function": t} for t in self.tools]

    def check(self) -> str | None:
        try:
            tags = requests.get(f"{self.base_url}/api/tags", timeout=3).json()
        except requests.RequestException:
            return f"Ollama isn't running at {self.base_url}. Start the Ollama app."
        names = {m["name"] for m in tags.get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            return f"Ollama model {self.model} not found. Run: ollama pull {self.model}"
        return None

    def warm_up(self):
        """Load the model into VRAM now so the first answer isn't slow."""
        try:
            # same options as real requests, otherwise Ollama reloads the model on the first question
            requests.post(f"{self.base_url}/api/chat", json={
                "model": self.model, "messages": [], "keep_alive": self.keep_alive,
                "options": {"temperature": self.temperature, "num_ctx": self.num_ctx}}, timeout=120)
        except requests.RequestException:
            pass

    def prime(self):
        """Have Ollama process the system prompt + tool list once now, so the first real question
        reuses the cached prefix (~0.7 s) instead of paying the full ~3 s."""
        try:
            self._post({"model": self.model, "stream": False, "think": self.think, "keep_alive": self.keep_alive,
                        "messages": [{"role": "system", "content": self.system_prompt},
                                     {"role": "user", "content": "hi"}],
                        "tools": self._tool_defs,
                        "options": {"temperature": self.temperature, "num_ctx": self.num_ctx, "num_predict": 1}},
                       stream=False)
        except requests.RequestException:
            pass

    def _post(self, body: dict, stream: bool):
        r = requests.post(f"{self.base_url}/api/chat", json=body, timeout=180, stream=stream)
        if r.status_code == 400 and "think" in r.text:   # model without a thinking switch
            body.pop("think")
            r = requests.post(f"{self.base_url}/api/chat", json=body, timeout=180, stream=stream)
        r.raise_for_status()
        return r

    def step(self, use_tools: bool = True, on_text: Callable[[str], None] | None = None) -> Turn:
        self._trim()
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system_prompt}] + self.messages,
            "tools": self._tool_defs if use_tools else [],
            "stream": on_text is not None,
            "think": self.think,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        if on_text is None:
            reply = self._post(body, stream=False).json()
            msg = reply["message"]
        else:  # stream tokens so the voice can start on the first sentence
            content, tool_calls, reply = [], [], {}
            with self._post(body, stream=True) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    m = chunk.get("message") or {}
                    if m.get("content"):
                        content.append(m["content"])
                        on_text(m["content"])
                    tool_calls += m.get("tool_calls") or []
                    if chunk.get("done"):
                        reply = chunk
                        break
            msg = {"role": "assistant", "content": "".join(content)}
            if tool_calls:
                msg["tool_calls"] = tool_calls
        msg.pop("thinking", None)
        self.messages.append(msg)
        self.warn_if_full(reply.get("prompt_eval_count"))

        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args))
        return Turn(text=msg.get("content") or "", tool_calls=calls)

    def add_tool_results(self, results: list[ToolResult]):
        for res in results:
            self.messages.append({"role": "tool", "tool_name": res.call.name, "content": res.content})
