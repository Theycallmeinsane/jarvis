"""Ollama native /api/chat with tool calling (default local brain). Works with any installed model."""
from __future__ import annotations

import json
from typing import Callable

import requests

from ..config import local_url

from .base import LLMProvider, ToolCall, ToolResult, Turn
from .ollama_models import OllamaUnavailable, list_models, resolve


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, *, base_url: str = "http://127.0.0.1:11434", model: str = "auto",
                 think: bool = False, keep_alive: str = "30m", num_ctx: int = 8192, **kw):
        super().__init__(**kw)
        self.base_url = local_url(base_url).rstrip("/")
        self.wanted = (model or "auto").strip()   # what the config asked for; "auto" = the best installed model
        self.model = self.wanted                  # what actually runs (set by check())
        self.capabilities: list[str] = []         # from /api/show; empty = unknown (older Ollama)
        self.installed: int | None = None         # how many models Ollama has
        self.note: str | None = None              # e.g. "qwen3.5:4b isn't pulled, using llama3.2:3b; ..."
        self.think = think
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self._tool_defs = [{"type": "function", "function": t} for t in self.tools]

    def check(self) -> str | None:
        """Pick the model to run: the configured one, or the best installed one if that isn't pulled."""
        try:
            models = list_models(self.base_url)
        except OllamaUnavailable as e:
            return str(e)
        self.installed = len(models)
        pick, note = resolve(models, self.wanted)
        if pick is None:
            return note
        self.model, self.capabilities, self.note = pick["name"], pick["capabilities"], note or None
        return None

    def _can(self, capability: str) -> bool:
        return capability in self.capabilities if self.capabilities else True

    def _ready(self):
        if self.model == "auto":   # Ollama wasn't up at start-up: look for a model now
            self.check()

    def warm_up(self):
        """Load the model into VRAM now so the first answer isn't slow."""
        self._ready()
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
        self._ready()
        try:
            self._post(self._body([{"role": "system", "content": self.system_prompt},
                                   {"role": "user", "content": "hi"}], use_tools=True, stream=False, num_predict=1),
                       stream=False)
        except requests.RequestException:
            pass

    def _body(self, messages: list[dict], use_tools: bool, stream: bool, **options) -> dict:
        body = {
            "model": self.model,
            "messages": messages,
            # a model without tool calling (e.g. gemma3) can still chat; the fast path runs the PC commands
            "tools": self._tool_defs if use_tools and self._can("tools") else [],
            "stream": stream,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx, **options},
        }
        if self._can("thinking"):   # only thinking models (qwen3, deepseek-r1...) take the switch
            body["think"] = self.think
        return body

    def _post(self, body: dict, stream: bool):
        r = requests.post(f"{self.base_url}/api/chat", json=body, timeout=180, stream=stream)
        if r.status_code == 400 and "think" in r.text and "think" in body:   # model without a thinking switch
            body.pop("think")
            r = requests.post(f"{self.base_url}/api/chat", json=body, timeout=180, stream=stream)
        if r.status_code == 400 and "tools" in r.text and body.get("tools"):  # model without tool calling
            body["tools"] = []
            r = requests.post(f"{self.base_url}/api/chat", json=body, timeout=180, stream=stream)
        r.raise_for_status()
        return r

    def step(self, use_tools: bool = True, on_text: Callable[[str], None] | None = None) -> Turn:
        self._trim()
        self._ready()
        body = self._body([{"role": "system", "content": self.system_prompt}] + self.messages,
                          use_tools=use_tools, stream=on_text is not None)
        if on_text is None:
            msg = self._post(body, stream=False).json()["message"]
        else:  # stream tokens so the voice can start on the first sentence
            content, tool_calls = [], []
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
                        break
            msg = {"role": "assistant", "content": "".join(content)}
            if tool_calls:
                msg["tool_calls"] = tool_calls
        msg.pop("thinking", None)
        self.messages.append(msg)

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
