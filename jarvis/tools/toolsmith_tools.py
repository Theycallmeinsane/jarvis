"""Voice access to the tool builder: "build a tool that ...", "build it" (after Jarvis said it can't do something
yet), and managing the tools Jarvis wrote ("what tools have you built", "turn off the currency tool").
"""
from __future__ import annotations

import time

from .builtin import ToolFailed
from .registry import Spoken, tool

GAP_FRESH_S = 15 * 60          # "build it" refers to something Jarvis couldn't do in the last 15 minutes


def _on(cfg) -> bool:
    return (cfg.get("toolsmith") or {}).get("enabled", True)


def smith(ctx):
    s = ctx.context.get("toolsmith")
    if s is None:
        from ..toolsmith import Toolsmith

        s = ctx.context["toolsmith"] = Toolsmith(ctx.cfg, ctx)
    return s


def _say(ctx, text: str):
    fn = ctx.context.get("speak") or ctx.context.get("notify")
    if fn:
        try:
            fn(text)
        except Exception:
            pass


def _spoken_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


@tool("Write a brand-new tool (Python code, with tests) when the user asks you to build, code or learn a "
      "capability you don't have, or says 'build it' after you said you can't do something yet. The user approves "
      "it before it is switched on. Takes a minute or two.",
      {"capability": {"type": "string", "description": "What the new tool should do, in the user's words. Empty = "
                                                         "the thing you just said you couldn't do."}},
      required=[], enabled=_on,
      triggers=("build", "make a tool", "write a tool", "code", "create a tool", "teach yourself", "learn",
                "new tool", "new skill", "add a tool", "a tool", "skill", "yes", "do it", "go ahead"))
def build_tool(capability: str = "", ctx=None):
    s = smith(ctx)
    request = (capability or "").strip()
    gap = ctx.context.get("last_gap") or {}
    if not request or request.lower() in ("it", "that", "this", "one", "a tool", "the tool"):
        if not gap or time.time() - gap.get("t", 0) > GAP_FRESH_S:
            raise ToolFailed("Tell me what the new tool should do, for example: build a tool that converts "
                             "currencies.")
        request = gap["request"]
    _say(ctx, "On it. Writing and testing a new tool, this takes a minute or two.")
    build = s.build(request, progress=lambda text: _say(ctx, text))
    if not build.ok:
        if build.existing:
            return Spoken(build.message)
        raise ToolFailed(build.message)
    names = [n.replace("_", " ") for n in build.tools]
    what = build.summary.rstrip(".") or f"a tool called {names[0]}"
    risk = (f" It {_spoken_list(build.risks)}, so I'll ask before each use." if build.risks else "")
    question = (f"I've built {_spoken_list(names)}: {what}. It passed {build.tests_run} tests.{risk} "
                "Shall I switch it on?")
    confirm = ctx.context.get("confirm") or (lambda q: False)
    if not confirm(question):
        s.discard(build)
        return Spoken("Okay, I've thrown it away.")
    problem = s.install(build)
    if problem:
        s.discard(build)
        raise ToolFailed(problem)
    ctx.context["last_gap"] = None
    return Spoken(f"Done. {what[:1].upper() + what[1:]}: ask me again and I'll use it. "
                  f"The code is in jarvis/tools/generated/{build.module}.py.")


@tool("Manage the tools you wrote yourself: list them, turn one off, turn one back on (the user approves it "
      "again), delete one, or list the requests you couldn't handle yet (gaps).",
      {"action": {"type": "string", "enum": ["list", "disable", "enable", "delete", "gaps"]},
       "name": {"type": "string", "description": "The tool or module name, for disable / enable / delete"}},
      required=["action"], enabled=_on,
      triggers=("tool", "tools", "skill", "skills", "built", "build", "can't do", "cannot do", "couldn't",
                "can't you", "cant you", "can you not", "you can't", "you cannot", "gaps", "learned", "learnt",
                "delete", "remove", "disable", "enable", "turn off", "turn it off", "turn on", "turn it on",
                "switch off", "switch on", "switch it"))
def manage_tools(action: str, name: str = "", ctx=None):
    s = smith(ctx)
    if action == "gaps":
        rows = s.gaps(8)
        if not rows:
            return "Nothing so far: I haven't run into anything I couldn't do."
        return "Things I couldn't do yet, newest first:\n" + "\n".join(f"- {r['request']}" for r in rows)
    items = s.listing()
    if action == "list":
        if not items:
            return "I haven't built any tools yet. Say: build a tool that ..., and I'll write one."
        lines = []
        for it in items:
            state = {"on": "on", "changed": "off: its code changed since you approved it", "off": "off"}[it["status"]]
            lines.append(f"- {it['module'].replace('_', ' ')} ({state}): {it['summary'] or ', '.join(it['tools'])}")
        return "Tools I've built:\n" + "\n".join(lines)
    module = _find(items, name)
    if not module:
        raise ToolFailed(f"I haven't built a tool called {name or 'that'}.")
    spoken = module.replace("_", " ")
    confirm = ctx.context.get("confirm") or (lambda q: False)
    if action == "disable":
        s.remove(module)
        return f"Turned off {spoken}."
    if action == "delete":
        if not confirm(f"Delete the {spoken} tool and its tests for good?"):
            return "Okay, I've kept it."
        s.remove(module, delete_files=True)
        return f"Deleted {spoken}."
    if action == "enable":
        tools = s.tools_in(module)
        if not tools:
            raise ToolFailed(f"I couldn't read the {spoken} tool's code.")
        ok, output, ran = s.run_tests(module)
        if not ok:
            raise ToolFailed(f"The {spoken} tool fails its tests now, so I've left it off.")
        if not confirm(f"The {spoken} tool passed its {ran} tests. Switch it on?"):
            return "Okay, it stays off."
        from ..toolsmith import Build

        problem = s.install(Build(True, module=module, tools=tools, summary=_summary(items, module)))
        if problem:
            raise ToolFailed(problem)
        return f"{spoken[:1].upper() + spoken[1:]} is on."
    raise ToolFailed(f"I can't {action} tools.")


def _find(items: list[dict], name: str) -> str | None:
    from ..wordmatch import alike, tokens

    said = [w for w in tokens(name) if w not in ("the", "tool", "tools", "my")]
    if not said:
        return None
    best = None
    for it in items:
        words = tokens(it["module"].replace("_", " ") + " " + " ".join(it["tools"]).replace("_", " "))
        hits = sum(1 for w in said if any(alike(w, x) for x in words))
        if hits and (best is None or hits > best[0]):
            best = (hits, it["module"])
    return best[1] if best else None


def _summary(items: list[dict], module: str) -> str:
    return next((it["summary"] for it in items if it["module"] == module), "")
