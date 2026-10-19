"""Any Ollama model as the brain: discovery, auto pick, fallback, misheard names, think/vision, choosing by voice.
Ollama's API is faked, so no Ollama or network is needed."""
import io
import json
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import requests

from jarvis import router
from jarvis.config import load_config
from jarvis.llm import ollama_models as om
from jarvis.llm.ollama import OllamaProvider
from jarvis.tools import ToolBox

C = ["completion"]
MODELS = {   # name -> size in bytes, parameter_size, family, capabilities (as /api/tags and /api/show report them)
    "deepseek-r1:8b": (5.2e9, "8.2B", "qwen3", C + ["thinking"]),
    "gemma3:4b": (3.3e9, "4.3B", "gemma3", C + ["vision"]),
    "llama3.1:8b": (4.9e9, "8.0B", "llama", C + ["tools"]),
    "llama3.2:3b": (2.0e9, "3.2B", "llama", C + ["tools"]),
    "mistral:7b": (4.1e9, "7.2B", "llama", C + ["tools"]),
    "nomic-embed-text:latest": (0.27e9, "137M", "nomic-bert", ["embedding"]),
    "qwen3:8b": (5.2e9, "8.2B", "qwen3", C + ["tools", "thinking"]),
}


class FakeResponse:
    def __init__(self, data=None, status=200, text=None):
        self.status_code, self._data = status, data
        self.text = text if text is not None else json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(self.text)

    def iter_lines(self):
        return iter([json.dumps({"message": self._data["message"], "done": True})])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOllama:
    """Answers /api/tags, /api/show and /api/chat like Ollama does and records every chat request."""

    def __init__(self, names=None, up=True):
        self.models = {n: MODELS[n] for n in (names or MODELS)}
        self.up = up
        self.chats, self.shows = [], 0

    def get(self, url, **kw):
        if not self.up:
            raise requests.ConnectionError("refused")
        assert url.endswith("/api/tags"), url
        return FakeResponse({"models": [{"name": n, "model": n, "size": s, "digest": f"sha-{n}",
                                         "details": {"parameter_size": p, "family": f}}
                                        for n, (s, p, f, _) in self.models.items()]})

    def post(self, url, json=None, **kw):
        if not self.up:
            raise requests.ConnectionError("refused")
        if url.endswith("/api/show"):
            self.shows += 1
            _, p, f, caps = self.models[json["model"]]
            return FakeResponse({"capabilities": caps, "details": {"parameter_size": p, "family": f}})
        self.chats.append(dict(json))
        return FakeResponse({"message": {"role": "assistant", "content": "Ready."}})

    def __enter__(self):
        self._patch = mock.patch.multiple(requests, get=self.get, post=self.post)
        self._patch.start()
        return self

    def __exit__(self, *a):
        self._patch.stop()


def models(names=None):
    with FakeOllama(names):
        return om.list_models()


class _Clean(unittest.TestCase):
    def setUp(self):
        om._SHOW.clear()
        om._LAST.clear()

    tearDown = setUp


class DiscoveryTests(_Clean):
    def test_lists_models_with_sizes_and_capabilities(self):
        with FakeOllama() as fake:
            found = om.list_models("http://localhost:11434")
            om.list_models()
        self.assertEqual([m["name"] for m in found], sorted(MODELS))
        llama = om.find(found, "llama3.2:3b")
        self.assertEqual((llama["params"], llama["family"], llama["size"]), ("3.2B", "llama", 2_000_000_000))
        self.assertIn("tools", llama["capabilities"])
        self.assertEqual(om.find(found, "nomic-embed-text")["capabilities"], ["embedding"])   # ":latest" optional
        self.assertEqual(fake.shows, len(MODELS))         # /api/show once per model, then cached
        self.assertEqual(om.known_models(), found)

    def test_ollama_not_running(self):
        with FakeOllama(up=False), self.assertRaises(om.OllamaUnavailable) as e:
            om.list_models()
        self.assertIn("Start the Ollama app", str(e.exception))

    def test_capability_marks(self):
        found = models()
        self.assertEqual(om.features(om.find(found, "gemma3:4b")), "vision, no tools")
        self.assertEqual(om.features(om.find(found, "qwen3:8b")), "tools, thinking")
        self.assertEqual(om.features(om.find(found, "nomic-embed-text")), "embeddings only")
        self.assertEqual(om.say_name("llama3.2:3b"), "llama 3.2 3B")
        self.assertEqual(om.say_name("deepseek-r1:8b"), "deepseek r1 8B")


