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


class SentenceSplitter:
    """Collect streamed LLM text and hand over complete sentences, so speaking starts early."""

    BOUNDARY = re.compile(r"[.!?;:](?=\s)|\n")
    MIN_CHARS = 12   # so "Mr." or "1." never get spoken on their own

    def __init__(self, emit):
        self.emit = emit
        self.buf = ""
        self.emitted = False

    def feed(self, text: str):
        self.buf += text
        if "<think>" in self.buf and "</think>" not in self.buf:
            return  # wait for the reasoning block to close, then drop it
        self.buf = re.sub(r"<think>.*?</think>", "", self.buf, flags=re.S)
        while True:
            cut = next((m.end() for m in self.BOUNDARY.finditer(self.buf)
                        if len(self.buf[:m.end()].strip()) >= self.MIN_CHARS), None)
            if cut is None:
                break
            sentence, self.buf = self.buf[:cut].strip(), self.buf[cut:]
            if sentence:
                self.emitted = True
                self.emit(sentence)

    def flush(self):
        rest = re.sub(r"<think>.*?(</think>|$)", "", self.buf, flags=re.S).strip()
        self.buf = ""
        if rest:
            self.emitted = True
            self.emit(rest)


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


def cuda_onnx_available() -> bool:
    try:
        import onnxruntime as ort

        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:  # noqa: BLE001
        return False


class KokoroTTS:
    """Kokoro-82M: the most natural open voices. Needs an NVIDIA GPU (onnxruntime-gpu) to keep up in
    conversation; on a CPU it's slower than real time."""

    def __init__(self, voice: str = "af_heart", speed: float = 1.0, models_dir: str = "models/kokoro",
                 device: str = "auto", allow_cpu: bool = False):
        import onnxruntime as ort
        from kokoro_onnx import Kokoro

        d = resolve_path(models_dir)
        _download(f"{KOKORO_BASE}/kokoro-v1.0.onnx", d / "kokoro-v1.0.onnx")
        _download(f"{KOKORO_BASE}/voices-v1.0.bin", d / "voices-v1.0.bin")
        ort.set_default_logger_severity(3)   # hide harmless CUDA graph-optimisation warnings
        providers = ["CPUExecutionProvider"]
        if device in ("auto", "cuda") and cuda_onnx_available():
            if hasattr(ort, "preload_dlls"):
                ort.preload_dlls()    # load the CUDA/cuDNN DLLs shipped as pip packages
            providers.insert(0, "CUDAExecutionProvider")
        session = ort.InferenceSession(str(d / "kokoro-v1.0.onnx"), providers=providers)
        self.device = "cuda" if "CUDAExecutionProvider" in session.get_providers() else "cpu"
        if self.device == "cpu" and not allow_cpu:
            raise RuntimeError("Kokoro needs an NVIDIA GPU to speak in real time (set allow_cpu: true to force)")
        self.kokoro = Kokoro.from_session(session, str(d / "voices-v1.0.bin"))
        self.voice = voice
        self.speed = speed
        self.lang = "en-gb" if voice.startswith("b") else "en-us"
        log.info("Kokoro on %s", self.device)

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


# Ready-made voices. All open source and fully local. "-hd" voices use Kokoro and need an NVIDIA GPU;
# on other PCs they fall back to the Piper voice named in "fallback".
VOICE_PRESETS: dict[str, dict] = {
    "friday":    {"engine": "piper", "voice": "en_GB-jenny_dioco-medium", "desc": "female, British"},
    "amy":       {"engine": "piper", "voice": "en_US-amy-medium", "desc": "female, American"},
    "jarvis":    {"engine": "piper", "voice": "en_GB-alan-medium", "desc": "male, British"},
    "friday-hd": {"engine": "kokoro", "voice": "bf_emma", "fallback": "friday", "desc": "female, British, HD"},
    "heart-hd":  {"engine": "kokoro", "voice": "af_heart", "fallback": "amy", "desc": "female, American, HD"},
    "bella-hd":  {"engine": "kokoro", "voice": "af_bella", "fallback": "amy", "desc": "female, American, HD"},
    "jarvis-hd": {"engine": "kokoro", "voice": "bm_george", "fallback": "jarvis", "desc": "male, British, HD"},
    "windows":   {"engine": "sapi", "desc": "built-in Windows voice"},
}
VOICE_ALIASES = {"female": "friday", "woman": "friday", "girl": "friday", "male": "jarvis", "man": "jarvis",
                 "british": "friday", "american": "amy", "hd": "friday-hd", "default": "friday"}


def resolve_preset(name: str) -> str | None:
    key = (name or "").lower().strip().replace(" voice", "").replace(" ", "-")
    key = VOICE_ALIASES.get(key, key)
    return key if key in VOICE_PRESETS else None


def _build(engine: str, cfg: Section, voice: str | None = None):
    opts = dict(cfg.get(engine) or {})
    if engine == "piper":
        if voice:
            opts["voice"] = voice
        return PiperTTS(**{k: v for k, v in opts.items() if k in ("voice", "length_scale", "models_dir")})
    if engine == "kokoro":
        if voice:
            opts["voice"] = voice
        return KokoroTTS(**{k: v for k, v in opts.items()
                            if k in ("voice", "speed", "models_dir", "device", "allow_cpu")})
    return SapiTTS(**{k: v for k, v in opts.items() if k in ("rate", "voice")})


def create_tts(cfg: Section, choice: str | None = None):
    """Build the chosen voice (a preset like 'friday' / 'jarvis-hd', or an engine name), falling back
    HD -> Piper -> the Windows voice, so Jarvis always talks and the log says why."""
    choice = choice or cfg.get("voice") or cfg.get("provider") or "friday"
    attempts: list[tuple[str, str | None, str]] = []      # (engine, voice, label)
    preset = resolve_preset(choice)
    if preset:
        p = VOICE_PRESETS[preset]
        attempts.append((p["engine"], p.get("voice"), preset))
        if p.get("fallback"):
            fb = VOICE_PRESETS[p["fallback"]]
            attempts.append((fb["engine"], fb["voice"], p["fallback"]))
    elif choice in ("piper", "kokoro", "sapi"):
        attempts.append((choice, None, choice))
    else:
        log.warning("Unknown voice %r; using friday", choice)
    if not any(a[0] == "piper" for a in attempts):
        attempts.append(("piper", VOICE_PRESETS["friday"]["voice"], "friday"))
    attempts.append(("sapi", None, "windows"))

    for engine, voice, label in attempts:
        try:
            tts = _build(engine, cfg, voice)
            tts.preset = label
            if label != attempts[0][2]:
                log.warning("Voice: using %s instead of %s", label, attempts[0][2])
            log.info("Voice: %s (%s)", label, VOICE_PRESETS.get(label, {}).get("desc", engine))
            return tts
        except Exception as e:  # noqa: BLE001
            hint = " (run setup.ps1 to repair the install)" if "onnxruntime" in str(e) else ""
            log.warning("Voice %s unavailable: %s%s", label, e, hint)
    raise RuntimeError("No voice could be loaded, not even the built-in Windows one")
