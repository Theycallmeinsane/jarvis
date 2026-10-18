"""Guided Google setup: `jarvis.bat --google-setup`.

Google only lets the account owner create an OAuth client, so this can't be fully automatic. It opens each
Google Cloud page in your browser in the right order, tells you exactly what to click, waits for the client
file to land in Downloads, moves it into secrets/, and runs the sign-in. About five minutes.
"""
from __future__ import annotations

import json
import shutil
import time
import webbrowser
from pathlib import Path

from .tools.google_tools import _paths, login

STEPS = [
    ("Create a Google Cloud project",
     "https://console.cloud.google.com/projectcreate",
     ["Project name: Jarvis (anything is fine). Click CREATE.",
      "Wait for the notification bell to say it's ready, and make sure the new project is selected at the top."]),
    ("Turn on the Calendar and Gmail APIs",
     "https://console.cloud.google.com/flows/enableapi?apiid=calendar-json.googleapis.com,gmail.googleapis.com",
     ["Check the project name at the top is your new one, then click NEXT and ENABLE."]),
    ("Set up the consent screen",
     "https://console.cloud.google.com/auth/overview",
     ["Click GET STARTED. App name: Jarvis. User support email: your email. NEXT.",
      "Audience: External. NEXT.  Contact information: your email. NEXT.",
      "Tick 'I agree…', then CONTINUE and CREATE."]),
    ("Allow your own account",
     "https://console.cloud.google.com/auth/audience",
     ["Under 'Test users' click + ADD USERS, enter the Gmail address Jarvis should use, SAVE.",
      "Optional but recommended: click PUBLISH APP and CONFIRM. Apps left in 'Testing' are signed out by",
      "Google every 7 days; a published personal app isn't (you'll see an 'unverified app' warning once)."]),
    ("Create the Desktop client and download it",
     "https://console.cloud.google.com/auth/clients",
     ["Click + CREATE CLIENT. Application type: Desktop app. Name: Jarvis. CREATE.",
      "In the window that appears click DOWNLOAD JSON (a file named client_secret_....json)."]),
]


def _downloads() -> Path:
    from .tools.files_ext import known_folders

    return known_folders()["downloads"]


def is_client_file(p: Path) -> bool:
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    client = data.get("installed") or {}
    return bool(client.get("client_id") and client.get("client_secret"))


def find_client_file(folder: Path, since: float) -> Path | None:
    """The newest client_secret*.json (or any OAuth Desktop client JSON) saved in `folder` after `since`."""
    try:
        cands = [p for p in folder.glob("*.json") if p.stat().st_mtime >= since - 5]
    except OSError:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return next((p for p in cands if is_client_file(p)), None)


def install_client_file(src: Path, cfg) -> Path:
    creds_file, _ = _paths(cfg)
    creds_file.parent.mkdir(parents=True, exist_ok=True)
    if creds_file.exists():
        creds_file.replace(creds_file.with_suffix(".old.json"))
    shutil.move(str(src), str(creds_file))
    return creds_file


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return ""


def run(cfg, client_file: str | None = None, wait_minutes: int = 15) -> int:
    creds_file, token_file = _paths(cfg)
    print("\n=== Connect Google Calendar + Gmail to Jarvis ===\n")
    if client_file:
        src = Path(client_file).expanduser()
        if not is_client_file(src):
            print(f"{src} isn't a Desktop OAuth client file (it needs an 'installed' client_id and secret).")
            return 1
        print(f"Using {src} -> {install_client_file(src, cfg)}")
    elif creds_file.exists() and is_client_file(creds_file):
        print(f"Found your OAuth client at {creds_file}; skipping the Google Cloud steps.")
    else:
        started = time.time()
        print("You'll click through 5 Google Cloud pages; this window tells you what to do on each.")
        print("Sign in with the Google account you want Jarvis to use. Press Enter to open each page,")
        print("or type 's' to skip a step you've already done.\n")
        for i, (title, url, notes) in enumerate(STEPS, 1):
            print(f"Step {i} of {len(STEPS)}: {title}")
            for n in notes:
                print(f"   {n}")
            if _ask("   [Enter] open the page, [s] skip: ") != "s":
                webbrowser.open(url)
                if i < len(STEPS):
                    _ask("   Press Enter when you've done this step... ")
            print()
        downloads = _downloads()
        print(f"Waiting for the downloaded client file in {downloads} (up to {wait_minutes} minutes)...")
        print("If you saved it somewhere else, run:  jarvis.bat --google-setup <path to the .json>")
        deadline = time.time() + wait_minutes * 60
        found = None
        while time.time() < deadline and not found:
            found = find_client_file(downloads, started)
            if not found:
                time.sleep(2)
        if not found:
            print("No client file arrived. Run this again when you have it.")
            return 1
        print(f"Got {found.name} -> {install_client_file(found, cfg)}")

    print("\nLast step: your browser opens a Google sign-in. Choose your account and allow access.")
    print("(If it says 'Google hasn't verified this app', click Advanced -> Go to Jarvis. It's your own app.)")
    try:
        print(login(cfg))
    except Exception as e:  # noqa: BLE001  any OAuth failure: explain and let them retry
        print(f"Sign-in didn't finish: {e}\nRun  jarvis.bat --google-login  to try again.")
        return 1
    print("\nDone. Restart Jarvis, then try: \"Hey Jarvis, what's on my calendar today?\" or \"any new emails?\"")
    return 0 if token_file.exists() else 1
