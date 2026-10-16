"""Microphone capture and speaker playback (sounddevice / PortAudio)."""
from __future__ import annotations

import logging
import queue
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000      # what openWakeWord, webrtcvad and Whisper all expect
FRAME_SAMPLES = 1280     # 80 ms, openWakeWord's native chunk size


def find_device(spec, kind: str):
    """Resolve a config value (None, index, or name substring) to a device index."""
    if spec is None or spec == "":
        return None
    if isinstance(spec, int) or (isinstance(spec, str) and spec.isdigit()):
        return int(spec)
    channels_key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, dev in enumerate(sd.query_devices()):
        if spec.lower() in dev["name"].lower() and dev[channels_key] > 0:
            return i
    log.warning("No %s device matching %r, using the Windows default", kind, spec)
    return None


def list_devices() -> str:
    lines = []
    default_in, default_out = sd.default.device
    for i, dev in enumerate(sd.query_devices()):
        tags = []
        if dev["max_input_channels"] > 0:
            tags.append("IN")
        if dev["max_output_channels"] > 0:
            tags.append("OUT")
        if i == default_in:
            tags.append("default-in")
        if i == default_out:
            tags.append("default-out")
        api = sd.query_hostapis(dev["hostapi"])["name"]
        lines.append(f"[{i:2}] {dev['name']}  ({api})  {' '.join(tags)}")
    return "\n".join(lines)


class Microphone:
    """Continuous 16 kHz mono int16 capture delivered as 80 ms frames on a queue."""

    def __init__(self, device=None):
        self.device = find_device(device, "input")
        self.frames: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=500)
        self._stream: sd.InputStream | None = None
        self.muted = threading.Event()   # set while Jarvis is talking, so it doesn't hear itself

    def _callback(self, indata, frames, time_info, status):
        if status:
            log.debug("mic status: %s", status)
        if self.muted.is_set():
            return
        try:
            self.frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def start(self):
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=FRAME_SAMPLES, device=self.device, callback=self._callback,
        )
        self._stream.start()
        name = sd.query_devices(self._stream.device)["name"]
        log.info("Listening on microphone: %s", name)

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def read(self, timeout: float | None = None) -> np.ndarray | None:
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def clear(self):
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break


class Speaker:
    def __init__(self, device=None):
        self.device = find_device(device, "output")
        self._lock = threading.Lock()

    def play(self, audio: np.ndarray, sample_rate: int, blocking: bool = True):
        if audio is None or len(audio) == 0:
            return
        with self._lock:
            sd.play(audio, sample_rate, device=self.device)
            if blocking:
                sd.wait()

    def stop(self):
        sd.stop()


def tone(freqs: list[float], duration: float = 0.09, sr: int = 22050, volume: float = 0.25) -> np.ndarray:
    """A tiny synthesized chime: one short sine per frequency, with fades."""
    parts = []
    n = int(sr * duration)
    t = np.arange(n) / sr
    fade = np.minimum(1, np.minimum(t, t[::-1]) / 0.01)
    for f in freqs:
        parts.append(np.sin(2 * np.pi * f * t) * fade * volume)
    return np.concatenate(parts).astype(np.float32)


CHIME_SR = 22050
CHIME_LISTEN = tone([660, 990])
CHIME_DONE = tone([880, 587])
CHIME_ERROR = tone([330, 220], duration=0.15)
