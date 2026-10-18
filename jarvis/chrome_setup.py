"""Link Chrome to Jarvis: `jarvis.bat --chrome-setup`.

Chrome only installs an unpacked extension when you click "Load unpacked" yourself, so this opens the right
page and folder, tells you the three clicks, and waits until the extension connects.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .config import ROOT

EXTENSION_DIR = ROOT / "extension" / "chrome"


def chrome_exe() -> str | None:
    env = os.environ
    for base in (env.get("ProgramFiles"), env.get("ProgramFiles(x86)"), env.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if p.exists():
                return str(p)
    return None


def _copy(text: str):
    try:
        subprocess.run("clip", input=text, text=True, shell=True, timeout=5)
    except Exception:  # noqa: BLE001  (clipboard is only a convenience)
        pass


def run(cfg, wait_minutes: int = 5) -> int:
    from . import chrome_bridge

    bridge = chrome_bridge.get(cfg)
    print("\n=== Link Chrome to Jarvis ===\n")
    if bridge.connected:
        print("Chrome is already linked. Nothing to do.")
        return 0
    exe = chrome_exe()
    folder = str(EXTENSION_DIR)
    print("Chrome will open its Extensions page. There:")
    print("  1. Turn on 'Developer mode' (switch at the top right).")
    print("  2. Click 'Load unpacked'.")
    print(f"  3. Choose this folder (its path is on your clipboard, paste it into the address bar):\n     {folder}")
    print("The extension only talks to Jarvis on this PC. To remove it later, click Remove on the same page.\n")
    _copy(folder)
    if exe:
        subprocess.Popen([exe, "chrome://extensions"])
    else:
        print("I couldn't find Chrome. Open chrome://extensions in it yourself.")
    if not bridge.listening:
        print("Jarvis is already running, so the extension links to it directly. When it's linked, the\n"
              "extension's icon has no 'off' badge. Then try: \"Hey Jarvis, what tabs do I have open?\"")
        return 0
    deadline = time.time() + wait_minutes * 60
    print(f"Waiting for the extension to connect (up to {wait_minutes} minutes)...")
    while time.time() < deadline:
        if bridge.connected:
            print("\nLinked. Try: \"Hey Jarvis, what tabs do I have open?\" or \"ask Claude what's new in Python 3.13\".")
            return 0
        time.sleep(1)
    print("It didn't connect. Click the Jarvis Control extension icon and press Reconnect, then run this again.")
    return 1
