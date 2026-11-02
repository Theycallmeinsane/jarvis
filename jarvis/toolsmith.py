"""The tool builder: when Jarvis has no tool for something, it writes one.

    request ("convert 50 dollars to rupees", no tool fits)
      -> Jarvis says it can't yet, logs the gap (data/tool_gaps.jsonl) and offers to build it
      -> "build it": a coding model writes the tool and its tests
      -> static review: the code is parsed, nothing forbidden (eval, exec, secrets...), anything that changes the
         PC or sends data makes the tool `dangerous` (Jarvis asks before every run)
      -> its tests run in a sandboxed process (jarvis/tool_sandbox.py)
      -> review or test failures go back to the model to fix, a few rounds at most
      -> passed: Jarvis says what the tool does and asks the user to switch it on
      -> approved: the module stays in jarvis/tools/generated, its tests in tests/generated, the approval and the
         code's fingerprint in data/toolsmith.json, and the tool is offered from the next request on

Which model writes the code: toolsmith.provider (auto = Claude when an API key is set, otherwise the current brain;
a local coder such as qwen2.5-coder works through toolsmith.model).
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import ROOT, Section, resolve_path
from .tool_sandbox import sandbox_env
from .tools import generated

log = logging.getLogger(__name__)

TOOLS_DIR = generated.HERE
TESTS_DIR = ROOT / "tests" / "generated"

# never in generated code
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint", "input", "globals", "vars", "getattr_static"}
FORBIDDEN_MODULES = {"importlib", "pickle", "marshal", "shelve", "code", "codeop", "pty", "runpy", "builtins",
                     "keyring", "win32cred", "browser_cookie3", "pynput", "keyboard"}
JARVIS_ALLOWED = {"jarvis.tools.registry", "jarvis.tools.builtin", "jarvis.config", "jarvis.webtext",
                  "jarvis.websearch", "jarvis.wordmatch"}
SECRET_PATTERNS = re.compile(r"""['"][^'"]*(\.env\b|secrets[\\/]|google_token|google_credentials|"""
                             r"""ANTHROPIC_API_KEY|Login Data|Cookies|\.ssh[\\/]|toolsmith\.json)""", re.I)

# fine in a tool, but the tool then asks before each run (dangerous=True)
RISKY_MODULES = {"subprocess": "runs programs", "shutil": "moves or deletes files", "send2trash": "deletes files",
                 "winreg": "touches the registry", "ctypes": "calls Windows directly", "socket": "opens network "
                 "connections", "smtplib": "sends email", "ftplib": "uploads files", "telnetlib": "remote access",
                 "pyautogui": "controls the mouse and keyboard", "win32api": "calls Windows directly",
                 "win32con": "calls Windows directly", "wmi": "changes system settings", "psutil": None}
# method calls that change things: (method, owner or None for any owner) -> why
RISKY_CALLS = {
    **{(m, "os"): why for m, why in {"remove": "deletes files", "unlink": "deletes files", "rmdir": "deletes folders",
                                     "removedirs": "deletes folders", "rename": "moves files", "replace": "moves files",
                                     "kill": "stops processes", "startfile": "opens programs",
                                     "chmod": "changes permissions"}.items()},
    **{(m, None): why for m, why in {"write_text": "writes files", "write_bytes": "writes files",
                                     "unlink": "deletes files", "rmdir": "deletes folders", "touch": "creates files",
                                     "rename": "moves files", "kill": "stops processes",
                                     "terminate": "stops processes", "suspend": "pauses processes",
                                     "sendmail": "sends email", "send_message": "sends messages",
                                     "nice": "changes priorities"}.items()},
    **{(m, o): "sends data online" for m in ("post", "put", "patch", "delete")
       for o in ("requests", "session", "httpx", "client", "http", "api")},
}

EXISTING = re.compile(r"^\s*EXISTING:\s*(\w+)", re.M)

SYSTEM = """You write tools for Jarvis, a Python voice assistant on Windows. A tool is a Python function the \
assistant's language model can call. You reply with complete files only, no explanations."""

