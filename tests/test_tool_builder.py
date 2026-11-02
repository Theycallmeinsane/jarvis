"""The self-improving loop: when Jarvis can't do something it offers to write a tool; the code is reviewed, its tests
run in a sandbox, failures go back to the model, and only a tool the user approved is switched on (and only while
its code is unchanged). The coding model is faked here; the sandbox is real."""
import os
import tempfile
import threading
import time
import unittest

from jarvis.config import load_config
from jarvis.llm.base import LLMProvider, Turn
from jarvis.tools import ToolBox, generated
from jarvis.toolsmith import Build, Toolsmith, module_name, parse, review

TOOL = '''"""Shouting."""
from __future__ import annotations

from ..builtin import ToolFailed
from ..registry import tool


@tool("Say text back in capitals.", {"text": {"type": "string"}}, triggers=("shout",), keywords="shout capitals")
def shout_text(text: str, ctx=None):
    if not text.strip():
        raise ToolFailed("There was nothing to shout.")
    return text.upper() + "!"
'''

TESTS = '''import unittest

from jarvis.tools.builtin import ToolFailed
from jarvis.tools.generated import MODULE as mod


class Tests(unittest.TestCase):
    def test_shouts(self):
        self.assertEqual(mod.shout_text("hi"), "HI!")

    def test_empty(self):
        with self.assertRaises(ToolFailed):
            mod.shout_text("  ")

    def test_already_loud(self):
        self.assertEqual(mod.shout_text("HEY"), "HEY!")
'''

SANDBOX_TEST = '''
    def test_the_sandbox(self):
        import os
        import socket
        import subprocess
        home_file = os.path.join(os.path.expanduser("~"), "Documents", "ok.txt")
        with open(home_file, "w") as f:          # "home" is inside the sandbox: allowed
            f.write("x")
        with self.assertRaises(PermissionError):
            open(OUTSIDE, "w")
        with self.assertRaises(PermissionError):
            subprocess.run(["python", "--version"])
        with self.assertRaises(PermissionError):
            socket.create_connection(("93.184.216.34", 80), timeout=1)
        self.assertEqual([k for k in os.environ if "KEY" in k.upper() or "TOKEN" in k.upper()], [])
'''


def reply(tool=TOOL, tests=TESTS, summary="It shouts text back in capitals."):
    return f"```python\n# file: tool.py\n{tool}```\n\n```python\n# file: test_tool.py\n{tests}```\nSUMMARY: {summary}\n"


class ParseTests(unittest.TestCase):
    def test_two_files_and_a_summary(self):
        d = parse(reply())
        self.assertIn("def shout_text", d.tool_code)
        self.assertIn("class Tests", d.test_code)
        self.assertEqual(d.summary, "It shouts text back in capitals.")

    def test_existing_tool(self):
        self.assertEqual(parse("EXISTING: get_weather").existing, "get_weather")

    def test_module_names(self):
        self.assertEqual(module_name("Build a tool that converts currencies"), "converts_currencies")
        self.assertEqual(module_name("check crypto prices for me"), "check_crypto_prices")


