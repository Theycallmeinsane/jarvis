"""python -m jarvis [--tray] [--text] [--llm claude] [--voice friday] [--list-devices] [--check] [--setup] [--google-login]"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jarvis", description="Local voice assistant for Windows")
    ap.add_argument("--tray", action="store_true", help="run in the system tray (no console)")
    ap.add_argument("--text", action="store_true", help="type instead of talk (no mic needed)")
    ap.add_argument("--speak", action="store_true", help="in --text mode, also speak replies")
    ap.add_argument("--llm", help="brain to use: ollama | lmstudio | openai_compat | claude")
    ap.add_argument("--voice", "--tts", dest="tts",
                    help="voice: friday | amy | jarvis | friday-hd | heart-hd | bella-hd | jarvis-hd | windows")
    ap.add_argument("--setup", action="store_true", help="first-run wizard: voice, name, mic, brain")
    ap.add_argument("--yes", action="store_true", help="with --setup: accept every recommendation, no questions")
    ap.add_argument("--config", help="path to a config.yaml")
    ap.add_argument("--list-devices", action="store_true", help="show microphones and speakers")
    ap.add_argument("--check", action="store_true", help="test every component and exit")
    ap.add_argument("--wake-test", action="store_true", help="live wake-word score meter for tuning the threshold")
    ap.add_argument("--google-login", action="store_true", help="connect Google Calendar + Gmail (opens browser)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    from .config import ROOT, load_config

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    has_console = sys.stdout is not None
    if has_console:
        os.system("")  # enable ANSI colours in the Windows console
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    handlers: list[logging.Handler] = []
    if has_console:
        handlers.append(logging.StreamHandler())
    if args.tray or not has_console:
        (ROOT / "data").mkdir(exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            ROOT / "data" / "jarvis.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "httpcore2", "urllib3", "faster_whisper", "comtypes", "phonemizer",
                  "googleapiclient", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = load_config(args.config)

    if args.list_devices:
        from .audio import list_devices

        print(list_devices())
        return
    if args.google_login:
        from .tools.google_tools import login

        print(login(cfg))
        return
    if args.setup:
        from .setup_wizard import run_wizard

        run_wizard(answers={} if args.yes else None)
        return
    if args.wake_test:
        from .diagnostics import run_wake_test

        sys.exit(run_wake_test(cfg))
    if args.check:
        from .diagnostics import run_checks

        sys.exit(run_checks(cfg, args.llm, args.tts))
    if args.text:
        from .app import run_text_mode

        run_text_mode(cfg, args.llm, args.tts, speak=args.speak)
        return
    if args.tray:
        from .tray import run_tray

        run_tray(cfg, args.llm, args.tts)
        return

    from .app import VoiceAssistant

    VoiceAssistant(cfg, llm=args.llm, tts=args.tts).run()


if __name__ == "__main__":
    main()
