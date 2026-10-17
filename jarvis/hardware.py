"""Detect this PC's hardware and recommend models that will run well on it."""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)
NO_WINDOW = 0x08000000


@dataclass
class Hardware:
    gpu_name: str | None = None
    vram_gb: float = 0.0
    ram_gb: float = 0.0
    cpu_threads: int = 0

    @property
    def cuda(self) -> bool:
        return self.gpu_name is not None


@dataclass
class Recommendation:
    whisper_model: str
    whisper_compute: str
    ollama_model: str
    voice: str
    onnxruntime: str          # "onnxruntime-gpu" or "onnxruntime"
    notes: list[str] = field(default_factory=list)

    def config(self) -> dict:
        """The part of config.local.yaml these choices map to."""
        return {
            "stt": {"model": self.whisper_model, "compute_type": self.whisper_compute},
            "llm": {"providers": {"ollama": {"model": self.ollama_model}}},
            "tts": {"voice": self.voice},
        }


def detect() -> Hardware:
    import os

    import psutil

    hw = Hardware(ram_gb=psutil.virtual_memory().total / 2**30, cpu_threads=os.cpu_count() or 0)
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout.strip()
        if out:
            name, mem = [x.strip() for x in out.splitlines()[0].split(",")]
            hw.gpu_name, hw.vram_gb = name, float(mem) / 1024
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return hw


def recommend(hw: Hardware) -> Recommendation:
    notes = []
    if hw.cuda and hw.vram_gb >= 11.5:
        rec = Recommendation("large-v3-turbo", "float16", "qwen3.5:9b", "heart-hd", "onnxruntime-gpu")
        notes.append("Plenty of VRAM: the best Whisper model, a 9B brain and the HD voice fit together.")
    elif hw.cuda and hw.vram_gb >= 7.5:
        rec = Recommendation("small.en", "float16", "qwen3.5:4b", "heart-hd", "onnxruntime-gpu")
        notes.append("The HD (Kokoro) voice runs on the GPU here; friday / jarvis are lighter Piper voices.")
    elif hw.cuda and hw.vram_gb >= 3.5:
        rec = Recommendation("base.en", "float16", "qwen3.5:2b", "friday", "onnxruntime-gpu")
        notes.append("Smaller GPU: the 2B brain keeps everything on the graphics card.")
    elif hw.ram_gb >= 15:
        rec = Recommendation("base.en", "int8", "qwen3.5:2b", "friday", "onnxruntime")
        notes.append("No NVIDIA GPU: everything runs on the CPU, so replies take a few seconds longer.")
    else:
        rec = Recommendation("base.en", "int8", "qwen3.5:0.8b", "friday", "onnxruntime")
        notes.append("Low memory: the smallest brain is used. Claude (--llm claude) gives much better answers.")
    rec.notes = notes
    return rec


PERFORMANCE = ("fast", "balanced", "quality")
SMALLER_BRAIN = {"qwen3.5:9b": "qwen3.5:4b", "qwen3.5:4b": "qwen3.5:2b", "qwen3.5:2b": "qwen3.5:0.8b"}


def performance_settings(profile: str, hw: Hardware | None = None) -> dict:
    """Concrete settings for a speed-vs-quality choice on this hardware (written to config.local.yaml).

    fast:     smaller Whisper + brain, shorter pause before Jarvis answers (may cut slow talkers off)
    balanced: what setup recommends for the hardware
    quality:  bigger Whisper, a longer pause so you can think mid-sentence, more tool steps for the brain
    """
    hw = hw or detect()
    rec = recommend(hw)
    s = rec.config()
    s["recording"] = {"silence_ms": 700}
    s["llm"]["providers"]["ollama"]["max_tool_rounds"] = 1
    if profile == "fast":
        s["stt"]["model"] = "base.en"
        s["llm"]["providers"]["ollama"]["model"] = SMALLER_BRAIN.get(rec.ollama_model, rec.ollama_model)
        s["recording"]["silence_ms"] = 550
    elif profile == "quality":
        if hw.cuda and hw.vram_gb >= 7.5:
            s["stt"]["model"] = "large-v3-turbo" if hw.vram_gb >= 11.5 else "distil-large-v3"
        elif rec.whisper_model == "base.en":
            s["stt"]["model"] = "small.en"
        s["recording"]["silence_ms"] = 950
        s["llm"]["providers"]["ollama"]["max_tool_rounds"] = 2
    s["performance"] = profile
    s.pop("tts", None)          # the voice is chosen separately
    return s


def describe(hw: Hardware) -> str:
    gpu = f"{hw.gpu_name} ({hw.vram_gb:.0f} GB VRAM)" if hw.cuda else "no NVIDIA GPU"
    return f"{gpu}, {hw.ram_gb:.0f} GB RAM, {hw.cpu_threads} CPU threads"


def onnxruntime_status() -> tuple[bool, str]:
    """Exactly one onnxruntime build must be installed: CPU and GPU builds share one module and break
    each other when both (or neither) are present."""
    from importlib import metadata

    installed = []
    for dist in ("onnxruntime", "onnxruntime-gpu", "onnxruntime-directml"):
        try:
            installed.append(f"{dist} {metadata.version(dist)}")
        except metadata.PackageNotFoundError:
            pass
    try:
        import onnxruntime as ort

        providers = ort.get_available_providers()
    except Exception as e:  # noqa: BLE001
        return False, f"onnxruntime won't import ({e}); run setup.ps1 to repair"
    if len(installed) != 1:
        return False, f"{len(installed)} onnxruntime builds installed ({', '.join(installed) or 'none'}); " \
                      "run setup.ps1 to repair"
    gpu = "CUDA" if "CUDAExecutionProvider" in providers else "CPU only"
    return True, f"{installed[0]} ({gpu})"