GUIDE = """Write a new Jarvis tool for this request:

  "{request}"

Tools Jarvis already has (don't rebuild one of these; if one of them already does this, reply with only
EXISTING: <tool_name>):
{existing}

How a tool module looks (it is saved as jarvis/tools/generated/{module_hint}.py):

```python
# file: tool.py
\"\"\"One line on what these tools do.\"\"\"
from __future__ import annotations

import requests

from ..builtin import ToolFailed          # raise ToolFailed("plain sentence") for failures the user should hear
from ..registry import tool


@tool("What the tool does and when to use it, for the language model.",
      {{"amount": {{"type": "number", "description": "How much"}},
        "currency": {{"type": "string", "description": "Three-letter code, e.g. EUR"}}}},
      required=["amount", "currency"],
      triggers=("convert", "exchange", "in dollars"),   # the user's request must contain one of these
      keywords="currency exchange rate convert dollars euros rupees")   # words people say for it
def convert_currency(amount: float, currency: str, ctx=None):
    r = requests.get("https://example.com/rates", params={{"base": "USD"}}, timeout=10)
    if r.status_code != 200:
        raise ToolFailed("The exchange rate service didn't answer.")
    rate = r.json()["rates"].get(currency.upper())
    if rate is None:
        raise ToolFailed(f"I don't know the currency {{currency}}.")
    return f"{{amount}} US dollars is {{amount * rate:.2f}} {{currency.upper()}}."
```

Rules for the tool:
- One to three @tool functions with clear snake_case names that aren't taken. Return a short plain-text string
  that can be spoken (no markdown, no URLs read out), or raise ToolFailed with a short spoken explanation.
- Optional last parameter `ctx=None`: Jarvis's toolbox (ctx.cfg = settings, ctx.context["confirm"](question) asks
  the user yes/no). Don't use anything else from Jarvis.
- Standard library first. Installed packages you may use: requests, psutil, numpy, pyyaml (yaml), pillow (PIL),
  pypdf, python-docx (docx), pycaw, comtypes, ddgs, send2trash. Nothing else.
- Free web APIs that need no key are best. If a key is unavoidable, read it from os.environ and raise ToolFailed
  telling the user which variable to add to the .env file when it's missing.
- Every network call has timeout=10. Import Windows-only modules (winreg, ctypes.windll, pycaw) inside the function,
  so the module imports on any OS.
- No code runs at import time except imports, constants and definitions. Never eval/exec, never read Jarvis's
  secrets (.env, secrets/, API keys, browser data), never install packages.
- If the tool changes anything on the PC or outside it (writes/moves/deletes files, changes settings, starts or
  stops programs, sends messages, posts data), add dangerous=True to @tool: Jarvis then asks before each run.
- Keep it small and focused: under 150 lines.

Then the tests, saved as tests/generated/test_{module_hint}.py:

```python
# file: test_tool.py
import unittest
from unittest import mock

from jarvis.tools.builtin import ToolFailed
from jarvis.tools.generated import {module_hint} as mod


class Tests(unittest.TestCase):
    def test_converts(self):
        reply = mock.Mock(status_code=200)
        reply.json.return_value = {{"rates": {{"EUR": 0.5}}}}
        with mock.patch.object(mod.requests, "get", return_value=reply):
            self.assertIn("5.00 EUR", mod.convert_currency(10, "eur"))
```

Rules for the tests:
- unittest only, at least three test methods covering the normal case, a failure case and an edge case.
- They run offline in a sandbox that refuses network connections, starting programs, Windows API calls, the
  registry, and writing files outside a temporary folder: mock all of those (unittest.mock), use tempfile for files.
- They must pass on Windows and on Linux. Call the tool functions directly.

Reply with exactly two code blocks, the first starting with "# file: tool.py" and the second with
"# file: test_tool.py", then one line "SUMMARY: <what the tool does, one short spoken sentence>"."""

FIX = """That didn't pass. Fix it and reply with both complete files again in the same format.

{problems}"""


@dataclass
class Draft:
    tool_code: str = ""
    test_code: str = ""
    summary: str = ""
    existing: str = ""


@dataclass
class Review:
    problems: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    descriptions: dict[str, str] = field(default_factory=dict)


@dataclass
class Build:
    ok: bool
    module: str = ""
    tools: list[str] = field(default_factory=list)
    summary: str = ""
    risks: list[str] = field(default_factory=list)
    tests_run: int = 0
    attempts: int = 0
    message: str = ""
    existing: str = ""


# ---------------------------------------------------------------- parsing what the model wrote

def parse(reply: str) -> Draft:
    d = Draft()
    m = EXISTING.search(reply or "")
    if m and "```" not in reply:
        d.existing = m.group(1)
        return d
    blocks = re.findall(r"```(?:python|py)?[^\n]*\n(.*?)```", reply or "", re.S)
    for b in blocks:
        head = b.lstrip().split("\n", 1)[0].lower()
        if "test_tool.py" in head or (not d.test_code and d.tool_code and "unittest" in b):
            d.test_code = d.test_code or b
        elif "tool.py" in head or not d.tool_code:
            d.tool_code = d.tool_code or b
    s = re.search(r"^\s*SUMMARY:\s*(.+)$", reply or "", re.M)
    d.summary = s.group(1).strip() if s else ""
    return d


