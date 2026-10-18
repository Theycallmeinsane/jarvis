"""`python -m jarvis --check`: load every component once and report what works."""
from __future__ import annotations

import time
import traceback

import numpy as np

from .config import Section


def _row(ok: bool, name: str, detail: str):
    mark = "\033[92m OK \033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"[{mark}] {name:<12} {detail}")


def run_wake_test(cfg: Section) -> int:
    """Live meter: mic level + wake-word score, to pick a good wake_word.threshold. Ctrl+C to stop."""
    from .audio import Microphone
    from .wakeword import WakeWordDetector

    ww = cfg.wake_word
    det = WakeWordDetector(ww.get("model", "hey_jarvis"), ww.get("threshold", 0.5), cooldown_seconds=0)
    mic = Microphone(cfg.audio.get("input_device"))
    mic.start()
    thr = float(ww.get("threshold", 0.5))
    print(f"Say \"Hey Jarvis\" a few times, near and far. Current threshold {thr:.2f}. Ctrl+C to stop.\n",
          flush=True)
    best, hits = 0.0, 0
    try:
        while True:
            frame = mic.read(timeout=1)
            if frame is None:
                continue
            scores = det.model.predict(frame)
            score = max(scores.values()) if scores else 0.0
            level = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            if score > 0.05:
                best = max(best, score)
                bar = "#" * int(score * 40)
                fired = score >= thr
                hits += fired
                print(f"score {score:.2f} {bar:<40} {'WAKE' if fired else ''}  (mic level {level:.0f})", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        mic.stop()
    print(f"\nBest score {best:.2f}, {hits} frames over the threshold.")
    print("If real tries peak below the threshold, lower wake_word.threshold a little; "
          "if TV or chatter crosses it, raise it.")
    return 0


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

    def runtime():
        from .hardware import onnxruntime_status

        ok, text = onnxruntime_status()
        if not ok:
            raise RuntimeError(text)
        return text

    def voice_check():
        from .tts import create_tts, resolve_preset

        engine = create_tts(cfg.tts, tts)
        audio, sr = engine.synthesize("Systems check complete.")
        wanted = tts or cfg.tts.get("voice") or "friday"
        wanted = resolve_preset(wanted) or wanted
        if engine.preset == "windows" and wanted != "windows":
            raise RuntimeError(f"only the Windows fallback voice works ({wanted} failed; see the log)")
        note = "" if engine.preset == wanted else f" (fallback for {wanted})"
        return f"{engine.preset}{note}, {len(audio) / sr:.1f}s of audio"

    from .hardware import describe, detect

    print("Jarvis systems check\n")
    print(f"[ -- ] {'hardware':<12} {describe(detect())}")
    check("runtime", runtime)
    check("microphone", mic)
    check("wake word", wake)
    check("speech->text", stt)
    check("voice", voice_check)
    check("brain", brain)

    # optional extras: informational, never counted as failures
    import os

    from .routines import routines_from
    from .tools.google_tools import _paths

    def info(name: str, text: str):
        print(f"[ -- ] {name:<12} {text}")

    print()
    has_key = any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
    info("claude", "API key found" if has_key else "no ANTHROPIC_API_KEY in .env (optional)")
    creds, token = _paths(cfg)
    info("google", "connected" if token.exists() else
         ("credentials found, run --google-login" if creds.exists() else "not set up (optional): jarvis.bat --google-setup"))
    from .tools.github_tools import status_line

    info("github", status_line())
    info("hotkey", cfg.get("hotkey") or "off")
    info("routines", ", ".join(routines_from(cfg)) or "none")
    print(f"\n{'All systems go.' if not failures else f'{failures} problem(s) found.'}")
    return 1 if failures else 0
