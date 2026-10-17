"""Offline tests: no mic, no models, no network.  Run: python -m unittest discover tests"""
import threading
import time
import unittest
from types import SimpleNamespace

from jarvis.config import load_config
from jarvis.llm import ToolCall, ToolResult
from jarvis.llm.base import LLMProvider, Turn
from jarvis.tools import ToolBox
from jarvis.tts import clean_for_speech


class ToolSchemaTests(unittest.TestCase):
    def test_schemas_are_valid_json_schema_objects(self):
        box = ToolBox(load_config())
        names = [s["name"] for s in box.schemas()]
        self.assertIn("open_app", names)
        self.assertIn("switch_model", names)
        self.assertNotIn("run_powershell", names)  # off by default
        for s in box.schemas():
            self.assertEqual(s["parameters"]["type"], "object")
            for r in s["parameters"]["required"]:
                self.assertIn(r, s["parameters"]["properties"])

    def test_find_files_matches_all_spoken_words(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            for f in ("Resume_Muhammad.pdf", "resume-old.docx", "notes.txt"):
                (Path(d) / f).write_text("x")
            cfg = load_config()
            cfg["tools"] = {**cfg["tools"], "search_dirs": [d]}
            out = ToolBox(cfg).run("find_files", {"name": "resume pdf"})
            self.assertIn("Resume_Muhammad.pdf", out)
            self.assertNotIn("resume-old.docx", out)

    def test_run_injects_context(self):
        box = ToolBox(load_config())
        self.assertIn("timer", box.run("list_timers", {}).lower())
        self.assertTrue(box.run("get_datetime", {"unexpected": 1}))


class SpeechCleanupTests(unittest.TestCase):
    def test_strips_markdown_and_think(self):
        text = "<think>hmm</think>**Sure!** Here's a [link](http://x.y) and `code` 🚀"
        self.assertEqual(clean_for_speech(text), "Sure! Here's a link and code")


def _block(**kw):
    ns = SimpleNamespace(**kw)
    ns.model_dump = lambda exclude_none=True: dict(kw)
    return ns


class FakeClaudeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class ClaudeProviderTests(unittest.TestCase):
    def test_tool_round_trip(self):
        from jarvis.llm.anthropic_claude import ClaudeProvider

        p = ClaudeProvider(system_prompt="sys", tools=[{"name": "get_datetime", "description": "d",
                                                        "parameters": {"type": "object", "properties": {}}}],
                           api_key_env=None)
        p.client = FakeClaudeClient([
            SimpleNamespace(stop_reason="tool_use", content=[
                _block(type="thinking", thinking="", signature="sig"),
                _block(type="tool_use", id="tu_1", name="get_datetime", input={})]),
            SimpleNamespace(stop_reason="end_turn", content=[_block(type="text", text="It's noon.")]),
        ])
        p.add_user("time?")
        turn = p.step()
        self.assertEqual(turn.tool_calls[0].name, "get_datetime")
        p.add_tool_results([ToolResult(turn.tool_calls[0], "12:00")])
        self.assertEqual(p.step().text, "It's noon.")

        first = p.client.calls[0]
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(first["tools"][0]["input_schema"]["type"], "object")
        # history: user, assistant (thinking kept), user(tool_result), assistant
        self.assertEqual([m["role"] for m in p.messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(p.messages[1]["content"][0]["type"], "thinking")
        self.assertEqual(p.messages[2]["content"][0]["tool_use_id"], "tu_1")


class ScriptedProvider(LLMProvider):
    name = "scripted"

    def __init__(self, turns, **kw):
        super().__init__(system_prompt="", tools=[], **kw)
        self.turns = list(turns)
        self.results = []

    def step(self, use_tools=True, on_text=None):
        return self.turns.pop(0)

    def add_tool_results(self, results):
        self.results += results


class AgentTests(unittest.TestCase):
    def _agent(self, turns, confirm):
        from jarvis.agent import Agent

        agent = Agent.__new__(Agent)
        agent.cfg = load_config()
        agent.confirm = confirm
        agent.toolbox = ToolBox(agent.cfg, {})
        agent.memory = None
        agent._lock = threading.Lock()
        agent._pending_switch = None
        agent._last_activity = time.monotonic()
        agent.provider = ScriptedProvider(turns)
        return agent

    def test_dangerous_tool_needs_confirmation(self):
        call = ToolCall("1", "close_app", {"name": "definitely-not-running"})
        agent = self._agent([Turn(tool_calls=[call]), Turn(text="Okay, left it open.")], confirm=lambda q: False)
        self.assertEqual(agent.handle("close it"), "Okay, I've left it.")   # declined: said directly
        self.assertIn("declined", agent.provider.results[0].content)

    def test_trigger_words_block_unrequested_actions(self):
        box = ToolBox(load_config())
        search = box.get("web_search")
        self.assertFalse(search.allowed_for("tell me a long story about a robot"))
        self.assertTrue(search.allowed_for("search YouTube for lofi"))
        self.assertTrue(box.get("open_app").allowed_for("could you launch spotify"))
        self.assertTrue(box.get("get_weather").allowed_for("anything at all"))   # info tools are unrestricted
        call = ToolCall("1", "web_search", {"query": "robot story"})
        agent = self._agent([Turn(tool_calls=[call]), Turn(text="Once upon a time...")], lambda q: True)
        agent.handle("tell me a story")
        self.assertIn("didn't ask", agent.provider.results[0].content)

    def test_unknown_tool_is_reported_as_error(self):
        agent = self._agent([Turn(tool_calls=[ToolCall("1", "fly", {})]), Turn(text="Can't.")], lambda q: True)
        agent.handle("fly")
        self.assertTrue(agent.provider.results[0].is_error)


if __name__ == "__main__":
    unittest.main()
