"""A tiny tool registry. Decorate a function with @tool and Jarvis can call it."""
from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)


class Spoken(str):
    """Return this from a tool to have its text said to the user directly, without the brain rephrasing it."""


@dataclass
class Tool:
    name: str
    description: str | Callable[[Any], str]   # a callable gets the config (e.g. to list routine names)
    parameters: dict
    func: Callable[..., Any]
    dangerous: bool = False          # ask for confirmation before running
    enabled: Callable[[Any], bool] | None = None   # cfg -> bool
    confirm: Callable[[dict], str] | None = None   # args -> the question Jarvis asks before running
    # the user's request must contain one of these words, or the call is refused (stops small models
    # from e.g. opening a browser search when asked for a story)
    triggers: tuple[str, ...] = ()

    def allowed_for(self, request: str) -> bool:
        if not self.triggers:
            return True
        return bool(re.search(r"\b(" + "|".join(self.triggers) + r")", request, re.I))

    def schema(self, cfg=None) -> dict:
        desc = self.description(cfg) if callable(self.description) else self.description
        return {"name": self.name, "description": desc, "parameters": self.parameters}

    def confirmation(self, args: dict) -> str:
        if self.confirm:
            return self.confirm(args)
        detail = ", ".join(f"{k} {v}" for k, v in args.items())
        return f"Shall I {self.name.replace('_', ' ')} {detail}?".replace("  ", " ")


_TOOLS: dict[str, Tool] = {}


def tool(description: str | Callable[[Any], str], params: dict[str, dict] | None = None, required: list[str] | None = None,
         dangerous: bool = False, enabled: Callable[[Any], bool] | None = None, name: str | None = None,
         confirm: Callable[[dict], str] | None = None, triggers: tuple[str, ...] = ()):
    """Register a tool. `params` maps argument name -> JSON-schema snippet."""
    def deco(fn):
        props = params or {}
        req = required if required is not None else [
            p for p, v in inspect.signature(fn).parameters.items()
            if v.default is inspect.Parameter.empty and p in props
        ]
        schema = {"type": "object", "properties": props, "required": req}
        t = Tool(name or fn.__name__, description, schema, fn, dangerous, enabled, confirm, tuple(triggers))
        _TOOLS[t.name] = t
        return fn
    return deco


class ToolBox:
    """The set of tools active for this run, plus shared context they can use."""

    def __init__(self, cfg, context: dict[str, Any] | None = None):
        from . import (builtin, files_ext, github_tools, google_tools, memory_tools, rgb_tools,  # noqa: F401  built-ins
                       system_ext, vlc_tools, windows)

        self.cfg = cfg
        self.context = context or {}
        self.tools = {n: t for n, t in _TOOLS.items() if t.enabled is None or t.enabled(cfg)}
        if cfg.get("mcp_servers"):
            from ..mcp_client import mcp_tools

            for t in mcp_tools(cfg):
                self.tools.setdefault(t.name, t)
        # tools.disabled: names you never want offered (fewer tools = faster, steadier small models)
        for name in (cfg.get("tools") or {}).get("disabled") or []:
            self.tools.pop(name, None)

    def schemas(self) -> list[dict]:
        return [t.schema(self.cfg) for t in self.tools.values()]

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def run(self, name: str, args: dict) -> str:
        t = self.tools.get(name)
        if not t:
            raise KeyError(f"Unknown tool {name}")
        sig = inspect.signature(t.func)
        if "ctx" in sig.parameters:
            args = {**args, "ctx": self}
        takes_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        allowed = args if takes_any else {k: v for k, v in args.items() if k in sig.parameters}
        result = t.func(**allowed)
        if result is None:
            return "done"
        return result if isinstance(result, str) else str(result)   # keeps Spoken
