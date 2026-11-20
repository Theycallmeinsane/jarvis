"""The local brain's prompt has to fit its context window: only the tools a request needs are sent, and
memory only holds what the user asked to keep."""
import json
import os
import tempfile
import threading
import time
import unittest

from jarvis import tool_select
from jarvis.config import load_config
from jarvis.llm import ToolCall
from jarvis.llm.base import LLMProvider, Turn
from jarvis.memory import Memory
from jarvis.tools import ToolBox


class Recorder(LLMProvider):
    """Remembers which tools it was offered at each step."""
    name = "ollama"

    def __init__(self, turns, num_ctx=8192):
        super().__init__(system_prompt="You are Jarvis.", tools=[])
        self.turns, self.offered, self.results, self.num_ctx = list(turns), [], [], num_ctx

    def step(self, use_tools=True, on_text=None):
        self.offered.append([t["name"] for t in self.tools] if use_tools else [])
        return self.turns.pop(0)

    def add_tool_results(self, results):
        self.results += results


def agent_with(provider, memory=None):
    from jarvis.agent import Agent

    agent = Agent.__new__(Agent)
    agent.cfg = load_config()
    agent.confirm = lambda q: True
    agent.toolbox = ToolBox(agent.cfg, {"memory": memory})
    agent.memory = memory
    agent._lock = threading.Lock()
    agent._pending_switch = None
    agent._last_activity = time.monotonic()
    agent.provider = provider
    return agent


class PickTests(unittest.TestCase):
    def setUp(self):
        self.box = ToolBox(load_config(), {})

    def size(self, names):
        return tool_select.estimate_tokens([{"type": "function", "function": self.box.get(n).schema(self.box.cfg)}
                                            for n in names])

    def test_everything_at_once_would_overflow(self):
        # the reason for all this: every tool together is bigger than an 8k context
        self.assertGreater(self.size(list(self.box.tools)), 8192)

    def test_requests_get_their_tools(self):
        cases = {"set a timer for ten minutes": "set_timer", "turn the volume up a little": "volume",
                 "what's the weather like tomorrow": "get_weather", "remember that my car is on level 2": "remember",
                 "what do you remember about me": "list_memories", "take a screenshot": "take_screenshot",
                 "find my resume pdf": "find_files", "what time is it in tokyo": "get_datetime"}
        for request, tool in cases.items():
            names = tool_select.pick(self.box, request)
            self.assertIn(tool, names, request)
            self.assertLessEqual(len(names), 16, request)
            self.assertLess(self.size(names), 3000, request)

    def test_chat_gets_no_tools(self):
        for request in ("tell me a joke", "how are you", "thanks, that was great"):
            self.assertEqual(tool_select.pick(self.box, request), [], request)

    def test_remember_only_when_asked(self):
        for request in ("what's my favourite colour", "my favourite colour is", "write a note that the meeting moved"):
            self.assertNotIn("remember", tool_select.pick(self.box, request), request)

    def test_follow_ups_keep_the_last_tools(self):
        self.assertIn("volume", tool_select.pick(self.box, "and a bit more", previous=["volume"]))

    def test_fit_drops_tools_then_old_messages(self):
        p = Recorder([])
        p.messages = [{"role": "user", "content": "x" * 4000}, {"role": "assistant", "content": "ok"}] * 3 + \
                     [{"role": "user", "content": "now this"}]
        schemas = [self.box.get(n).schema(self.box.cfg) for n in list(self.box.tools)[:40]]
        kept = tool_select.fit(p, schemas, num_ctx=4096)
        self.assertGreater(len(kept), 5)                   # old chat went first, not this request's tools
        total = tool_select.estimate_tokens(p.system_prompt, [{"type": "function", "function": t} for t in kept],
                                            p.messages)
        self.assertLessEqual(total, 4096 - 1024)
        self.assertEqual(p.messages[-1]["content"], "now this")
        self.assertEqual(p.messages[0]["role"], "user")


