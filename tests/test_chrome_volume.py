"""App volume routing, stale-question replay, Whisper confidence, and the Chrome link (no browser needed)."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis import chrome_bridge
from jarvis.agent import Agent
from jarvis.config import Section, load_config
from jarvis.llm import ToolCall
from jarvis.memory import Memory
from jarvis.router import route
from jarvis.stt import _confident
from jarvis.tools import chrome as ch


class AllTools:
    def get(self, name):
        return object()


class AppVolumeTests(unittest.TestCase):
    def test_app_named_anywhere_goes_to_that_app(self):
        cases = {
            "increase the volume on spotify": {"app": "spotify", "action": "up"},
            "turn up spotify's volume a bit": {"app": "spotify", "action": "up"},
            "decrease the volume of spotify": {"app": "spotify", "action": "down"},
            "can you lower spotify by 20": {"app": "spotify", "action": "down", "percent": 20},
            "turn the volume down on spotify": {"app": "spotify", "action": "down"},
            "increase the volume in chrome to 80": {"app": "chrome", "action": "set", "percent": 80},
            "max out the music": {"app": "music", "action": "set", "percent": 100},
            "what's the volume on spotify": {"app": "spotify", "action": "get"},
        }
        for text, args in cases.items():
            self.assertEqual(route(text, AllTools()), [("app_volume", args)], text)

    def test_whole_pc_and_other_spotify_commands_are_unchanged(self):
        self.assertEqual(route("increase volume", AllTools()), [("volume", {"action": "up"})])
        self.assertEqual(route("pause spotify", AllTools())[0][0], "media_control")
        self.assertEqual(route("play glimpse of us on spotify", AllTools())[0][0], "play_song")

    def test_brain_picking_system_volume_for_an_app_is_redirected(self):
        agent = Agent.__new__(Agent)
        agent.toolbox = AllTools()
        agent._request = "make spotify a little louder please"
        call = agent._redirect(ToolCall("1", "volume", {"action": "up", "percent": 10}))
        self.assertEqual((call.name, call.arguments), ("app_volume", {"app": "spotify", "action": "up", "percent": 10}))
        agent._request = "make it louder"
        self.assertEqual(agent._redirect(ToolCall("2", "volume", {"action": "up"})).name, "volume")


class RepeatTests(unittest.TestCase):
    def test_a_question_jarvis_asked_is_never_replayed(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Section({"memory": {"history_file": str(Path(d) / "h.jsonl"), "facts_file": str(Path(d) / "m.json")}})
            m = Memory(cfg)
            m.log_exchange("open oculus", "I cannot find a window named Oculus. Would you like me to search online?", "x")
            m.log_exchange("what time is it", "It's 9 pm.", "x")
            self.assertEqual([r["user"] for r in m.recent_exchanges()], ["what time is it"])
            rows = [json.loads(line) for line in (Path(d) / "h.jsonl").read_text().splitlines()]
            rows[-1]["t"] -= 31 * 60                      # older than 30 minutes: not replayed either
            (Path(d) / "h.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            self.assertEqual(m.recent_exchanges(), [])

    def test_idle_reset_starts_clean(self):
        agent = Agent.__new__(Agent)
        agent.memory = mock.Mock(seed_messages=lambda: [{"role": "user", "content": "old"}])
        agent._system_prompt = lambda: "prompt"
        p = SimpleNamespace(reset=lambda: None, system_prompt="prompt", messages=[])
        agent._fresh_conversation(p)
        self.assertEqual(p.messages, [])
        agent._fresh_conversation(p, prime_now=True)  # after a restart: picks up where it left off
        self.assertEqual(p.messages, [{"role": "user", "content": "old"}])

    def test_unsure_whisper_segments_are_dropped(self):
        seg = lambda **kw: SimpleNamespace(**{"no_speech_prob": 0.1, "avg_logprob": -0.3, "compression_ratio": 1.4, **kw})
        self.assertTrue(_confident(seg()))
        self.assertFalse(_confident(seg(no_speech_prob=0.8, avg_logprob=-1.1)))   # a video's background noise
        self.assertFalse(_confident(seg(compression_ratio=3.1)))                   # "here, here, here, here"
        self.assertFalse(_confident(seg(avg_logprob=-1.8)))

    def test_shut_up_cancels(self):
        from jarvis.app import CANCEL

        for t in ("shut up", "be quiet", "stop talking", "Stop.", "never mind"):
            self.assertTrue(CANCEL.match(t), t)


class ChromeTests(unittest.TestCase):
    def test_spoken_names_to_addresses(self):
        self.assertEqual(ch._url("youtube"), "https://youtube.com")
        self.assertEqual(ch._url("github.com/theycallmeinsane"), "https://github.com/theycallmeinsane")
        self.assertEqual(ch._url("localhost:3000"), "http://localhost:3000")
        self.assertTrue(ch._url("weather in lahore").startswith("https://www.google.com/search?q=weather"))

    def test_find_tab(self):
        tabs = [{"id": 1, "title": "Inbox (3) - Gmail", "url": "https://mail.google.com/"},
                {"id": 2, "title": "lofi beats - YouTube", "url": "https://www.youtube.com/watch?v=x"},
                {"id": 3, "title": "Theycallmeinsane/jarvis", "url": "https://github.com/Theycallmeinsane/jarvis"}]
        self.assertEqual(ch.find_tab(tabs, "the youtube one")["id"], 2)
        self.assertEqual(ch.find_tab(tabs, "my email")["id"], 1)
        self.assertEqual(ch.find_tab(tabs, "github")["id"], 3)
        self.assertEqual(ch.find_tab(tabs, "second")["id"], 2)
        self.assertIsNone(ch.find_tab(tabs, "netflix"))

    def test_tab_and_claude_phrases_are_instant(self):
        self.assertEqual(route("close this tab", AllTools()), [("chrome_tab", {"action": "close", "name": "this"})])
        self.assertEqual(route("switch to the youtube tab", AllTools()),
                         [("chrome_tab", {"action": "switch", "name": "youtube"})])
        self.assertEqual(route("ask Claude what's new in Python", AllTools()),
                         [("claude_chat", {"action": "ask", "prompt": "what's new in python"})])

    def test_long_claude_answers_are_trimmed_for_speech(self):
        short = ch._speakable("Four.")
        self.assertEqual(short, "Four.")
        long = ch._speakable("```py\nx = 1\n```\n" + "A full sentence here. " * 60)
        self.assertLess(len(long), 700)
        self.assertIn("(code on screen)", long)
        self.assertTrue(long.endswith("on screen in Claude."))

    def test_bridge_only_accepts_the_extension(self):
        from websockets.exceptions import InvalidStatus
        from websockets.sync.client import connect

        bridge = chrome_bridge.Bridge(port=0).start()      # any free port
        self.addCleanup(bridge.stop)
        port = bridge._server.socket.getsockname()[1]
        with self.assertRaises(InvalidStatus):
            connect(f"ws://127.0.0.1:{port}/chrome", origin="https://some-website.example")
        ext = connect(f"ws://127.0.0.1:{port}/chrome", origin=f"chrome-extension://{chrome_bridge.EXTENSION_ID}")
        self.addCleanup(ext.close)

        def extension():
            for raw in ext:
                msg = json.loads(raw)
                ext.send(json.dumps({"id": msg["id"], "ok": True, "result": [{"id": 7, "title": "x"}]}))

        threading.Thread(target=extension, daemon=True).start()
        for _ in range(50):
            if bridge.connected:
                break
            time.sleep(0.05)
        self.assertEqual(bridge.call("tabs"), [{"id": 7, "title": "x"}])

    def test_without_the_extension_tools_say_how_to_link(self):
        box = SimpleNamespace(cfg=Section(load_config()), context={})
        with mock.patch.object(ch, "_linked", return_value=False), \
                mock.patch.object(ch, "_bridge", side_effect=lambda ctx: chrome_bridge.Bridge(port=1)):
            with self.assertRaises(ch.ToolFailed) as e:
                ch.chrome_read(ctx=box)
            self.assertIn("--chrome-setup", str(e.exception))
            with mock.patch.object(ch.webbrowser, "open") as opened:
                self.assertIn("opened Claude", ch.claude_chat("ask", "hello there", ctx=box))
                self.assertIn("claude.ai/new?q=hello%20there", opened.call_args[0][0])

    def test_risky_clicks_ask_first(self):
        asked = []
        box = SimpleNamespace(cfg=Section(load_config()), context={"confirm": lambda q: asked.append(q) or False})
        with mock.patch.object(ch, "_call") as call:
            self.assertEqual(ch.chrome_click("buy now", ctx=box), "Okay, I didn't click it.")
            call.assert_not_called()
        self.assertEqual(asked, ["Shall I click buy now?"])


if __name__ == "__main__":
    unittest.main()