def module_name(text: str) -> str:
    words = [w for w in re.findall(r"[a-z]+", text.lower()) if w not in
             {"a", "an", "the", "to", "for", "of", "my", "me", "and", "can", "you", "please", "jarvis", "how",
              "what", "is", "it", "that", "this", "in", "on", "with", "tool", "build", "make", "write", "new"}]
    return "_".join(words[:3]) or "custom_tool"


# ---------------------------------------------------------------- static review

def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _tool_decorator(dec) -> ast.Call | None:
    if isinstance(dec, ast.Call) and _dotted(dec.func).split(".")[-1] == "tool":
        return dec
    return None


def _imports(tree, package: str) -> list[str]:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:                           # relative: resolve against the package
                base = package.split(".")
                base = base[:len(base) - node.level + 1] if node.level > 1 else base
                names.append(".".join(base + ([node.module] if node.module else [])))
            elif node.module:
                names.append(node.module)
    return names


def _check_common(tree, source: str, package: str, problems: list[str], where: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name.split(".")[-1] in FORBIDDEN_CALLS and name.split(".")[0] in ("", name.split(".")[-1], "builtins"):
                problems.append(f"{where}: {name}() is not allowed")
            if name in ("os.system", "os.popen") or name.startswith(("os.exec", "os.spawn")):
                problems.append(f"{where}: {name}() is not allowed; use subprocess.run with a list of arguments "
                                "and dangerous=True")
            if name.startswith("subprocess.") and any(k.arg == "shell" and getattr(k.value, "value", False) is True
                                                     for k in node.keywords):
                problems.append(f"{where}: subprocess with shell=True is not allowed")
            if name.endswith(("pip.main",)) or (name.startswith("subprocess.") and "pip" in ast.unparse(node)):
                problems.append(f"{where}: installing packages is not allowed")
    for mod in _imports(tree, package):
        top = mod.split(".")[0]
        if top in FORBIDDEN_MODULES:
            problems.append(f"{where}: importing {mod} is not allowed")
        if top == "jarvis" and mod not in JARVIS_ALLOWED and not mod.startswith("jarvis.tools.generated"):
            problems.append(f"{where}: importing {mod} is not allowed (only the tool registry, ToolFailed and config)")
    if SECRET_PATTERNS.search(source):
        problems.append(f"{where}: it refers to Jarvis's secrets or browser data")


def review(tool_code: str, test_code: str, module: str, taken: set[str]) -> Review:
    """Parse and check generated code before anything of it runs."""
    r = Review()
    try:
        tree = ast.parse(tool_code)
    except SyntaxError as e:
        r.problems.append(f"tool.py doesn't parse: {e.msg} on line {e.lineno}")
        return r
    package = "jarvis.tools.generated"
    _check_common(tree, tool_code, package, r.problems, "tool.py")
    for node in tree.body:                       # nothing may run at import time
        ok = isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                               ast.Assign, ast.AnnAssign)) or (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        if isinstance(node, ast.Try):
            ok = all(isinstance(n, (ast.Import, ast.ImportFrom, ast.Assign, ast.Pass)) for n in node.body)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            ok = not any(isinstance(n, ast.Call) and not _plain_constructor(n) for n in ast.walk(node.value))
        if not ok:
            r.problems.append(f"tool.py line {node.lineno}: code runs when the module is imported; move it into "
                              "the tool function")
    for mod in _imports(tree, package):
        why = RISKY_MODULES.get(mod.split(".")[0])
        if why:
            r.risks.append(why)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = (_dotted(node.func.value).split(".")[-1] or "").lower()
            why = RISKY_CALLS.get((node.func.attr, owner)) or RISKY_CALLS.get((node.func.attr, None))
            if why:
                r.risks.append(why)
        if isinstance(node, ast.Call) and _dotted(node.func) == "open" and len(node.args) > 1:
            mode = getattr(node.args[1], "value", "")
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                r.risks.append("writes files")
        if isinstance(node, ast.Call) and _dotted(node.func) == "open":
            for k in node.keywords:
                if k.arg == "mode" and isinstance(getattr(k.value, "value", None), str) and \
                        any(c in k.value.value for c in "wax+"):
                    r.risks.append("writes files")
    r.risks = list(dict.fromkeys(r.risks))
    decorated = 0
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            call = _tool_decorator(dec)
            if not call:
                continue
            decorated += 1
            kw = {k.arg: k.value for k in call.keywords}
            name = getattr(kw.get("name"), "value", None) or node.name
            if not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", name):
                r.problems.append(f"tool name {name!r} must be snake_case, 3-40 characters")
            if name in taken:
                r.problems.append(f"a tool called {name} already exists; pick another name")
            r.tools.append(name)
            if call.args and isinstance(call.args[0], ast.Constant):
                r.descriptions[name] = str(call.args[0].value)
            dangerous = getattr(kw.get("dangerous"), "value", False) is True
            if r.risks and not dangerous:
                r.problems.append(f"{name}: the module {', '.join(r.risks)}, so its @tool needs dangerous=True "
                                  "(or don't do that)")
    if not decorated:
        r.problems.append("tool.py has no @tool function")
    if len(tool_code.splitlines()) > 300:
        r.problems.append("tool.py is too long; keep it under 150 lines")
    # the tests
    try:
        ttree = ast.parse(test_code)
    except SyntaxError as e:
        r.problems.append(f"test_tool.py doesn't parse: {e.msg} on line {e.lineno}")
        return r
    _check_common(ttree, test_code, "tests.generated", r.problems, "test_tool.py")
    tests = [n for n in ast.walk(ttree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
    if len(tests) < 3:
        r.problems.append("test_tool.py needs at least three test methods")
    if f"jarvis.tools.generated import {module}" not in test_code and \
            f"jarvis.tools.generated.{module}" not in test_code:
        r.problems.append(f"test_tool.py must import the tool as: from jarvis.tools.generated import {module} as mod")
    return r


def _plain_constructor(node: ast.Call) -> bool:
    """re.compile(...), frozenset(...), dict(...) at module level are fine."""
    return _dotted(node.func) in {"re.compile", "frozenset", "set", "dict", "tuple", "list", "Path",
                                  "pathlib.Path", "dataclass", "field", "namedtuple", "collections.namedtuple",
                                  "logging.getLogger", "object", "threading.Lock", "threading.RLock",
                                  "os.environ.get", "timedelta", "datetime.timedelta", "dt.timedelta"}


# ---------------------------------------------------------------- the builder

class Toolsmith:
    def __init__(self, cfg: Section, toolbox, ask_model: Callable[[str, str], str] | None = None):
        t = cfg.get("toolsmith") or {}
        self.cfg = cfg
        self.toolbox = toolbox
        self.enabled = t.get("enabled", True)
        self.max_attempts = int(t.get("max_attempts", 3))
        self.test_timeout = float(t.get("test_timeout", 120))
        self.gaps_path = resolve_path(t.get("gaps_file", "data/tool_gaps.jsonl"))
        self.git_commit = bool(t.get("git_commit", False))
        self._ask_model = ask_model           # (system, prompt) -> reply; tests pass a fake

    # -- the coding model
    def _model(self):
        """A fresh conversation with the coding model: (send(prompt) -> reply text, label)."""
        if self._ask_model:
            return (lambda prompt: self._ask_model(SYSTEM, prompt)), "test model"
        from .llm import create_provider

        t = self.cfg.get("toolsmith") or {}
        name = t.get("provider", "auto")
        if name == "auto":
            name = "claude" if any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")) \
                else self.cfg.llm.get("provider", "ollama")
        llm = Section(json.loads(json.dumps(dict(self.cfg.llm))))
        opts = dict((llm.get("providers") or {}).get(name) or {})
        if t.get("model"):
            opts["model"] = t["model"]
        if name == "claude":
            opts["effort"] = t.get("effort", "high")
            opts["max_tokens"] = max(int(opts.get("max_tokens", 16000)), 16000)
        else:
            opts["num_ctx"] = max(int(opts.get("num_ctx", 8192) or 8192), int(t.get("num_ctx", 16384)))
            opts["max_tool_rounds"] = None
        llm.setdefault("providers", {})[name] = {k: v for k, v in opts.items() if k in _accepted(name)}
        llm["temperature"] = 0.2
        llm["max_history_messages"] = 40
        p = create_provider(name, llm, SYSTEM, [])
        if name == "claude":
            # a plain conversation without tools (the provider's step always sends its tool list)
            messages: list[dict] = []

            def send(prompt: str) -> str:
                messages.append({"role": "user", "content": prompt})
                r = p._create(model=p.model, max_tokens=p.max_tokens, system=SYSTEM, messages=messages,
                              output_config={"effort": p.effort})
                text = "\n".join(b.text for b in r.content if b.type == "text")
                messages.append({"role": "assistant", "content": text})
                return text
        else:
            def send(prompt: str) -> str:
                p.add_user(prompt)
                # streamed, so a slow local model writing a long file doesn't hit the request timeout
                return p.step(use_tools=False, on_text=lambda s: None).text

        return send, p.label

    # -- gaps
    def log_gap(self, request: str, reply: str):
        try:
            self.gaps_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.gaps_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), "request": request, "reply": reply[:300]},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass

    def gaps(self, limit: int = 10) -> list[dict]:
        try:
            rows = [json.loads(line) for line in self.gaps_path.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, ValueError):
            return []
        return rows[::-1][:limit]

    # -- build
    def build(self, request: str, progress: Callable[[str], None] | None = None) -> Build:
        """Write, review and test a tool for the request. Nothing is switched on here (see install)."""
        say = progress or (lambda s: None)
        module = self._free_module(module_name(request))
        taken = set(self.toolbox.tools) | {n for n in _all_registered()}
        existing = "\n".join(f"- {n}" for n in sorted(self.toolbox.tools))
        try:
            send, label = self._model()
        except Exception as e:
            return Build(False, message=f"I couldn't reach a model to write code with: {e}")
        prompt = GUIDE.format(request=request, existing=existing, module_hint=module)
        problems = ""
        tool_path, test_path = TOOLS_DIR / f"{module}.py", TESTS_DIR / f"test_{module}.py"
        for attempt in range(1, self.max_attempts + 1):
            log.info("toolsmith: attempt %d for %r with %s", attempt, request, label)
            try:
                reply = send(prompt if attempt == 1 else FIX.format(problems=problems))
            except Exception as e:
                self._discard(tool_path, test_path)
                return Build(False, module=module, attempts=attempt, message=f"The coding model failed: {e}")
            draft = parse(reply)
            if draft.existing:
                if draft.existing in self.toolbox.tools:
                    return Build(False, existing=draft.existing, attempts=attempt,
                                 message=f"I already have a tool for that: {draft.existing.replace('_', ' ')}.")
                problems = f"There is no existing tool called {draft.existing}. Write the new tool."
                continue
            if not draft.tool_code or not draft.test_code:
                problems = "Your reply needs two code blocks: '# file: tool.py' and '# file: test_tool.py'."
                continue
            rev = review(draft.tool_code, draft.test_code, module, taken)
            if rev.problems:
                problems = "Review problems:\n- " + "\n- ".join(rev.problems)
                log.info("toolsmith: review failed: %s", rev.problems)
                continue
            self._write(tool_path, draft.tool_code, request, label)
            self._write(test_path, draft.test_code, request, label)
            if attempt == 1:
                say("Testing it.")
            ok, output, ran = self.run_tests(module)
            if ok:
                return Build(True, module=module, tools=rev.tools, summary=draft.summary or
                             rev.descriptions.get(rev.tools[0], ""), risks=rev.risks, tests_run=ran,
                             attempts=attempt)
            problems = "The tests failed:\n" + _tail(output, 3500)
            log.info("toolsmith: tests failed:\n%s", _tail(output, 1500))
        self._discard(tool_path, test_path)
        self.log_gap(request, f"tool build failed after {self.max_attempts} attempts")
        return Build(False, module=module, attempts=self.max_attempts,
                     message=f"I tried {self.max_attempts} times but couldn't get a working tool for that. "
                             f"The last problem was: {_first_line(problems)}")

    def run_tests(self, module: str) -> tuple[bool, str, int]:
        """Run tests/generated/test_<module>.py in the sandbox. (passed, output, tests run)"""
        root = tempfile.mkdtemp(prefix="jarvis-tool-test-")
        try:
            proc = subprocess.run([sys.executable, "-X", "utf8", "-m", "jarvis.tool_sandbox",
                                   f"tests.generated.test_{module}", root],
                                  cwd=str(ROOT), env=sandbox_env(root), capture_output=True, text=True,
                                  timeout=self.test_timeout, encoding="utf-8", errors="replace",
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = (proc.stdout or "") + (proc.stderr or "")
            m = re.search(r"Ran (\d+) tests?", out)
            return proc.returncode == 0, out, int(m.group(1)) if m else 0
        except subprocess.TimeoutExpired:
            return False, f"The tests took longer than {self.test_timeout:.0f} seconds (something hangs; mock " \
                          "network calls and anything that waits).", 0
        finally:
            shutil.rmtree(root, ignore_errors=True)

    # -- switching it on
    def install(self, build: Build) -> str:
        generated.approve(build.module, build.tools, {"risks": build.risks, "summary": build.summary},
                          self.cfg)
        try:
            generated.import_module(build.module, fresh=True)
        except Exception as e:
            generated.revoke(build.module, self.cfg)
            return f"The tool didn't load: {e}"
        self.toolbox.add(build.tools)
        if self.git_commit:
            self._commit(build)
        return ""

    def discard(self, build: Build):
        self._discard(TOOLS_DIR / f"{build.module}.py", TESTS_DIR / f"test_{build.module}.py")

    def remove(self, module: str, delete_files: bool = False) -> list[str]:
        entry = generated.read_store(self.cfg).get("approved", {}).get(module) or {}
        names = entry.get("tools") or []
        generated.revoke(module, self.cfg)
        self.toolbox.remove(names)
        if delete_files:
            self._discard(TOOLS_DIR / f"{module}.py", TESTS_DIR / f"test_{module}.py")
        return names

    def listing(self) -> list[dict]:
        approved = generated.read_store(self.cfg).get("approved", {})
        out = []
        for m in generated.modules():
            entry = approved.get(m) or {}
            out.append({"module": m, "status": generated.status(m, self.cfg), "tools": entry.get("tools") or [],
                        "summary": entry.get("summary", "")})
        return out

    def tools_in(self, module: str) -> list[str]:
        """The tool names a module registers (read from its code, without importing it)."""
        try:
            tree = ast.parse((TOOLS_DIR / f"{module}.py").read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            return []
        names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    call = _tool_decorator(dec)
                    if call:
                        kw = {k.arg: k.value for k in call.keywords}
                        names.append(getattr(kw.get("name"), "value", None) or node.name)
        return names

    # -- helpers
    def _free_module(self, base: str) -> str:
        name, i = base, 2
        while (TOOLS_DIR / f"{name}.py").exists() or f"jarvis.tools.generated.{name}" in sys.modules:
            name, i = f"{base}_{i}", i + 1
        return name

    @staticmethod
    def _write(path: Path, code: str, request: str, label: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        body = code.strip("\n")
        body = re.sub(r"^\s*# file: [\w.]+\s*\n", "", body, count=1)
        header = f"# Written by Jarvis's tool builder ({label}) for: {request[:120]!r}\n"
        path.write_text(header + body + "\n", encoding="utf-8")

    @staticmethod
    def _discard(*paths: Path):
        for p in paths:
            try:
                p.unlink()
            except OSError:
                pass

    def _commit(self, build: Build):
        files = [f"jarvis/tools/generated/{build.module}.py", f"tests/generated/test_{build.module}.py"]
        try:
            subprocess.run(["git", "add", *files], cwd=ROOT, check=True, capture_output=True, timeout=20)
            subprocess.run(["git", "commit", "-m", f"Jarvis built a tool: {', '.join(build.tools)}\n\n{build.summary}",
                            "--", *files], cwd=ROOT, check=True, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("toolsmith: couldn't commit %s: %s", build.module, e)


def _accepted(provider: str) -> set[str]:
    """The settings a provider class takes, so options meant for another provider don't break it."""
    import importlib
    import inspect

    where = {"ollama": ("jarvis.llm.ollama", "OllamaProvider"),
             "claude": ("jarvis.llm.anthropic_claude", "ClaudeProvider"),
             "lmstudio": ("jarvis.llm.openai_compat", "LMStudioProvider"),
             "openai_compat": ("jarvis.llm.openai_compat", "OpenAICompatProvider")}.get(provider)
    if not where:
        return set()
    klass = getattr(importlib.import_module(where[0]), where[1])
    names = set()
    for k in klass.__mro__:
        if "__init__" in k.__dict__:
            names |= set(inspect.signature(k.__init__).parameters)
    return names - {"self", "kw", "kwargs", "system_prompt", "tools", "temperature", "max_history"}


def _all_registered() -> set[str]:
    from .tools.registry import _TOOLS

    return set(_TOOLS)


def _tail(text: str, n: int) -> str:
    return text if len(text) <= n else "..." + text[-n:]


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip(" -")
        if line and not line.endswith(":"):
            return line[:200]
    return "unknown"
