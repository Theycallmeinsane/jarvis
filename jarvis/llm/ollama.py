"""Ollama native /api/chat with tool calling (default local brain)."""
from __future__ import annotations

import requests

from .base import LLMProvider, ToolCall, ToolResult, Turn


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, *, base_url: str = "http://localhost:11434", model: str = "qwen3.5:4b",
                 think: bool = False, keep_alive: str = "30m", num_ctx: int = 8192, **kw):
        super().__init__(**kw)
        self.base_url = base_url.rstrip("/")
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

    def step(self, use_tools: bool = True) -> Turn:
        self._trim()
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system_prompt}] + self.messages,
            "tools": self._tool_defs if use_tools else [],
            "stream": False,
            "think": self.think,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        r = requests.post(f"{self.base_url}/api/chat", json=body, timeout=180)
        if r.status_code == 400 and "think" in r.text:   # model without a thinking switch
            body.pop("think")
            r = requests.post(f"{self.base_url}/api/chat", json=body, timeout=180)
        r.raise_for_status()
        msg = r.json()["message"]
        msg.pop("thinking", None)
        self.messages.append(msg)

        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                import json

                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args))
        return Turn(text=msg.get("content") or "", tool_calls=calls)

    def add_tool_results(self, results: list[ToolResult]):
        for res in results:
            self.messages.append({"role": "tool", "tool_name": res.call.name, "content": res.content})