class ChoosingTests(_Clean):
    def test_auto_picks_a_tool_model_of_a_sensible_size(self):
        found = models()
        self.assertEqual(om.best_model(found)["name"], "llama3.2:3b")             # tools, nearest ~4B
        self.assertEqual(om.best_model(found, "qwen3.5:9b")["name"], "qwen3:8b")   # nearest the size you had
        self.assertEqual(om.best_model(found, "mistral:7b")["name"], "mistral:7b")
        self.assertIsNone(om.best_model(models(["nomic-embed-text:latest"])))      # embeddings can't chat
        self.assertEqual(om.best_model(models(["gemma3:4b", "deepseek-r1:8b"]))["name"], "gemma3:4b")

    def test_configured_model_missing_falls_back_with_a_note(self):
        pick, note = om.resolve(models(), "qwen3.5:4b")
        self.assertEqual(pick["name"], "llama3.2:3b")
        self.assertIn("qwen3.5:4b isn't pulled, using llama3.2:3b", note)
        self.assertIn("--model", note)
        self.assertNotIn("ollama pull", note)       # only said when nothing usable is installed
        self.assertEqual(om.resolve(models(), "auto"), (om.find(models(), "llama3.2:3b"), ""))

    def test_nothing_usable_says_what_to_pull(self):
        with mock.patch.object(om, "suggestion", return_value="qwen3.5:4b"):
            pick, note = om.resolve(models(["nomic-embed-text:latest"]), "auto")
            self.assertIsNone(pick)
            self.assertIn("ollama pull qwen3.5:4b", note)
            self.assertEqual(om.resolve([], "llama3.2:3b")[1], "No Ollama chat model is installed. "
                                                               "Run: ollama pull llama3.2:3b")

    def test_provider_falls_back_instead_of_failing(self):
        with FakeOllama():
            p = OllamaProvider(system_prompt="s", tools=[], model="qwen3.5:4b")
            self.assertIsNone(p.check())
        self.assertEqual((p.model, p.installed, p.label), ("llama3.2:3b", len(MODELS), "ollama:llama3.2:3b"))
        self.assertIn("isn't pulled", p.note)
        with FakeOllama(["nomic-embed-text:latest"]), mock.patch.object(om, "suggestion", return_value="qwen3.5:2b"):
            p = OllamaProvider(system_prompt="s", tools=[])
            self.assertIn("ollama pull qwen3.5:2b", p.check())
        with FakeOllama(up=False):
            self.assertIn("isn't running", OllamaProvider(system_prompt="s", tools=[]).check())

    def test_model_without_tools_still_chats(self):
        with FakeOllama() as fake:
            p = OllamaProvider(system_prompt="s", tools=[{"name": "t", "description": "d", "parameters": {}}],
                               model="gemma3:4b")
            self.assertIsNone(p.check())
            p.add_user("hi")
            p.step()
        self.assertEqual(fake.chats[-1]["tools"], [])
        self.assertIn("can't use tools", p.note)

    def test_setup_pulls_only_when_no_installed_model_can_use_tools(self):
        with FakeOllama():
            self.assertEqual(om.model_to_pull(), "")
        with FakeOllama(["gemma3:4b"]), mock.patch.object(om, "suggestion", return_value="qwen3.5:4b"):
            self.assertEqual(om.model_to_pull(), "qwen3.5:4b")


