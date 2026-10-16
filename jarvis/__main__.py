"""python -m jarvis [--text] [--llm claude] [--tts kokoro] [--list-devices] [--check]"""
from __future__ import annotations

import argparse
import logging
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jarvis", description="Local voice assistant for Windows")
    ap.add_argument("--text", action="store_true", help="type instead of talk (no mic needed)")
    ap.add_argument("--speak", action="store_true", help="in --text mode, also speak replies")
    ap.add_argument("--llm", help="brain to use: ollama | lmstudio | openai_compat | claude")
    ap.add_argument("--tts", help="voice engine: piper | kokoro | sapi")
    ap.add_argument("--config", help="path to a config.yaml")
    ap.add_argument("--list-devices", action="store_true", help="show microphones and speakers")
    ap.add_argument("--check", action="store_true", help="test every component and exit")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    os.system("")  # enable ANSI colours in the Windows console
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "urllib3", "faster_whisper", "comtypes", "phonemizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from .config import load_config

    cfg = load_config(args.config)

    if args.list_devices:
        from .audio import list_devices

        print(list_devices())
        return
    if args.check:
        from .diagnostics import run_checks

        sys.exit(run_checks(cfg, args.llm, args.tts))
    if args.text:
        from .app import run_text_mode

        run_text_mode(cfg, args.llm, args.tts, speak=args.speak)
        return

    from .app import VoiceAssistant

    VoiceAssistant(cfg, llm=args.llm, tts=args.tts).run()


if __name__ == "__main__":
    main()
