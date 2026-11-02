"""Runs the tests of a tool Jarvis wrote, before anyone has approved that tool.

    python -m jarvis.tool_sandbox tests.generated.test_<name> <sandbox folder>

The tests run in their own process with a Python audit hook that refuses anything that could change the PC or
reach the outside world: writing, moving or deleting files outside the sandbox folder, starting programs, network
connections, registry changes and raw Windows API calls (ctypes). Home, AppData and temp all point into the sandbox
folder, so "~/Documents" in a test is a throwaway copy. A test that needs one of those must mock it, and the
refusal message says so, which is what the tool builder sends back to the model to fix.

This guards against mistakes in generated code (a test that really deletes files, a tool that really calls an
API). It is not a security boundary against code written to escape it; the user's approval before a tool is
switched on is that.
"""
from __future__ import annotations

import os
import sys

WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

# events refused outright
BLOCKED = {
    "subprocess.Popen": "starting programs",
    "os.system": "running shell commands",
    "os.exec": "starting programs",
    "os.posix_spawn": "starting programs",
    "os.spawn": "starting programs",
    "os.startfile": "opening files or programs",
    "os.fork": "starting processes",
    "os.forkpty": "starting processes",
    "os.kill": "stopping processes",
    "os.killpg": "stopping processes",
    "ctypes.call_function": "calling Windows APIs directly",
    "ctypes.dlopen": "loading system libraries",
    "socket.connect": "network connections",
    "socket.bind": "opening network ports",
    "webbrowser.open": "opening the browser",
    "winreg.CreateKey": "changing the registry",
    "winreg.DeleteKey": "changing the registry",
    "winreg.DeleteValue": "changing the registry",
    "winreg.SetValue": "changing the registry",
    "winreg.SaveKey": "changing the registry",
    "winreg.LoadKey": "changing the registry",
}
# events allowed only for paths inside the sandbox folder (the first argument is the path)
PATH_EVENTS = {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.utime",
               "shutil.rmtree", "shutil.copyfile", "shutil.copytree", "shutil.move", "os.symlink", "os.link"}


def _inside(path, root: str) -> bool:
    if isinstance(path, int) or path is None:
        return True                       # an already-open file descriptor
    try:
        p = os.path.normcase(os.path.realpath(os.fsdecode(path)))
    except (TypeError, ValueError):
        return False
    root = os.path.normcase(root)              # Windows paths ignore case
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def install(root: str):
    root = os.path.realpath(root)

    def refuse(what: str, detail=""):
        raise PermissionError(f"Blocked in the tool test sandbox: {what}{f' ({detail})' if detail else ''}. "
                              "Tests must not do this for real; mock it with unittest.mock instead.")

    def hook(event: str, args: tuple):
        if event in BLOCKED or event.startswith(("os.exec", "os.spawn")):
            if event == "socket.connect" and args and _local(args[1] if len(args) > 1 else None):
                return
            refuse(BLOCKED.get(event, "starting programs"), event)
        if event == "open" and args:
            path, mode, flags = (list(args) + [None, None, 0])[:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool((flags or 0) & WRITE_FLAGS)
            if writing and not _inside(path, root):
                refuse("writing files outside the test folder", os.fsdecode(path) if path is not None else "")
        elif event == "os.rename" and args:
            if not (_inside(args[0], root) and _inside(args[1], root)):
                refuse("moving files outside the test folder", f"{args[0]} -> {args[1]}")
        elif event in PATH_EVENTS and args:
            if event in ("shutil.copyfile", "shutil.copytree"):        # reading from anywhere is fine
                targets = list(args[1:2])
            elif event in ("shutil.move", "os.symlink", "os.link"):
                targets = list(args[:2])
            else:
                targets = [args[0]]
            if not all(_inside(t, root) for t in targets):
                refuse("changing files outside the test folder", f"{event} {args[0]}")

    sys.addaudithook(hook)


def _local(address) -> bool:
    try:
        host = address[0] if isinstance(address, tuple) else address
        return host in ("127.0.0.1", "::1", "localhost")
    except Exception:
        return False


def sandbox_env(root: str) -> dict:
    """The environment for the test process: no secrets, and every per-user folder inside the sandbox."""
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL",
                                                "AUTH", "COOKIE", "SESSION"))}
    home = os.path.join(root, "home")
    for k in ("HOME", "USERPROFILE"):
        env[k] = home
    for k in ("APPDATA", "LOCALAPPDATA"):
        env[k] = os.path.join(home, "AppData", "Roaming" if k == "APPDATA" else "Local")
    for k in ("TEMP", "TMP", "TMPDIR"):
        env[k] = os.path.join(root, "tmp")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["JARVIS_TOOL_SANDBOX"] = "1"
    return env


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m jarvis.tool_sandbox <test module> <sandbox folder>", file=sys.stderr)
        return 2
    module, root = argv[0], os.path.realpath(argv[1])
    for sub in ("tmp", os.path.join("home", "AppData", "Roaming"), os.path.join("home", "AppData", "Local"),
                os.path.join("home", "Documents"), os.path.join("home", "Desktop"), os.path.join("home", "Downloads")):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    sys.dont_write_bytecode = True
    import tempfile
    import unittest

    tempfile.tempdir = os.path.join(root, "tmp")
    from jarvis.config import ROOT

    sys.path.insert(0, str(ROOT))
    # everything the tests need from Jarvis is imported before the hook goes in (imports themselves are harmless)
    import jarvis.tools.registry  # noqa: F401
    import jarvis.tools.builtin  # noqa: F401

    os.chdir(root)
    install(root)
    program = unittest.main(module=None, argv=["sandbox", module, "-v"], exit=False)
    result = program.result
    return 0 if result.wasSuccessful() and result.testsRun > 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