class ReviewTests(unittest.TestCase):
    def check(self, tool=TOOL, tests=TESTS.replace("MODULE", "probe"), taken=()):
        return review(tool, tests, "probe", set(taken))

    def test_good_code_passes(self):
        r = self.check()
        self.assertEqual(r.problems, [])
        self.assertEqual(r.tools, ["shout_text"])
        self.assertEqual(r.risks, [])

    def test_forbidden_things(self):
        cases = {
            "eval(": TOOL.replace("text.upper()", "eval(text)"),
            "importing pickle": TOOL.replace("from __future__ import annotations", "import pickle"),
            "secrets": TOOL.replace('"Shouting."', '"Shouting."\nKEYFILE = ".env"'),
            "imported": TOOL + "\nprint('hello')\n",
            "shell=True": TOOL.replace("return text.upper()", "import subprocess\n    subprocess.run('dir', shell=True)"
                                                              "\n    return text.upper()"),
            "only the tool registry": TOOL.replace("from ..registry import tool",
                                                   "from ..registry import tool\nfrom ...agent import Agent"),
        }
        for expected, code in cases.items():
            problems = " ".join(self.check(code).problems)
            self.assertIn(expected, problems, expected)

    def test_risky_code_must_ask_first(self):
        deleting = TOOL.replace("return text.upper()", "import os\n    os.remove(text)\n    return text.upper()")
        r = self.check(deleting)
        self.assertIn("deletes files", r.risks)
        self.assertTrue(any("dangerous=True" in p for p in r.problems))
        r = self.check(deleting.replace('triggers=("shout",)', 'triggers=("shout",), dangerous=True'))
        self.assertEqual(r.problems, [])
        self.assertIn("deletes files", r.risks)

    def test_harmless_string_methods_are_not_risky(self):
        code = TOOL.replace("return text.upper()", 'text = text.replace("a", "b")\n    return text.upper()')
        self.assertEqual(self.check(code).risks, [])

    def test_names_and_tests(self):
        self.assertIn("already exists", " ".join(self.check(taken=["shout_text"]).problems))
        few = TESTS.replace("MODULE", "probe").split("    def test_empty")[0]
        self.assertIn("three test methods", " ".join(self.check(tests=few).problems))
        self.assertIn("must import the tool", " ".join(self.check(tests=TESTS.replace("MODULE", "other")).problems))


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = load_config()
        self.cfg["mcp_servers"] = None
        self.cfg["toolsmith"] = {"approvals_file": os.path.join(self.dir, "approvals.json"),
                                 "gaps_file": os.path.join(self.dir, "gaps.jsonl"), "max_attempts": 3}
        self.box = ToolBox(self.cfg, {})
        self.prompts = []
        self.replies = []
        self.smith = Toolsmith(self.cfg, self.box, ask_model=self.ask)
        self.module = self.smith._free_module(module_name("zz probe shout"))
        self.outside = os.path.join(self.dir, "outside.txt")

    def tearDown(self):
        self.smith.remove(self.module, delete_files=True)

    def ask(self, system, prompt):
        self.prompts.append(prompt)
        return self.replies[len(self.prompts) - 1].replace("MODULE", self.module).replace(
            "OUTSIDE", repr(self.outside))

    def test_review_and_test_failures_go_back_to_the_model(self):
        self.replies = [reply(tool=TOOL.replace("text.upper()", "eval(text)")),          # fails review
                        reply(tests=TESTS.replace('"HI!"', '"hi!"')),                    # fails its tests
                        reply(tests=TESTS + SANDBOX_TEST)]                               # passes
        build = self.smith.build("zz probe shout")
        self.assertTrue(build.ok, build.message)
        self.assertEqual((build.attempts, build.tools, build.tests_run), (3, ["shout_text"], 4))
        self.assertIn("eval", self.prompts[1])
        self.assertIn("AssertionError", self.prompts[2])
        self.assertFalse(os.path.exists(self.outside))           # the sandbox stopped the write

        # nothing is on until approved
        self.assertNotIn("shout_text", ToolBox(self.cfg, {}).tools)
        self.assertEqual(self.smith.install(build), "")
        self.assertEqual(self.box.run("shout_text", {"text": "hello"}), "HELLO!")
        self.assertIn("shout_text", ToolBox(self.cfg, {}).tools)                         # after a restart too
        self.assertEqual(self.smith.listing()[0]["status"], "on")

        # edited after approval: off until approved again
        path = generated.HERE / f"{self.module}.py"
        path.write_text(path.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
        with self.assertLogs("jarvis.tools.generated", "WARNING"):
            self.assertNotIn("shout_text", ToolBox(self.cfg, {}).tools)

    def test_gives_up_and_cleans_up(self):
        self.replies = [reply(tests=TESTS.replace('"HI!"', '"nope"'))] * 3
        build = self.smith.build("zz probe shout")
        self.assertFalse(build.ok)
        self.assertIn("3 times", build.message)
        self.assertFalse((generated.HERE / f"{self.module}.py").exists())
        self.assertEqual(self.smith.gaps()[0]["request"], "zz probe shout")

    def test_an_existing_tool_is_pointed_out(self):
        self.replies = ["EXISTING: get_weather"]
        build = self.smith.build("tell me if it will rain")
        self.assertFalse(build.ok)
        self.assertEqual(build.existing, "get_weather")


class ModelChoiceTests(unittest.TestCase):
    def test_claude_writes_the_code_when_a_key_is_set(self):
        from types import SimpleNamespace
        from unittest import mock

        sent = []

        class FakeClaude:
            label, model, max_tokens, effort = "claude:test", "claude-test", 16000, "high"

            def _create(self, **kw):
                sent.append([m["role"] for m in kw["messages"]])
                self.kw = kw
                return SimpleNamespace(content=[SimpleNamespace(type="text", text="EXISTING: get_weather")])

        fake = FakeClaude()
        cfg = load_config()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), \
                mock.patch("jarvis.llm.create_provider", return_value=fake) as create:
            send, label = Toolsmith(cfg, ToolBox(cfg, {}))._model()
            self.assertEqual(send("write it"), "EXISTING: get_weather")
            send("fix it")
        self.assertEqual(create.call_args.args[0], "claude")
        self.assertEqual(sent, [["user"], ["user", "assistant", "user"]])
        self.assertNotIn("tools", fake.kw)


