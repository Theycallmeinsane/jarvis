"""End-to-end check of the local voice chain, no microphone or speakers needed:

    Piper TTS  ->  openWakeWord ("hey jarvis")  ->  faster-whisper (CPU)  ->  routed to the right tool

Run:  python scripts/voice_smoke.py   (used by GitHub Actions; downloads ~150 MB of models on first run)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def main() -> int:
    from scipy.signal import resample_poly

    from jarvis.app import WAKE_PREFIX
    from jarvis.config import load_config
    from jarvis.stt import Transcriber
    from jarvis.tools import ToolBox
    from jarvis.tts import PiperTTS, clean_for_speech
    from jarvis.wakeword import WakeWordDetector

    failures = 0

    def check(ok: bool, what: str):
        nonlocal failures
        print(("PASS " if ok else "FAIL ") + what, flush=True)
        failures += not ok

    cfg = load_config()
    t0 = time.perf_counter()
    tts = PiperTTS(**dict(cfg.tts.piper))
    audio, sr = tts.synthesize(clean_for_speech("Hey Jarvis, open Spotify."))
    check(len(audio) / sr > 1.0, f"Piper spoke {len(audio) / sr:.1f}s of audio in {time.perf_counter() - t0:.1f}s")

    a16 = resample_poly(audio, 16000, sr).astype(np.float32)
    pcm = (np.concatenate([np.zeros(8000), a16, np.zeros(16000)]) * 32767).astype(np.int16)
    det = WakeWordDetector("hey_jarvis", 0.5, cooldown_seconds=0)
    best = 0.0
    for i in range(0, len(pcm) - 1280, 1280):
        scores = det.model.predict(pcm[i:i + 1280])
        best = max(best, max(scores.values()))
    check(best >= 0.5, f"wake word detected (score {best:.2f})")

    # base.en on CPU: tiny.en is too rough ("open no-tad"); hints are the same ones Jarvis uses
    stt = Transcriber("base.en", "cpu", "int8", "en", str(ROOT / "models" / "whisper"),
                      hints=cfg.stt.get("hints"))
    text = stt.transcribe(a16)
    command = WAKE_PREFIX.sub("", text).strip()
    check("spotify" in text.lower(), f"Whisper heard {text!r}; command after wake word: {command!r}")

    box = ToolBox(cfg)
    open_app = box.get("open_app")
    check(open_app is not None and open_app.allowed_for(command), "command is allowed to reach open_app")
    check(not box.get("web_search").allowed_for(command), "command is NOT allowed to open a browser search")

    print(f"\n{'voice chain OK' if not failures else f'{failures} check(s) failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
