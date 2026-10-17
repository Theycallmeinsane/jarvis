"""Voice presets, fallbacks, hardware recommendations and the setup wizard (non-interactive)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from jarvis import hardware, tts
from jarvis.config import Section, load_config


class PresetTests(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(tts.resolve_preset("female"), "friday")
        self.assertEqual(tts.resolve_preset("Male voice"), "jarvis")
        self.assertEqual(tts.resolve_preset("heart hd"), "heart-hd")
        self.assertIsNone(tts.resolve_preset("klingon"))

    def test_every_hd_preset_has_a_piper_fallback(self):
        for name, p in tts.VOICE_PRESETS.items():
            if p["engine"] == "kokoro":
                self.assertEqual(tts.VOICE_PRESETS[p["fallback"]]["engine"], "piper", name)

    def _fake_build(self, broken: set):
        def build(engine, cfg, voice=None):
            if engine in broken:
                raise RuntimeError(f"{engine} broken (No module named 'onnxruntime')")
            return mock.MagicMock()
        return build

    def test_hd_falls_back_to_piper_then_windows(self):
        cfg = Section({"voice": "heart-hd"})
        with mock.patch.object(tts, "_build", side_effect=self._fake_build({"kokoro"})):
            self.assertEqual(tts.create_tts(cfg).preset, "amy")
        with mock.patch.object(tts, "_build", side_effect=self._fake_build({"kokoro", "piper"})), \
                self.assertLogs("jarvis.tts", "WARNING") as logs:
            self.assertEqual(tts.create_tts(cfg).preset, "windows")
        self.assertTrue(any("setup.ps1" in line for line in logs.output))   # tells the user how to repair

    def test_default_is_the_female_voice(self):
        self.assertEqual(load_config().tts.get("voice") if not Path("config.local.yaml").exists()
                         else yaml.safe_load(open("config.yaml", encoding="utf-8"))["tts"]["voice"], "friday")


class HardwareTests(unittest.TestCase):
    def test_recommendations_scale_with_hardware(self):
        big = hardware.recommend(hardware.Hardware("RTX 4090", 24, 64, 32))
        mid = hardware.recommend(hardware.Hardware("RTX 3050", 8, 16, 16))
        small_gpu = hardware.recommend(hardware.Hardware("GTX 1650", 4, 16, 8))
        cpu = hardware.recommend(hardware.Hardware(None, 0, 16, 8))
        tiny = hardware.recommend(hardware.Hardware(None, 0, 8, 4))
        self.assertEqual(big.ollama_model, "qwen3.5:9b")
        self.assertEqual((mid.ollama_model, mid.voice, mid.onnxruntime), ("qwen3.5:4b", "heart-hd", "onnxruntime-gpu"))
        self.assertEqual(small_gpu.ollama_model, "qwen3.5:2b")
        self.assertEqual((cpu.whisper_compute, cpu.voice, cpu.onnxruntime), ("int8", "friday", "onnxruntime"))
        self.assertEqual(tiny.ollama_model, "qwen3.5:0.8b")
        for r in (big, mid, small_gpu, cpu, tiny):     # CPU-only PCs never get a GPU-only voice
            if r.onnxruntime == "onnxruntime":
                self.assertEqual(tts.VOICE_PRESETS[r.voice]["engine"], "piper")

    def test_runtime_status_reports_a_single_build(self):
        ok, text = hardware.onnxruntime_status()
        self.assertTrue(ok, text)


class WizardTests(unittest.TestCase):
    def test_non_interactive_run_writes_local_config_only(self):
        from jarvis import setup_wizard

        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "config.local.yaml"
            hw = hardware.Hardware(None, 0, 16, 8)
            with mock.patch.object(setup_wizard, "detect", return_value=hw), \
                    mock.patch.object(setup_wizard, "save_local",
                                      side_effect=lambda s: __import__("jarvis.config").config.save_local(s, local)), \
                    mock.patch.object(setup_wizard, "cuda_onnx_available", return_value=False):
                settings = setup_wizard.run_wizard(answers={"name": "Sam", "address": "boss", "voice": "amy",
                                                            "mic": "", "brain": "local", "shortcuts": "n",
                                                            "autostart": "n"})
            saved = yaml.safe_load(local.read_text(encoding="utf-8"))
        self.assertEqual(saved["assistant"], {"user_name": "Sam", "address": "boss"})
        self.assertEqual(saved["tts"]["voice"], "amy")
        self.assertEqual(saved["llm"]["provider"], "ollama")
        self.assertEqual(saved["llm"]["providers"]["ollama"]["model"], "qwen3.5:2b")
        self.assertEqual(saved["stt"]["compute_type"], "int8")
        self.assertEqual(settings["tts"]["voice"], "amy")

    def test_save_local_merges(self):
        from jarvis.config import save_local

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.local.yaml"
            save_local({"assistant": {"address": "sir"}, "tts": {"voice": "friday"}}, p)
            save_local({"tts": {"voice": "jarvis"}}, p)
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        self.assertEqual(data, {"assistant": {"address": "sir"}, "tts": {"voice": "jarvis"}})


if __name__ == "__main__":
    unittest.main()