class Chat(LLMProvider):
    name = "ollama"

    def __init__(self, turns):
        super().__init__(system_prompt="You are Jarvis.", tools=[])
        self.turns, self.num_ctx = list(turns), 8192

    def step(self, use_tools=True, on_text=None):
        return self.turns.pop(0)

    def add_tool_results(self, results):
        pass


class FakeSmith:
    def __init__(self):
        self.built, self.installed, self.gaps_logged = [], [], []

    def log_gap(self, request, reply):
        self.gaps_logged.append(request)

    def build(self, request, progress=None):
        self.built.append(request)
        return Build(True, module="flight_prices", tools=["flight_prices"], summary="It checks flight prices",
                     tests_run=3)

    def install(self, build):
        self.installed.append(build.module)
        return ""

    def discard(self, build):
        pass


class LoopTests(unittest.TestCase):
    def agent(self, turns, answers=(True,)):
        from jarvis.agent import Agent

        answers = list(answers)
        agent = Agent.__new__(Agent)
        agent.cfg = load_config()
        agent.cfg["router"] = {"enabled": False}
        agent.confirm = lambda q: answers.pop(0)
        self.smith = FakeSmith()
        agent.toolbox = ToolBox(agent.cfg, {"memory": None, "last_gap": None, "toolsmith": self.smith,
                                            "confirm": agent.confirm})
        agent.memory = None
        agent._lock = threading.Lock()
        agent._pending_switch = None
        agent._last_activity = time.monotonic()
        agent.provider = Chat(turns)
        return agent

    def test_cant_then_yes_builds_and_switches_it_on(self):
        from jarvis.agent import OFFER

        agent = self.agent([Turn(text="I'm afraid I can't check flight prices.")])
        said = []
        reply = agent.handle("how much is a flight to dubai next week", on_text=said.append)
        self.assertTrue(reply.endswith(OFFER))
        self.assertIn(" " + OFFER, said)
        self.assertEqual(self.smith.gaps_logged, ["how much is a flight to dubai next week"])
        reply = agent.handle("yes please")
        self.assertEqual(self.smith.built, ["how much is a flight to dubai next week"])
        self.assertEqual(self.smith.installed, ["flight_prices"])
        self.assertTrue(reply.startswith("Done. It checks flight prices"), reply)

    def test_declining_throws_it_away(self):
        agent = self.agent([Turn(text="I can't do that yet.")], answers=[False])
        agent.handle("order me a pizza from the place down the road")
        self.assertEqual(agent.handle("build it"), "Okay, I've thrown it away.")
        self.assertEqual(self.smith.installed, [])

    def test_no_offer_when_a_tool_ran_or_the_answer_is_fine(self):
        agent = self.agent([Turn(text="Why did the chicken cross the road?")])
        self.assertNotIn("build", agent.handle("tell me a joke about chickens"))
        self.assertIsNone(agent.toolbox.context["last_gap"])
        # a late "yes" with no offer standing is just chat
        self.assertIsNone(agent._build_follow_up("yes"))

    def test_offer_can_be_switched_off(self):
        agent = self.agent([Turn(text="I can't check flight prices.")])
        agent.cfg["toolsmith"] = {"offer": False}
        self.assertEqual(agent.handle("how much is a flight to dubai"), "I can't check flight prices.")

    def test_manage_tools(self):
        cfg = load_config()
        d = tempfile.mkdtemp()
        cfg["toolsmith"] = {"gaps_file": os.path.join(d, "gaps.jsonl"), "approvals_file": os.path.join(d, "a.json")}
        box = ToolBox(cfg, {"confirm": lambda q: True})
        self.assertIn("Nothing so far", box.run("manage_tools", {"action": "gaps"}))
        Toolsmith(cfg, box).log_gap("order a pizza", "I can't.")
        self.assertIn("order a pizza", box.run("manage_tools", {"action": "gaps"}))


if __name__ == "__main__":
    unittest.main()