class FuzzyNameTests(_Clean):
    def test_misheard_names_find_the_installed_tag(self):
        found = models()
        for spoken, tag in [("llama", "llama3.2:3b"), ("Llama 3.1", "llama3.1:8b"),
                            ("llama three point one", "llama3.1:8b"), ("the llama one", "llama3.2:3b"),
                            ("lama", "llama3.2:3b"), ("gemma three", "gemma3:4b"), ("jemma", "gemma3:4b"),
                            ("gamma 3", "gemma3:4b"), ("mistral", "mistral:7b"), ("mistrial", "mistral:7b"),
                            ("deep seek", "deepseek-r1:8b"), ("deepsea", "deepseek-r1:8b"),
                            ("quen 3", "qwen3:8b"), ("qwen", "qwen3:8b"), ("llama 8b", "llama3.1:8b")]:
            self.assertEqual(om.fuzzy_pick(found, spoken), tag, spoken)

    def test_refuses_a_bad_or_ambiguous_match(self):
        found = models()
        for spoken in ("banana", "photoshop", "dark mode", "3", "nomic"):    # nomic: embeddings, not a brain
            self.assertIsNone(om.fuzzy_pick(found, spoken), spoken)
        coders = [{"name": n, "size": 0, "params": "7B", "family": "", "capabilities": C + ["tools"]}
                  for n in ("qwen2.5-coder:7b", "deepseek-coder:6.7b")]
        self.assertIsNone(om.fuzzy_pick(coders, "coder"))                   # two different models: ask
        self.assertEqual(om.fuzzy_pick(coders, "qwen coder"), "qwen2.5-coder:7b")

    def test_strict_needs_a_whole_name(self):
        found = models()
        self.assertEqual(om.fuzzy_pick(found, "gemma", strict=True), "gemma3:4b")
        self.assertEqual(om.fuzzy_pick(found, "quen 3", strict=True), "qwen3:8b")
        self.assertIsNone(om.fuzzy_pick(found, "deepsea", strict=True))
        self.assertIsNone(om.fuzzy_pick(found, "male voice", strict=True))


class ThinkAndVisionTests(_Clean):
    def test_think_is_sent_only_to_thinking_models(self):
        for tag, thinks in (("qwen3:8b", True), ("llama3.2:3b", False), ("deepseek-r1:8b", True)):
            with FakeOllama() as fake:
                p = OllamaProvider(system_prompt="s", tools=[], model=tag)
                p.check()
                p.add_user("hi")
                p.step()
                p.step(on_text=lambda s: None)
            for body in fake.chats:
                self.assertEqual("think" in body, thinks, tag)
                if thinks:
                    self.assertIs(body["think"], False)

    def test_unknown_model_still_retries_without_think(self):
        calls = []

        def post(url, json=None, **kw):
            calls.append(dict(json))
            if "think" in json:
                return FakeResponse(text='{"error":"model does not support thinking"}', status=400)
            return FakeResponse({"message": {"role": "assistant", "content": "Hi."}})
        p = OllamaProvider(system_prompt="s", tools=[], model="mystery:1b")   # never checked: capabilities unknown
        with mock.patch.object(requests, "post", post):
            p.add_user("hi")
            self.assertEqual(p.step().text, "Hi.")
        self.assertEqual(["think" in c for c in calls], [True, False])

    def _cfg(self, brain="auto", vision=None):
        cfg = load_config()
        cfg["llm"]["providers"]["ollama"]["model"] = brain
        cfg["vision"] = {**(cfg.get("vision") or {}), "provider": "ollama", "model": vision}
        return cfg

    def test_vision_picks_a_model_that_can_see(self):
        from jarvis import vision

        with FakeOllama():
            self.assertEqual(vision.vision_model(self._cfg(vision="llama3.2:3b"))[0], "llama3.2:3b")  # your choice
            self.assertEqual(vision.vision_model(self._cfg(brain="gemma3:4b"))[0], "gemma3:4b")       # the brain sees
            self.assertEqual(vision.vision_model(self._cfg(brain="llama3.2:3b"))[0], "gemma3:4b")     # any that sees
            with self.assertLogs("jarvis.vision", "WARNING"):
                self.assertEqual(vision.vision_model(self._cfg(vision="llava:7b"))[0], "gemma3:4b")   # not pulled
        with FakeOllama(["llama3.2:3b", "qwen3:8b"]):
            from jarvis.tools.builtin import ToolFailed

            with self.assertRaises(ToolFailed) as e:
                vision.vision_model(self._cfg())
        self.assertIn("can see images", str(e.exception))

    def test_screen_question_goes_to_the_seeing_model_without_think(self):
        from jarvis import vision

        with FakeOllama() as fake:
            self.assertEqual(vision.describe(self._cfg(brain="llama3.2:3b"), b"\xff\xd8jpeg", "what is this?"), "Ready.")
        body = fake.chats[-1]
        self.assertEqual(body["model"], "gemma3:4b")
        self.assertNotIn("think", body)              # gemma3 has no thinking switch
        self.assertTrue(body["messages"][0]["images"][0])


