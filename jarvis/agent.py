"""The agent loop: user text -> LLM -> tool calls -> LLM ... -> spoken reply."""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from typing import Callable

from .config import Section
from .llm import ALIASES, LLMProvider, ToolResult, create_provider
from .memory import Memory
from .tools import ToolBox

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are {name}, a voice assistant running on {user}'s Windows PC, modelled on J.A.R.V.I.S. \
from Iron Man: calm, precise, quietly witty, and loyal. {address_rule}

Everything you write is spoken aloud by a text-to-speech voice, so:
- Reply in one to three short sentences unless the user asks for detail.
- Plain words only: no markdown, lists, emoji, code blocks or URLs.

You can act on the computer through tools. When a request needs an action or live information, call the \
right tool instead of guessing, then report the outcome briefly. Never claim you did something unless a tool \
result confirms it. If a tool fails, say so plainly. If you're unsure what the user meant, ask one short question.
Do exactly what the latest request asks and nothing more: never start actions the user didn't ask for, \
and don't re-check your own work (no listing timers right after setting one, no reading notes right after \
saving one). web_search already opens the browser by itself.

The speech-to-text may mishear words; infer the most likely intent from context, but if a request is \
garbled or doesn't clearly ask for an action, ask what they meant instead of guessing an action.
Today is {today}."""

# Tools whose result is already the spoken answer ("Paused Spotify.", "Volume set to 30%."):
# when a turn only used these, their result is said directly instead of asking the brain to rephrase it
# (one model call instead of two, and no chance to "improve" a failure into a success).
SPEAKS_RESULT = {
    "media_control", "play_song", "volume", "app_volume", "open_app", "open_website", "web_search", "set_timer", "cancel_timer",
    "lock_computer", "take_screenshot", "take_note", "copy_to_clipboard", "type_text", "window_action",
    "close_window", "desktop_action", "press_keys", "open_settings", "open_folder", "theme", "switch_voice",
    "close_app", "power", "open_path",
}


class Agent:
    def __init__(self, cfg: Section, notify: Callable[[str], None] | None = None,
                 confirm: Callable[[str], bool] | None = None, provider: str | None = None,
                 speak: Callable[[str], None] | None = None,
                 switch_voice: Callable[[str], str] | None = None,
                 unduck: Callable[[], None] | None = None):
        self.cfg = cfg
        self.confirm = confirm or (lambda q: True)
        self.memory = Memory(cfg) if (cfg.get("memory") or {}).get("enabled", True) else None
        self.toolbox = ToolBox(cfg, context={
            "notify": notify, "switch_model": self._request_switch, "ask_claude": self._ask_claude,
            "memory": self.memory, "speak": speak, "switch_voice": switch_voice, "unduck": unduck})
        self._pending_switch: str | None = None
        self._lock = threading.RLock()   # requests, scheduled routines and tray brain switches take turns
        self._claude_helper = None
        self._last_activity = time.monotonic()
        self.provider: LLMProvider = self._make(provider or cfg.llm.provider)

    # ------------------------------------------------------------ providers

    def _system_prompt(self) -> str:
        a = self.cfg.assistant
        address = (a.get("address") or "").strip()
        rule = (f'Address the user as "{address}" now and then, not every sentence.' if address
                else "Speak to the user naturally, without titles like sir or madam.")
        prompt = SYSTEM_PROMPT.format(name=a.get("name") or "Jarvis", user=a.get("user_name") or "the user",
                                      address_rule=rule, today=dt.date.today().strftime("%A %d %B %Y"))
        return prompt + (self.memory.facts_prompt() if self.memory else "")

    def _make(self, name: str) -> LLMProvider:
        p = create_provider(name, self.cfg.llm, self._system_prompt(), self.toolbox.schemas())
        problem = p.check()
        if problem:
            log.warning(problem)
        if hasattr(p, "warm_up"):
            p.warm_up()
        self._fresh_conversation(p, prime_now=True)
        log.info("Brain: %s", p.label)
        return p

    def _fresh_conversation(self, p: LLMProvider, prime_now: bool = False):
        """New conversation: current date + remembered facts, and the last few exchanges replayed.
        (The system prompt only changes here, never mid-conversation, so histories stay append-only.)"""
        p.reset()
        old_prompt, p.system_prompt = p.system_prompt, self._system_prompt()
        if self.memory:
            p.messages = self.memory.seed_messages()
        if hasattr(p, "prime") and (prime_now or p.system_prompt != old_prompt):
            if prime_now:
                p.prime()          # at start-up: before "Online and ready"
            else:                  # mid-session: don't hold up the request that triggered it
                threading.Thread(target=p.prime, daemon=True).start()

    def switch(self, name: str) -> str:
        with self._lock:
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

    def handle(self, text: str, on_text: Callable[[str], None] | None = None) -> str:
        """Run one request. With on_text, reply text is streamed to it as it's generated
        (including any error or brain-switch message), so speech can start early."""
        with self._lock:  # an interrupted request may still be finishing in the background
            return self._handle(text, on_text)

    def _handle(self, text: str, on_text: Callable[[str], None] | None) -> str:
        idle_limit = float(self.cfg.assistant.get("idle_reset_minutes", 5)) * 60
        if time.monotonic() - self._last_activity > idle_limit:
            self._fresh_conversation(self.provider)
        self._last_activity = time.monotonic()
        emit = on_text or (lambda s: None)

        try:
            t0 = time.perf_counter()
            plan = None
            if (self.cfg.get("router") or {}).get("enabled", True):
                from .router import route

                plan = route(text, self.toolbox, self.cfg)
            if plan:
                reply = self._run_direct(text, plan, on_text)
                log.info("fast path %s in %.2fs", [n for n, _ in plan], time.perf_counter() - t0)
            else:
                reply = self._run(text, on_text)
                log.info("brain (%s) answered in %.2fs", self.provider.label, time.perf_counter() - t0)
            if self.memory and reply:
                self.memory.log_exchange(text, reply, self.provider.label)
        except Exception as e:
            log.exception("LLM error")
            reply = f"Sorry, my {self.provider.name} brain ran into a problem: {e.__class__.__name__}."
            problem = self.provider.check()
            if problem:
                reply += " " + problem
            emit(" " + reply)
            self._fresh_conversation(self.provider)

        if self._pending_switch:
            name, self._pending_switch = self._pending_switch, None
            note = self.switch(name)
            emit(" " + note)
            reply = (reply + " " if reply else "") + note
        self._last_activity = time.monotonic()
        return reply

    def _run(self, text: str, on_text: Callable[[str], None] | None = None) -> str:
        p = self.provider
        p.add_user(text)
        self._request = text
        # small local models tend to chain unrequested tool calls, so they get fewer rounds (config: max_tool_rounds)
        rounds = int(getattr(p, "max_tool_rounds", None) or self.cfg.assistant.get("max_tool_steps", 6))
        for _ in range(rounds):
            turn = p.step(on_text=on_text)
            if not turn.tool_calls:
                return turn.text.strip()
            if on_text and turn.text.strip():
                on_text(" ")   # keep "Let me check." apart from the answer that follows
            results = [self._execute(call) for call in turn.tool_calls]
            p.add_tool_results(results)
            direct = self._direct_reply(results)
            if direct:   # simple actions: say what really happened, no second model call
                p.add_assistant_text(direct)
                if on_text:
                    on_text(direct)
                return direct
        # out of tool rounds: ask for the spoken answer with tools switched off
        turn = p.step(use_tools=False, on_text=on_text)
        return turn.text.strip() or "Done."

    @staticmethod
    def _direct_reply(results: list[ToolResult]) -> str | None:
        spoken = [r.spoken for r in results]
        return " ".join(spoken) if spoken and all(spoken) else None

    def _run_direct(self, text: str, plan: list[tuple[str, dict]], on_text) -> str:
        """Fast path: the router already knows which tool to run; no model call at all."""
        from .llm import ToolCall

        self._request = text
        results = [self._execute(ToolCall(f"fast_{i}", name, args), gate=False)
                   for i, (name, args) in enumerate(plan)]
        reply = self._direct_reply(results) or " ".join(r.content for r in results)
        self.provider.add_user(text)
        self.provider.add_assistant_text(reply)   # so follow-ups ("and louder") have context
        if on_text:
            on_text(reply)
        return reply

    def _execute(self, call, gate: bool = True) -> ToolResult:
        tool = self.toolbox.get(call.name)
        log.info("tool %s(%s)", call.name, json.dumps(call.arguments)[:200])
        if not tool:
            return ToolResult(call, f"Unknown tool {call.name}", is_error=True)
        if gate and not tool.allowed_for(getattr(self, "_request", "")):
            log.info("  gate: %s not run (request doesn't mention it)", call.name)
            return ToolResult(call, f"NOT DONE: {call.name} was not run because the request didn't ask for it. "
                                    "Nothing happened on the PC. Do not say you did it; answer directly or ask.",
                              is_error=True)
        missing = [r for r in tool.parameters.get("required", []) if call.arguments.get(r) in (None, "")]
        if missing:  # small models sometimes fire tools with empty arguments
            return ToolResult(call, f"Not run: missing {', '.join(missing)}. Only call tools the user asked for.",
                              is_error=True)
        speaks = call.name in SPEAKS_RESULT
        if tool.dangerous and self.cfg.tools.get("confirm_dangerous", True):
            if not self.confirm(tool.confirmation(call.arguments)):
                return ToolResult(call, "The user declined; nothing was done.",
                                  spoken="Okay, I've left it." if speaks else None)
        t0 = time.perf_counter()
        try:
            out = self.toolbox.run(call.name, call.arguments)
            log.info("  -> %s (%.2fs)", out[:200], time.perf_counter() - t0)
            from .tools import Spoken

            direct = out if (speaks or isinstance(out, Spoken)) else None
            return ToolResult(call, out, spoken=direct)
        except Exception as e:
            from .tools.builtin import ToolFailed

            log.warning("tool %s failed: %s", call.name, e)
            if isinstance(e, ToolFailed):   # an expected failure, already phrased for the user
                return ToolResult(call, f"Failed: {e}", is_error=True, spoken=str(e))
            return ToolResult(call, f"Error: {e}", is_error=True,
                              spoken=f"That didn't work: {e}" if speaks else None)
