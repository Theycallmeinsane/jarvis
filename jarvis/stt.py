"""Speech-to-text with faster-whisper (local; CUDA when available, CPU otherwise)."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import numpy as np

from .config import resolve_path

log = logging.getLogger(__name__)


def _add_nvidia_dll_dirs():
    """On Windows, pip's nvidia-cublas/cudnn wheels put DLLs in site-packages/nvidia/*/bin."""
    if sys.platform != "win32":
        return
    for base in map(Path, sys.path):
        nv = base / "nvidia"
        if nv.is_dir():
            for bin_dir in nv.glob("*/bin"):
                try:
                    os.add_dll_directory(str(bin_dir))
                    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                except OSError:
                    pass


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


class Transcriber:
    def __init__(self, model: str = "small.en", device: str = "auto", compute_type: str = "auto",
                 language: str | None = "en", download_root: str = "models/whisper", hints: str | None = None):
        _add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel

        self.language = language or None
        self.hints = hints or None   # names Whisper should expect, e.g. "Jarvis, Spotify, Notepad"
        root = str(resolve_path(download_root))
        attempts = []
        if device in ("auto", "cuda") and _cuda_available():
            attempts.append(("cuda", "float16" if compute_type == "auto" else compute_type))
        if device in ("auto", "cpu") or not attempts:
            # float16 only exists on GPUs; a CPU fallback always uses int8 unless told int8/float32 explicitly
            cpu_type = compute_type if compute_type in ("int8", "float32", "int8_float32") else "int8"
            attempts.append(("cpu", cpu_type))

        last_err = None
        for dev, ctype in attempts:
            try:
                try:  # use the cached copy without touching the network
                    self.model = WhisperModel(model, device=dev, compute_type=ctype, download_root=root,
                                              local_files_only=True)
                except Exception:
                    self.model = WhisperModel(model, device=dev, compute_type=ctype, download_root=root)
                # a tiny warm-up catches missing CUDA DLLs now instead of on the first command
                # warm up on 3 s of quiet noise with the real settings, so the first command isn't slow
                noise = (np.random.default_rng(0).standard_normal(48000) * 0.01).astype(np.float32)
                for vad in (True, False):   # loads the VAD model, then exercises the decoder itself
                    segments, _ = self.model.transcribe(noise, language=self.language, beam_size=1, vad_filter=vad,
                                                        condition_on_previous_text=False, initial_prompt=self.hints)
                    list(segments)  # transcription is lazy; iterate to actually run it
                self.device = dev
                log.info("Whisper %s on %s (%s)", model, dev, ctype)
                return
            except Exception as e:  # missing cuBLAS/cuDNN etc. -> fall back to CPU
                last_err = e
                log.warning("Whisper on %s failed (%s), trying next option", dev, e)
        raise RuntimeError(f"Could not load Whisper model {model}: {last_err}")

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            audio, language=self.language, beam_size=1, vad_filter=True,
            condition_on_previous_text=False, initial_prompt=self.hints,
        )
        return " ".join(s.text.strip() for s in segments if _confident(s)).strip()


def _confident(seg) -> bool:
    """Whisper's own 'this wasn't speech' test, plus the repetition loops it produces on music."""
    if getattr(seg, "no_speech_prob", 0) > 0.6 and getattr(seg, "avg_logprob", 0) < -1.0:
        return False
    return getattr(seg, "avg_logprob", 0) > -1.5 and getattr(seg, "compression_ratio", 0) < 2.6
