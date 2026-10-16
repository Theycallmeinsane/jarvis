""""Hey Jarvis" detection with openWakeWord (pre-trained model, runs on CPU via ONNX)."""
from __future__ import annotations

import logging
import os
import time

import numpy as np

log = logging.getLogger(__name__)


class WakeWordDetector:
    def __init__(self, model: str = "hey_jarvis", threshold: float = 0.5, cooldown_seconds: float = 1.5):
        import openwakeword
        from openwakeword.model import Model

        is_path = os.path.sep in model or model.endswith((".onnx", ".tflite"))
        if not is_path:
            # first run: fetch the feature extractor + pre-trained wake word models (a few MB)
            try:
                openwakeword.utils.download_models(model_names=[model])
            except TypeError:
                openwakeword.utils.download_models()

        self.model = Model(wakeword_models=[model], inference_framework="onnx")
        self.names = list(self.model.models.keys())
        self.threshold = threshold
        self.cooldown = cooldown_seconds
        self._last = 0.0
        log.info("Wake word ready: %s (threshold %.2f)", ", ".join(self.names), threshold)

    def process(self, frame: np.ndarray) -> float | None:
        """Feed one 80 ms int16 frame. Returns the score if the wake word fired."""
        scores = self.model.predict(frame)
        best = max(scores.values()) if scores else 0.0
        if best >= self.threshold and time.monotonic() - self._last > self.cooldown:
            self._last = time.monotonic()
            self.reset()
            return best
        return None

    def reset(self):
        # clear the rolling audio buffer so one utterance can't trigger twice
        self.model.reset()