class RouterTests(_Clean):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        cls.box = ToolBox(cls.cfg)

    def r(self, text):
        return router.route(text, self.box, self.cfg)

    def test_model_questions(self):
        for text in ("Which model are you using?", "what model are you running", "what's your model",
                     "which brain are you using right now"):
            self.assertEqual(self.r(text), [("list_models", {"what": "current"})], text)
        for text in ("What models do I have?", "which models are installed", "list my ollama models",
                     "show me the installed models"):
            self.assertEqual(self.r(text), [("list_models", {"what": "installed"})], text)

    def test_switching_by_name(self):
        self.assertEqual(self.r("Switch to the llama model"),
                         [("switch_model", {"provider": "local", "model": "llama"})])
        self.assertEqual(self.r("use the gemma 3 model instead"),
                         [("switch_model", {"provider": "local", "model": "gemma 3"})])
        self.assertEqual(self.r("change the model to mistral"),
                         [("switch_model", {"provider": "local", "model": "mistral"})])
        self.assertEqual(self.r("switch to the claude model"), [("switch_model", {"provider": "claude"})])
        self.assertIsNone(self.r("use gemma"))               # nothing known about installed models yet
        models()                                             # Jarvis has listed them (at start-up)
        self.assertEqual(self.r("use gemma"), [("switch_model", {"provider": "local", "model": "gemma"})])
        self.assertEqual(self.r("switch to quen 3"), [("switch_model", {"provider": "local", "model": "quen 3"})])
        self.assertIsNone(self.r("switch to the male voice"))  # still not a model
        self.assertIsNone(self.r("use dark mode"))


class ProviderStub:
    """The brain before the switch (a real OllamaProvider is made for the new model)."""
    name, model, label = "ollama", "qwen3:8b", "ollama:qwen3:8b"

    def __init__(self):
        self.messages = []

    def add_user(self, t):
        self.messages.append(t)

    def add_assistant_text(self, t):
        self.messages.append(t)

    def check(self):
        return None


class VoiceSwitchTests(_Clean):
    def _agent(self):
        from jarvis.agent import Agent

        a = Agent.__new__(Agent)
        a.cfg = load_config()
        a.cfg["llm"]["provider"] = "ollama"
        a.confirm = lambda q: True
        a.toolbox = ToolBox(a.cfg, {"switch_model": a._request_switch, "brain": lambda: a.provider})
        a.memory = None
        a._pending_switch = None
        a._lock = threading.RLock()
        a._last_activity = time.monotonic()
        a.provider = ProviderStub()
        return a

    def test_switch_to_a_spoken_model_after_the_reply(self):
        a = self._agent()
        with FakeOllama(), mock.patch("jarvis.agent.save_local") as save:
            reply = a.handle("Switch to the llama model")
        self.assertTrue(reply.startswith("Switching to llama 3.2 3B after this reply."), reply)
        self.assertIn("Now using ollama:llama3.2:3b.", reply)
        self.assertIsInstance(a.provider, OllamaProvider)
        self.assertEqual(a.provider.model, "llama3.2:3b")
        save.assert_called_once_with({"llm": {"providers": {"ollama": {"model": "llama3.2:3b"}}}})
        self.assertEqual(a.cfg["llm"]["providers"]["ollama"]["model"], "llama3.2:3b")   # vision follows it too

    def test_use_gemma_warns_it_cannot_use_tools(self):
        a = self._agent()
        models()
        with FakeOllama(), mock.patch("jarvis.agent.save_local"), self.assertLogs("jarvis.agent", "WARNING") as logs:
            reply = a.handle("use gemma")
        self.assertEqual(a.provider.model, "gemma3:4b")
        self.assertIn("can't use tools", reply)
        self.assertIn("can't use tools", logs.output[0])

    def test_unknown_name_is_refused(self):
        a = self._agent()
        with FakeOllama(), mock.patch("jarvis.agent.save_local") as save:
            reply = a.handle("switch to the banana model")
        self.assertIn("None of your Ollama models sounds like banana", reply)
        self.assertIn("llama 3.2 3B", reply)
        self.assertIsInstance(a.provider, ProviderStub)
        save.assert_not_called()

    def test_already_using_and_provider_switches_still_work(self):
        a = self._agent()
        with FakeOllama():
            self.assertEqual(a._request_switch("local", "quen 3"), "Already using qwen 3 8B.")
            self.assertEqual(a.toolbox.run("switch_model", {"provider": "claude"}),
                             "Switching to claude after this reply.")
            self.assertEqual(a._pending_switch, ("claude", None))
            self.assertEqual(a._request_switch("local", "claude"), "Switching to claude after this reply.")
            self.assertEqual(a._request_switch("claude", "opus"), "Switching to claude after this reply.")

    def test_which_model_and_what_models(self):
        a = self._agent()
        with FakeOllama():
            self.assertEqual(a.handle("which model are you using?"),
                             "I'm using qwen 3 8B through Ollama. You have 6 local models installed.")
            reply = a.handle("what models do I have")
        self.assertTrue(reply.startswith("You have 6 local models: deepseek r1 8B (no tools), gemma 3 4B (no tools), "
                                         "llama 3.1 8B,"), reply)
        self.assertNotIn("nomic", reply)


