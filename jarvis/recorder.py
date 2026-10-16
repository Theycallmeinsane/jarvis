"""Record one spoken command: wait for speech, stop after a pause (webrtcvad endpointing)."""
from __future__ import annotations

import collections
import logging
import time

import numpy as np

from .audio import SAMPLE_RATE, Microphone

log = logging.getLogger(__name__)

VAD_FRAME = 480  # 30 ms at 16 kHz


class CommandRecorder:
    def __init__(self, mic: Microphone, aggressiveness: int = 2, silence_ms: int = 800,
                 start_timeout_s: float = 6, max_seconds: float = 20):
        import webrtcvad

        self.mic = mic
        self.vad = webrtcvad.Vad(aggressiveness)
        self.silence_frames = max(1, silence_ms // 30)
        self.start_timeout = start_timeout_s
        self.max_frames = int(max_seconds * 1000 / 30)

    def record(self, start_timeout: float | None = None) -> np.ndarray | None:
        """Returns float32 audio in [-1, 1] at 16 kHz, or None if nobody spoke."""
        start_timeout = self.start_timeout if start_timeout is None else start_timeout
        preroll = collections.deque(maxlen=10)   # ~300 ms before speech starts
        voiced: list[np.ndarray] = []
        pending = np.zeros(0, dtype=np.int16)
        speaking = False
        speech_run = 0
        silence_run = 0
        deadline = time.monotonic() + start_timeout

        while True:
            block = self.mic.read(timeout=0.5)
            if block is None:
                if not speaking and time.monotonic() > deadline:
                    return None
                continue
            pending = np.concatenate([pending, block])
            while len(pending) >= VAD_FRAME:
                frame, pending = pending[:VAD_FRAME], pending[VAD_FRAME:]
                is_speech = self.vad.is_speech(frame.tobytes(), SAMPLE_RATE)
                if not speaking:
                    preroll.append(frame)
                    speech_run = speech_run + 1 if is_speech else 0
                    if speech_run >= 3:   # 90 ms of speech -> started talking
                        log.debug("speech started")
                        speaking = True
                        voiced.extend(preroll)
                        preroll.clear()
                    elif time.monotonic() > deadline:
                        return None
                else:
                    voiced.append(frame)
                    silence_run = 0 if is_speech else silence_run + 1
                    if silence_run >= self.silence_frames or len(voiced) >= self.max_frames:
                        audio = np.concatenate(voiced).astype(np.float32) / 32768.0
                        log.debug("recorded %.1fs", len(audio) / SAMPLE_RATE)
                        return audio
