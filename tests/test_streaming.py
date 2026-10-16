"""Streaming pieces: sentence splitting, provider stream parsing, speak-while-generating."""
import json
import queue
import threading
import time
import unittest
from unittest import mock

import numpy as np

from jarvis.tts import SentenceSplitter


class SplitterTests(unittest.TestCase):
    def _run(self, chunks):
        out = []
        s = SentenceSplitter(out.append)
        for c in chunks:
            s.feed(c)
        s.flush()
        return out

    def test_splits_on_sentence_ends_across_chunks(self):
        out = self._run(["It is twenty", "-five degrees in Lahore. Hum", "idity is low! Anything else?"])
        self.assertEqual(out, ["It is twenty-five degrees in Lahore.", "Humidity is low!", "Anything else?"])

    def test_short_fragments_stay_attached(self):
        out = self._run(["Mr. Smith called at 3. He'll ring back."])
        self.assertEqual(out, ["Mr. Smith called at 3.", "He'll ring back."])

    def test_drops_think_blocks(self):
        out = self._run(["<think>let me", " reason</think>Sure thing, sir. ", "Done."])
        self.assertEqual(out, ["Sure thing, sir.", "Done."])

    def test_decimal_numbers_not_split(self):
        out = self._run(["Version 3.5 is out now and it is great."])
        self.assertEqual(out, ["Version 3.5 is out now and it is great."])


class _FakeResponse:
    def __init__(self, lines, status=200):
        self.lines, self.status_code, self.text = lines, status, ""

    def iter_lines(self, decode_unicode=False):
        return iter(self.lines)

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.lines[0])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ProviderStreamTests(unittest.TestCase):
    def test_ollama_stream_text_and_tool_calls(self):
        from jarvis.llm.ollama import OllamaProvider

        lines = [json.dumps({"message": {"content": "Let me "}}),
                 json.dumps({"message": {"content": "check."}}),
                 json.dumps({"message": {"tool_calls": [{"function": {"name": "get_datetime", "arguments": {}}}]}}),
                 json.dumps({"done": True, "message": {}})]
        p = OllamaProvider(system_prompt="s", tools=[])
        got = []
        with mock.patch("jarvis.llm.ollama.requests.post", return_value=_FakeResponse(lines)):
            p.add_user("time?")
            turn = p.step(on_text=got.append)
        self.assertEqual("".join(got), "Let me check.")
        self.assertEqual(turn.tool_calls[0].name, "get_datetime")
        self.assertEqual(p.messages[-1]["tool_calls"][0]["function"]["name"], "get_datetime")

    def test_openai_stream_reassembles_tool_call_fragments(self):
        from jarvis.llm.openai_compat import OpenAICompatProvider

        def sse(delta):
            return "data: " + json.dumps({"choices": [{"delta": delta}]})
        lines = [sse({"content": "On it."}),
                 sse({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "volume", "arguments": '{"act'}}]}),
                 sse({"tool_calls": [{"index": 0, "function": {"arguments": 'ion": "up"}'}}]}),
                 "data: [DONE]"]
        p = OpenAICompatProvider(base_url="http://x/v1", model="m", system_prompt="s", tools=[])
        got = []
        with mock.patch("jarvis.llm.openai_compat.requests.post", return_value=_FakeResponse(lines)):
            p.add_user("louder")
            turn = p.step(on_text=got.append)
        self.assertEqual(got, ["On it."])
        self.assertEqual(turn.tool_calls[0].arguments, {"action": "up"})
        self.assertEqual(turn.tool_calls[0].id, "c1")


class _Spk:
    def __init__(self):
        self.played = []

    def play(self, audio, sr, blocking=True):
        self.played.append((time.monotonic(), len(audio) / sr))

    def stop(self):
        pass


class SpeakWhileGeneratingTests(unittest.TestCase):
    def _assistant(self, agent):
        from jarvis.app import VoiceAssistant

        va = VoiceAssistant.__new__(VoiceAssistant)
        va.cfg = {"recording": {}}
        va.on_state = lambda s: None
        va.state = "idle"
        va.stop_event, va.paused, va.hush = threading.Event(), threading.Event(), threading.Event()
        va.barge_in = False
        va._active = None
        va.mic = mock.MagicMock()
        va.mic.muted = threading.Event()
        va.wake = mock.MagicMock()
        va.speaker = _Spk()
        va.tts = mock.MagicMock()
        va.tts.synthesize.side_effect = lambda text: (np.zeros(1600, dtype=np.float32), 16000)  # 0.1 s each
        va.agent = agent
        return va

    def test_first_sentence_plays_before_agent_finishes(self):
        finished = {}

        class SlowAgent:
            def handle(self, text, on_text=None):
                on_text("First sentence comes quickly. ")
                time.sleep(0.6)
                on_text("Second one is slower.")
                finished["t"] = time.monotonic()
                return "First sentence comes quickly. Second one is slower."

        va = self._assistant(SlowAgent())
        self.assertFalse(va.respond("hi"))
        self.assertEqual(len(va.speaker.played), 2)
        self.assertLess(va.speaker.played[0][0], finished["t"] - 0.4)

    def test_confirmation_is_asked_on_speaking_thread(self):
        asked = []

        class ConfirmingAgent:
            def __init__(self, va):
                self.va = va

            def handle(self, text, on_text=None):
                on_text("Preparing the email now. ")
                ok = self.va.confirm("Send it?")
                on_text("Sent." if ok else "Cancelled.")
                return ""

        va = self._assistant(None)
        va.agent = ConfirmingAgent(va)

        def ask(q):
            asked.append((q, threading.current_thread().name))
            return True
        va._ask_yes_no = ask
        va.respond("email sara")
        self.assertEqual(asked[0][0], "Send it?")
        self.assertNotEqual(asked[0][1], "agent")


if __name__ == "__main__":
    unittest.main()