class CliAndWizardTests(_Clean):
    def test_models_lists_and_marks_the_active_one(self):
        cfg = load_config()
        cfg["llm"]["providers"]["ollama"]["model"] = "qwen3.5:4b"
        out = io.StringIO()
        with FakeOllama(), redirect_stdout(out):
            self.assertEqual(om.print_models(cfg), 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(any(line.startswith(" * llama3.2:3b") and "tools" in line for line in lines), out.getvalue())
        self.assertTrue(any(line.startswith("   gemma3:4b") and "vision, no tools" in line for line in lines))
        self.assertIn("qwen3.5:4b isn't pulled, using llama3.2:3b", out.getvalue())

    def test_model_flag_saves_a_fuzzy_match(self):
        with FakeOllama(), mock.patch("jarvis.config.save_local") as save, redirect_stdout(io.StringIO()):
            save.return_value.name = "config.local.yaml"
            self.assertEqual(om.choose_model(load_config(), "gemma three"), 0)
            save.assert_called_once_with({"llm": {"provider": "ollama", "providers": {"ollama": {"model": "gemma3:4b"}}}})
            self.assertEqual(om.choose_model(load_config(), "banana"), 1)
            self.assertEqual(om.choose_model(load_config(), "qwen3.5:9b"), 1)     # an exact tag that isn't pulled
        self.assertEqual(save.call_count, 1)

    def test_wizard_lists_installed_models_and_takes_a_number(self):
        from jarvis import setup_wizard

        found = models()
        out = io.StringIO()
        with mock.patch.object(setup_wizard, "list_models", return_value=found), redirect_stdout(out):
            self.assertEqual(setup_wizard._choose_model("qwen3.5:4b", {}), "llama3.2:3b")   # Enter = recommended
            self.assertEqual(setup_wizard._choose_model("qwen3.5:4b", {"model": "2"}), "mistral:7b")
            self.assertEqual(setup_wizard._choose_model("qwen3.5:4b", {"model": "deep seek"}), "deepseek-r1:8b")
        self.assertIn("llama3.2:3b", out.getvalue().split("recommended")[0])
        self.assertNotIn("ollama pull", out.getvalue())
        out = io.StringIO()
        with mock.patch.object(setup_wizard, "list_models", return_value=[]), redirect_stdout(out):
            self.assertEqual(setup_wizard._choose_model("qwen3.5:2b", {}), "auto")
        self.assertIn("ollama pull qwen3.5:2b", out.getvalue())       # the suggestion only when nothing's there


if __name__ == "__main__":
    unittest.main()