class AgentTests(unittest.TestCase):
    def test_local_brain_is_offered_only_matching_tools(self):
        p = Recorder([Turn(text="Why did the scarecrow win an award? He was outstanding in his field.")])
        agent_with(p).handle("tell me a joke")
        self.assertEqual(p.offered, [[]])

        p = Recorder([Turn(tool_calls=[ToolCall("1", "set_timer", {"minutes": 10, "label": "oven"})]),
                      Turn(text="Timer set.")])
        agent = agent_with(p)
        agent.cfg["router"] = {"enabled": False}          # make the brain do it, not the fast path
        agent.handle("set a timer for ten minutes for the oven")
        self.assertIn("set_timer", p.offered[0])
        self.assertLess(len(p.offered[0]), 16)
        self.assertEqual(agent._recent_tools, ["set_timer"])

    def test_claude_still_gets_everything(self):
        p = Recorder([Turn(text="Hello.")])
        p.name = "claude"
        p.set_tools([{"name": "a", "description": "", "parameters": {}}])
        agent_with(p).handle("hi")
        self.assertEqual(p.offered, [["a"]])

    def test_made_up_memories_are_refused(self):
        d = tempfile.mkdtemp()
        cfg = load_config()
        cfg["memory"] = {"facts_file": os.path.join(d, "m.json"), "history_file": os.path.join(d, "h.jsonl")}
        mem = Memory(cfg)
        # the model decides to "remember" something the user never asked it to keep
        p = Recorder([Turn(tool_calls=[ToolCall("1", "remember", {"fact": "favourite colour is blue"})]),
                      Turn(text="Got it.")])
        agent_with(p, mem).handle("what colour should I paint the lights")
        self.assertEqual(mem.facts, [])
        p = Recorder([Turn(tool_calls=[ToolCall("1", "remember", {"fact": "car is on level 2"})]), Turn(text="ok")])
        agent_with(p, mem).handle("remember that my car is on level 2")
        self.assertEqual([f["fact"] for f in mem.facts], ["car is on level 2"])

    def test_memory_prompt_and_forget(self):
        d = tempfile.mkdtemp()
        cfg = load_config()
        cfg["memory"] = {"facts_file": os.path.join(d, "m.json"), "history_file": os.path.join(d, "h.jsonl")}
        mem = Memory(cfg)
        mem.remember("favourite colour is blue")
        self.assertIn("never bring them up otherwise", mem.facts_rule())
        self.assertEqual(mem.forget("my favourite colour"), "Forgotten 1 item.")
        with open(os.path.join(d, "m.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])

    def test_no_example_values_in_remember(self):
        schema = ToolBox(load_config(), {}).get("remember").schema()
        self.assertNotIn("colour", json.dumps(schema).lower())


def memory_in(d) -> Memory:
    cfg = load_config()
    cfg["memory"] = {"facts_file": os.path.join(d, "m.json"), "history_file": os.path.join(d, "h.jsonl")}
    return Memory(cfg)


class FactRetrievalTests(unittest.TestCase):
    """Only the remembered facts a request is about reach the brain, as a note on that request."""

    def setUp(self):
        self.mem = memory_in(tempfile.mkdtemp())
        for fact in ("car is parked on level 2", "favourite colour is blue", "sister is called Sara",
                     "allergic to peanuts", "the wifi password is fish"):
            self.mem.remember(fact)

    def test_a_request_finds_the_facts_it_is_about(self):
        self.assertEqual(self.mem.relevant_facts("where did I park"), ["car is parked on level 2"])
        self.assertEqual(self.mem.relevant_facts("what's my sister's name"), ["sister is called Sara"])
        self.assertEqual(self.mem.relevant_facts("can I eat this peanut butter"), ["allergic to peanuts"])
        self.assertEqual(self.mem.relevant_facts("what's the wi-fi password"), ["the wifi password is fish"])

    def test_misheard_and_other_spellings(self):
        self.assertEqual(self.mem.relevant_facts("what's my favorite color"), ["favourite colour is blue"])

    def test_unrelated_requests_get_no_facts(self):
        for request in ("tell me a joke", "what time is it", "what's your favourite movie", "how are you",
                        "what do you know about black holes", "what's using my memory"):
            self.assertEqual(self.mem.facts_note(request), "", request)

    def test_asking_what_jarvis_knows_gets_them_all_capped(self):
        everything = self.mem.relevant_facts("what do you know about me")
        self.assertEqual(len(everything), 5)
        self.assertEqual(everything[0], "the wifi password is fish")          # newest first
        self.assertEqual(len(self.mem.relevant_facts("what do you remember about me")), 5)
        for i in range(30):
            self.mem.remember(f"fact number {i}")
        self.assertEqual(len(self.mem.relevant_facts("what have I told you")), 20)

    def test_note_goes_to_the_brain_but_the_log_keeps_the_users_words(self):
        p = Recorder([Turn(text="On level 2.")])
        agent = agent_with(p, self.mem)
        agent.cfg["router"] = {"enabled": False}
        before = agent._system_prompt()
        agent.handle("where did I park")
        self.assertEqual(p.messages[0]["content"], "where did I park\n\n[Memory: car is parked on level 2]")
        with open(self.mem.history_path, encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readlines()[-1])["user"], "where did I park")
        self.assertNotIn("level 2", before)                    # facts never sit in the system prompt...
        self.mem.remember("dentist on friday")
        self.assertEqual(agent._system_prompt(), before)        # ...so it stays the same (Ollama reuses its cache)

    def test_a_full_context_is_reported(self):
        from unittest import mock

        from jarvis.llm.ollama import OllamaProvider

        class Reply:
            status_code, text = 200, ""

            def __init__(self, data=None, lines=()):
                self.data, self.lines = data, lines

            def json(self):
                return self.data

            def raise_for_status(self):
                pass

            def iter_lines(self):
                return iter(self.lines)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        p = OllamaProvider(system_prompt="s", tools=[], num_ctx=8192)
        full = {"message": {"role": "assistant", "content": "Hi."}, "done": True, "prompt_eval_count": 8192}
        with mock.patch("jarvis.llm.ollama.requests.post", return_value=Reply(full)), \
                self.assertLogs("jarvis.llm.base", "WARNING") as logs:
            p.add_user("hi")
            p.step()
        self.assertIn("filled the 8192-token context", logs.output[0])
        lines = [json.dumps({"message": {"content": "Hi."}}), json.dumps({**full, "message": {}})]
        with mock.patch("jarvis.llm.ollama.requests.post", return_value=Reply(lines=lines)), \
                self.assertLogs("jarvis.llm.base", "WARNING"):
            p.step(on_text=lambda s: None)
        roomy = {**full, "prompt_eval_count": 2500}
        with mock.patch("jarvis.llm.ollama.requests.post", return_value=Reply(roomy)), \
                self.assertNoLogs("jarvis.llm.base", "WARNING"):
            p.step()

    def test_small_talk_reaches_the_brain_unchanged(self):
        p = Recorder([Turn(text="Why did the chicken cross the road?")])
        agent_with(p, self.mem).handle("tell me a joke")
        self.assertEqual(p.messages[0]["content"], "tell me a joke")


if __name__ == "__main__":
    unittest.main()
