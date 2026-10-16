"""`python -m jarvis --check`: load every component once and report what works."""
from __future__ import annotations

import time
import traceback

import numpy as np

from .config import Section


def _row(ok: bool, name: str, detail: str):
    mark = "\033[92m OK \033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"[{mark}] {name:<12} {detail}")


def run_checks(cfg: Section, llm: str | None = None, tts: str | None = None) -> int:
    failures = 0

    def check(name, fn):
        nonlocal failures
        t0 = time.perf_counter()
        try:
            detail = fn()
            _row(True, name, f"{detail}  ({time.perf_counter() - t0:.1f}s)")
            return True
        except Exception as e:
            failures += 1
            _row(False, name, f"{e.__class__.__name__}: {e}")
            if cfg.get("debug"):
                traceback.print_exc()
            return False

    def mic():
        import sounddevice as sd

        from .audio import find_device

        dev = find_device(cfg.audio.get("input_device"), "input")
        info = sd.query_devices(dev if dev is not None else sd.default.device[0])
        rec = sd.rec(8000, samplerate=16000, channels=1, dtype="int16", device=dev)
        sd.wait()
        level = float(np.abs(rec).mean())
        return f"{info['name']} (level {level:.0f})"

    def wake():
        from .wakeword import WakeWordDetector

        d = WakeWordDetector(cfg.wake_word.get("model", "hey_jarvis"), cfg.wake_word.get("threshold", 0.5))
        d.process(np.zeros(1280, dtype=np.int16))
        return ", ".join(d.names)

    def stt():
        from .stt import Transcriber

        s = cfg.stt
        t = Transcriber(s.get("model", "small.en"), s.get("device", "auto"), s.get("compute_type", "auto"),
                        s.get("language", "en"), s.get("download_root", "models/whisper"))
        return f"{s.get('model')} on {t.device}"

    def voice():
        from .tts import create_tts

        engine = create_tts(cfg.tts, tts)
        audio, sr = engine.synthesize("Systems check complete.")
        return f"{engine.__class__.__name__}, {len(audio) / sr:.1f}s of audio"

    def brain():
        from .llm import create_provider
        from .tools import ToolBox

        name = llm or cfg.llm.provider
        p = create_provider(name, cfg.llm, "You are a test. Reply with one word.", ToolBox(cfg).schemas())
        problem = p.check()
        if problem:
            raise RuntimeError(problem)
        p.add_user("Say ready.")
        turn = p.step()
        return f"{p.label} replied: {turn.text.strip()[:40]!r}"

    print("Jarvis systems check\n")
    check("microphone", mic)
    check("wake word", wake)
    check("speech->text", stt)
    check("voice", voice)
    check("brain", brain)
    print(f"\n{'All systems go.' if not failures else f'{failures} problem(s) found.'}")
    return 1 if failures else 0
