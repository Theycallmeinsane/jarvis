"""The agent loop: user text -> LLM -> tool calls -> LLM ... -> spoken reply."""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Callable

from .config import Section
from .llm import ALIASES, LLMProvider, ToolResult, create_provider
from .memory import Memory
from .tools import ToolBox

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are {name}, a voice assistant running on {user}'s Windows PC, modelled on J.A.R.V.I.S. \
from Iron Man: calm, precise, quietly witty, and loyal. Address the user as "sir" now and then, not every sentence.

Everything you write is spoken aloud by a text-to-speech voice, so:
- Reply in one to three short sentences unless the user asks for detail.
- Plain words only: no markdown, lists, emoji, code blocks or URLs.

You can act on the computer through tools. When a request needs an action or live information, call the \
right tool instead of guessing, then report the outcome briefly. Never claim you did something unless a tool \
result confirms it. If a tool fails, say so plainly. If you're unsure what the user meant, ask one short question.
Do exactly what the latest request asks and nothing more: never start actions the user didn't ask for, \
and don't re-check your own work (no listing timers right after setting one, no reading notes right after \
saving one). web_search already opens the browser by itself.

The speech-to-text may mishear words; infer the most likely intent from context.
Today is {today}."""


class Agent:
    def __init__(self, cfg: Section, notify: Callable[[str], None] | None = None,
                 confirm: Callable[[str], bool] | None = None, provider: str | None = None):
        self.cfg = cfg
        self.confirm = confirm or (lambda q: True)
        self.memory = Memory(cfg) if (cfg.get("memory") or {}).get("enabled", True) else None
        self.toolbox = ToolBox(cfg, context={
            "notify": notify, "switch_model": self._request_switch, "ask_claude": self._ask_claude,
            "memory": self.memory})
        self._pending_switch: str | None = None
        self._claude_helper = None
        self._last_activity = time.monotonic()
        self.provider: LLMProvider = self._make(provider or cfg.llm.provider)

    # ------------------------------------------------------------ providers

    def _system_prompt(self) -> str:
        a = self.cfg.assistant
        prompt = SYSTEM_PROMPT.format(name=a.get("name", "Jarvis"), user=a.get("user_name", "the user"),
                                      today=dt.date.today().strftime("%A %d %B %Y"))
        return prompt + (self.memory.facts_prompt() if self.memory else "")

    def _make(self, name: str) -> LLMProvider:
        p = create_provider(name, self.cfg.llm, self._system_prompt(), self.toolbox.schemas())
        problem = p.check()
        if problem:
            log.warning(problem)
        if hasattr(p, "warm_up"):
            p.warm_up()
        self._fresh_conversation(p)
        log.info("Brain: %s", p.label)
        return p

    def _fresh_conversation(self, p: LLMProvider):
        """New conversation: current date + remembered facts, and the last few exchanges replayed.
        (The system prompt only changes here, never mid-conversation, so histories stay append-only.)"""
        p.reset()
        p.system_prompt = self._system_prompt()
        if self.memory:
            p.messages = self.memory.seed_messages()

    def switch(self, name: str) -> str:
        try:
            self.provider = self._make(name)
        except Exception as e:
            return f"I couldn't switch to {name}: {e}"
        return f"Now using {self.provider.label}."

    def _request_switch(self, name: str) -> str:
        key = ALIASES.get(name.lower(), name)
        if key == self.provider.name:
            return f"Already using {self.provider.label}."
        self._pending_switch = key
        return f"Switching to {key} after this reply."

    def _ask_claude(self, question: str) -> str:
        if self.provider.name == "claude":
            return "You are already Claude; answer directly."
        if self._claude_helper is None:
            self._claude_helper = create_provider("claude", self.cfg.llm, self._system_prompt(), [])
        return self._claude_helper.ask_once(question)

    # ------------------------------------------------------------ main entry

    def handle(self, text: str) -> str:
        idle_limit = float(self.cfg.assistant.get("idle_reset_minutes", 5)) * 60
        if time.monotonic() - self._last_activity > idle_limit:
            self._fresh_conversation(self.provider)
        self._last_activity = time.monotonic()

        try:
            reply = self._run(text)
            if self.memory and reply:
                self.memory.log_exchange(text, reply, self.provider.label)
        except Exception as e:
            log.exception("LLM error")
            reply = f"Sorry, my {self.provider.name} brain ran into a problem: {e.__class__.__name__}."
            problem = self.provider.check()
            if problem:
                reply += " " + problem
            self._fresh_conversation(self.provider)

        if self._pending_switch:
            name, self._pending_switch = self._pending_switch, None
            reply = (reply + " " if reply else "") + self.switch(name)
        self._last_activity = time.monotonic()
        return reply

    def _run(self, text: str) -> str:
        p = self.provider
        p.add_user(text)
        # small local models tend to chain unrequested tool calls, so they get fewer rounds (config: max_tool_rounds)
        rounds = int(getattr(p, "max_tool_rounds", None) or self.cfg.assistant.get("max_tool_steps", 6))
        for _ in range(rounds):
            turn = p.step()
            if not turn.tool_calls:
                return turn.text.strip()
            p.add_tool_results([self._execute(call) for call in turn.tool_calls])
        # out of tool rounds: ask for the spoken answer with tools switched off
        turn = p.step(use_tools=False)
        return turn.text.strip() or "Done, sir."

    def _execute(self, call) -> ToolResult:
        tool = self.toolbox.get(call.name)
        log.info("tool %s(%s)", call.name, json.dumps(call.arguments)[:200])
        if not tool:
            return ToolResult(call, f"Unknown tool {call.name}", is_error=True)
        missing = [r for r in tool.parameters.get("required", []) if call.arguments.get(r) in (None, "")]
        if missing:  # small models sometimes fire tools with empty arguments
            return ToolResult(call, f"Not run: missing {', '.join(missing)}. Only call tools the user asked for.",
                              is_error=True)
        if tool.dangerous and self.cfg.tools.get("confirm_dangerous", True):
            if not self.confirm(tool.confirmation(call.arguments)):
                return ToolResult(call, "The user declined; nothing was done.")
        try:
            out = self.toolbox.run(call.name, call.arguments)
            log.info("  -> %s", out[:200])
            return ToolResult(call, out)
        except Exception as e:
            log.warning("tool %s failed: %s", call.name, e)
            return ToolResult(call, f"Error: {e}", is_error=True)
