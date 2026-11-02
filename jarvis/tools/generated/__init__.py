"""Tools Jarvis wrote itself (see jarvis/toolsmith.py).

Each tool is a module here with its tests in tests/generated/. The code lives in the repo, but whether a tool is
switched on is decided on each PC: data/toolsmith.json keeps the tools the user approved, with a fingerprint
(SHA-256) of the code they approved. A tool whose file has changed since then, or that came from somewhere else
(a git pull), stays off until the user approves it again ("enable the <name> tool": its tests are run first).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import sys
import threading
import time
from pathlib import Path

from ...config import resolve_path

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
_lock = threading.Lock()


def store_path(cfg=None) -> Path:
    return resolve_path(((cfg or {}).get("toolsmith") or {}).get("approvals_file", "data/toolsmith.json"))


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def read_store(cfg=None) -> dict:
    try:
        data = json.loads(store_path(cfg).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_store(data: dict, cfg=None):
    path = store_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def modules() -> list[str]:
    """Every generated tool module in the folder, approved or not."""
    return sorted(p.stem for p in HERE.glob("*.py") if not p.name.startswith("_"))


def approve(module: str, tools: list[str], info: dict | None = None, cfg=None):
    with _lock:
        data = read_store(cfg)
        data.setdefault("approved", {})[module] = {
            "sha256": fingerprint(HERE / f"{module}.py"), "tools": tools,
            "approved": time.strftime("%Y-%m-%d %H:%M"), **(info or {})}
        write_store(data, cfg)


def revoke(module: str, cfg=None) -> bool:
    with _lock:
        data = read_store(cfg)
        found = data.get("approved", {}).pop(module, None) is not None
        if found:
            write_store(data, cfg)
        return found


def status(module: str, cfg=None) -> str:
    """'on', 'changed' (approved, but the file differs from what was approved), or 'off'."""
    entry = read_store(cfg).get("approved", {}).get(module)
    path = HERE / f"{module}.py"
    if not entry or not path.exists():
        return "off"
    return "on" if entry.get("sha256") == fingerprint(path) else "changed"


def import_module(module: str, fresh: bool = False):
    """Import a tool module (fresh: re-read it from disk, after it was rebuilt)."""
    name = f"{__name__}.{module}"
    if fresh and name in sys.modules:
        return importlib.reload(sys.modules[name])
    return importlib.import_module(name)


def load_approved(cfg=None) -> set[str]:
    """Import the approved, unchanged tool modules; returns the names of the tools they register."""
    if not ((cfg or {}).get("toolsmith") or {}).get("load_generated", True):
        return set()
    names: set[str] = set()
    approved = read_store(cfg).get("approved", {})
    for module in modules():
        state = status(module, cfg)
        if state == "off":
            log.debug("generated tool %s is waiting for approval", module)
            continue
        if state == "changed":
            log.warning("generated tool %s changed since you approved it; it stays off until you approve it "
                        "again (say 'enable the %s tool')", module, module.replace("_", " "))
            continue
        try:
            import_module(module)
        except Exception as e:
            log.warning("generated tool %s didn't load: %s", module, e)
            continue
        names.update(approved[module].get("tools") or [])
    return names
