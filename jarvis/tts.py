"""Text-to-speech engines. All run locally: Piper (fast), Kokoro (most natural), SAPI (built in)."""
from __future__ import annotations

import logging
import re
import urllib.request
from pathlib import Path

import numpy as np

from .config import Section, resolve_path

log = logging.getLogger(__name__)

PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"


def _download(url: str, dest: Path):
    if dest.exists() and dest.stat().st_size > 0:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("Downloading %s ...", url.rsplit("/", 1)[-1])
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)


def clean_for_speech(text: str) -> str:
    """Strip markdown, code, links, emoji and stray reasoning tags so the voice reads cleanly."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"\s?°\s?C\b", " degrees", text).replace("°", " degrees")
    text = re.sub(r"(?<=\d)%", " percent", text)
    text = re.sub(r"[*_#>|~]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", "", text)
    return re.sub(r"\s+", " ", text).strip()


class PiperTTS:
    def __init__(self, voice: str = "en_GB-alan-medium", length_scale: float = 1.0, models_dir: str = "models/piper"):
        from piper import PiperVoice

        model_path = resolve_path(models_dir) / f"{voice}.onnx"
        lang_region, name, quality = voice.split("-", 2)
        lang = lang_region.split("_")[0]
        url = f"{PIPER_BASE}/{lang}/{lang_region}/{name}/{quality}/{voice}.onnx"
        _download(url, model_path)
        _download(url + ".json", model_path.with_suffix(".onnx.json"))
        self.voice = PiperVoice.load(str(model_path))
        self.sample_rate = self.voice.config.sample_rate
        self.length_scale = length_scale

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        try:  # piper-tts >= 1.3
            from piper import SynthesisConfig

            cfg = SynthesisConfig(length_scale=self.length_scale)
            chunks = [c.audio_int16_array for c in self.voice.synthesize(text, syn_config=cfg)]
            audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
        except ImportError:  # piper-tts 1.2
            raw = b"".join(self.voice.synthesize_stream_raw(text, length_scale=self.length_scale))
            audio = np.frombuffer(raw, dtype=np.int16)
        return audio.astype(np.float32) / 32768.0, self.sample_rate


class KokoroTTS:
    def __init__(self, voice: str = "bm_george", speed: float = 1.0, models_dir: str = "models/kokoro"):
        from kokoro_onnx import Kokoro

        d = resolve_path(models_dir)
        _download(f"{KOKORO_BASE}/kokoro-v1.0.onnx", d / "kokoro-v1.0.onnx")
        _download(f"{KOKORO_BASE}/voices-v1.0.bin", d / "voices-v1.0.bin")
        self.kokoro = Kokoro(str(d / "kokoro-v1.0.onnx"), str(d / "voices-v1.0.bin"))
        self.voice = voice
        self.speed = speed
        self.lang = "en-gb" if voice.startswith("b") else "en-us"

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        samples, sr = self.kokoro.create(text, voice=self.voice, speed=self.speed, lang=self.lang)
        return np.asarray(samples, dtype=np.float32), sr


class SapiTTS:
    """Windows built-in voices via pyttsx3. Renders to a temp WAV so playback goes through our speaker."""

    def __init__(self, rate: int = 185, voice: str | None = None):
        import pyttsx3

        self.engine = pyttsx3.init()
        self.engine.setProperty("rate", rate)
        if voice:
            for v in self.engine.getProperty("voices"):
                if voice.lower() in v.name.lower():
                    self.engine.setProperty("voice", v.id)
                    break

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        import tempfile
        import wave

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.wav"
            self.engine.save_to_file(text, str(path))
            self.engine.runAndWait()
            with wave.open(str(path)) as w:
                sr, width = w.getframerate(), w.getsampwidth()
                data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16 if width == 2 else np.uint8)
                if w.getnchannels() > 1:
                    data = data.reshape(-1, w.getnchannels())[:, 0]
        return data.astype(np.float32) / 32768.0, sr


def create_tts(cfg: Section, provider: str | None = None):
    """Build the configured engine, falling back to SAPI so Jarvis can always talk."""
    provider = provider or cfg.provider
    order = [provider] + (["sapi"] if provider != "sapi" else [])
    for name in order:
        opts = dict(cfg.get(name) or {})
        try:
            engine = {"piper": PiperTTS, "kokoro": KokoroTTS, "sapi": SapiTTS}[name](**opts)
            log.info("TTS: %s", name)
            return engine
        except Exception as e:
            log.warning("TTS %s unavailable (%s)", name, e)
    raise RuntimeError("No TTS engine could be loaded")
