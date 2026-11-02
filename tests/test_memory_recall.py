"""Memory that keeps itself tidy: newer facts replace older ones about the same thing, "tomorrow" is stored as a
date and dropped once it has passed, plain statements about the user are kept, and earlier conversations can be
searched by topic or by time."""
import datetime as dt
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from jarvis.config import load_config
from jarvis.llm.base import LLMProvider, Turn
from jarvis.memory import Memory, absolute_dates, subject_of, time_window
from jarvis.tools import ToolBox


def memory_in(d, **extra) -> Memory:
    cfg = load_config()
    cfg["memory"] = {"facts_file": os.path.join(d, "m.json"), "history_file": os.path.join(d, "h.jsonl"), **extra}
    return Memory(cfg)


class Chat(LLMProvider):
    name = "ollama"

    def __init__(self, turns):
        super().__init__(system_prompt="You are Jarvis.", tools=[])
        self.turns = list(turns)
        self.num_ctx = 8192

    def step(self, use_tools=True, on_text=None):
        return self.turns.pop(0)

    def add_tool_results(self, results):
        pass


def agent_with(provider, memory):
    from jarvis.agent import Agent

    agent = Agent.__new__(Agent)
    agent.cfg = load_config()
    agent.cfg["router"] = {"enabled": False}
    agent.confirm = lambda q: True
    agent.toolbox = ToolBox(agent.cfg, {"memory": memory, "last_gap": None})
    agent.memory = memory
    agent._lock = threading.Lock()
    agent._pending_switch = None
    agent._last_activity = time.monotonic()
    agent.provider = provider
    return agent


class DateTests(unittest.TestCase):
    MONDAY = dt.date(2026, 11, 2)

    def test_relative_days_become_dates(self):
        self.assertEqual(absolute_dates("dentist tomorrow", self.MONDAY),
                         ("dentist on Tuesday 3 November", dt.date(2026, 11, 3)))
        self.assertEqual(absolute_dates("meeting on friday at 3", self.MONDAY)[0], "meeting on Friday 6 November at 3")
        self.assertEqual(absolute_dates("party tonight", self.MONDAY)[0], "party on the evening of Monday 2 November")
        self.assertEqual(absolute_dates("call mum next monday", self.MONDAY)[1], dt.date(2026, 11, 9))
        self.assertEqual(absolute_dates("favourite colour is blue", self.MONDAY), ("favourite colour is blue", None))

    def test_past_days_are_not_given_an_end(self):
        fact, day = absolute_dates("went to the gym yesterday", self.MONDAY)
        self.assertEqual(fact, "went to the gym on Sunday 1 November")
        self.assertIsNone(day)

    def test_dated_facts_go_once_the_day_has_passed(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "m.json"), "w", encoding="utf-8") as f:
            json.dump([{"fact": "dentist on Tuesday 3 November", "added": "2020-11-02", "expires": "2020-11-04"},
                       {"fact": "favourite colour is blue", "added": "2020-11-02"}], f)
        self.assertEqual([f["fact"] for f in memory_in(d).facts], ["favourite colour is blue"])

    def test_remember_stores_the_date_and_when_to_drop_it(self):
        mem = memory_in(tempfile.mkdtemp())
        mem.remember("dentist tomorrow")
        tomorrow = dt.date.today() + dt.timedelta(days=1)
        self.assertIn(f"{tomorrow.day} {tomorrow:%B}", mem.facts[0]["fact"])
        self.assertEqual(mem.facts[0]["expires"], (tomorrow + dt.timedelta(days=1)).isoformat())


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.mem = memory_in(tempfile.mkdtemp())

    def test_subjects(self):
        self.assertEqual(subject_of("favourite colour is blue"), ["favourite", "colour"])
        self.assertEqual(subject_of("my sister is called Sara"), ["sister", "name"])
        self.assertEqual(subject_of("car is parked on level 2"), ["car", "parked"])
        self.assertEqual(subject_of("allergic to peanuts"), [])

    def test_a_newer_fact_replaces_the_old_one(self):
        self.mem.remember("favourite colour is blue")
        reply = self.mem.remember("favorite color is green")
        self.assertIn("instead of favourite colour is blue", reply)
        self.assertEqual([f["fact"] for f in self.mem.facts], ["favorite color is green"])

    def test_different_things_about_one_subject_are_both_kept(self):
        self.mem.remember("car is parked on level 2")
        self.mem.remember("car is red")
        self.assertEqual(len(self.mem.facts), 2)

    def test_the_least_useful_fact_goes_when_full(self):
        mem = memory_in(tempfile.mkdtemp(), max_facts=3)
        for fact in ("gate code is 4321", "dog is called Max", "gym is on Tuesdays"):
            mem.remember(fact)
        mem.relevant_facts("what's the gate code")            # used: worth keeping
        mem.remember("boss is called Ali")
        facts = [f["fact"] for f in mem.facts]
        self.assertIn("gate code is 4321", facts)
        self.assertNotIn("dog is called Max", facts)
        self.assertIn("boss is called Ali", facts)
        self.assertEqual(mem.facts[0]["uses"], 1)


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.mem = memory_in(tempfile.mkdtemp())

    def test_plain_statements_about_the_user_are_kept(self):
        cases = {"I live in Lahore": "lives in Lahore",
                 "I'm allergic to cats, what should I do": "allergic to cats",
                 "by the way I work as a software engineer": "works as a software engineer",
                 "I’m vegetarian": "is vegetarian",
                 "my birthday is on the 5th of March": "birthday is the 5th of March"}
        for said, fact in cases.items():
            self.assertEqual(self.mem.notice(said), fact, said)
        self.assertEqual(self.mem.facts[0].get("source"), "noticed")

    def test_requests_questions_and_hypotheticals_are_not(self):
        for said in ("type I live in Lahore", "do I live in Lahore?", "if I lived in Paris I'd be happy",
                     "my friend said I work at Google", "search for I'm allergic to gluten",
                     "write a note that I live in Karachi now", "what's the weather"):
            self.assertIsNone(self.mem.notice(said), said)
        self.assertEqual(self.mem.facts, [])

    def test_can_be_switched_off(self):
        mem = memory_in(tempfile.mkdtemp(), auto_remember=False)
        self.assertIsNone(mem.notice("I live in Lahore"))

    def test_the_agent_notices_after_answering(self):
        agent = agent_with(Chat([Turn(text="Lovely city.")]), self.mem)
        agent.handle("I live in Lahore")
        self.assertEqual([f["fact"] for f in self.mem.facts], ["lives in Lahore"])
        agent.provider.turns = [Turn(text="It's in Lahore.")]
        agent.handle("where do I live")
        self.assertIn("[Memory: lives in Lahore]", agent.provider.messages[-1]["content"])


class SemanticTests(unittest.TestCase):
    def test_meaning_match_when_no_words_are_shared(self):
        mem = memory_in(tempfile.mkdtemp(), semantic=True, vectors_file=os.path.join(tempfile.mkdtemp(), "v.json"))
        mem.remember("allergic to peanuts")
        mem.remember("gym is on Tuesdays")
        vectors = {"allergic to peanuts": [1, 0, 0], "gym is on Tuesdays": [0, 1, 0],
                   "can I have a snickers bar": [0.9, 0.1, 0.1], "tell me a joke": [0, 0, 1]}
        with mock.patch.object(Memory, "_embed", side_effect=lambda texts: [vectors[t] for t in texts]):
            self.assertEqual(mem.relevant_facts("can I have a snickers bar"), ["allergic to peanuts"])
            self.assertEqual(mem.relevant_facts("tell me a joke"), [])

    def test_a_missing_model_turns_it_off_quietly(self):
        mem = memory_in(tempfile.mkdtemp(), semantic=True, vectors_file=os.path.join(tempfile.mkdtemp(), "v.json"))
        mem.remember("allergic to peanuts")
        with mock.patch("requests.post", side_effect=OSError("connection refused")), \
                self.assertLogs("jarvis.memory", "WARNING"):
            self.assertEqual(mem.relevant_facts("can I have a snickers bar"), [])
        self.assertFalse(mem.semantic)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.mem = memory_in(tempfile.mkdtemp())
        self.now = dt.datetime(2026, 11, 2, 15, 0).timestamp()
        rows = [(self.now - 86400 - 3600, "play some jazz", "Playing jazz on Spotify."),
                (self.now - 86400, "what's a good sci-fi book", "Try Project Hail Mary."),
                (self.now - 3 * 3600, "recommend a song for running", "Try Eye of the Tiger."),
                (self.now - 60, "what's the weather", "Sunny and 24 degrees.")]
        with open(self.mem.history_path, "w", encoding="utf-8") as f:
            for t, user, assistant in rows:
                f.write(json.dumps({"t": t, "user": user, "assistant": assistant, "brain": "x"}) + "\n")

    def users(self, query):
        return [r["user"] for r in self.mem.search_history(query, now=self.now)]

    def test_by_topic(self):
        self.assertEqual(self.users("what was that song you recommended"), ["recommend a song for running"])
        self.assertEqual(self.users("the book you suggested"), ["what's a good sci-fi book"])

    def test_by_time(self):
        self.assertEqual(self.users("what did we talk about yesterday"),
                         ["what's a good sci-fi book", "play some jazz"])
        self.assertEqual(self.users("what did I ask you this morning")[0], "what's the weather")

    def test_time_window(self):
        start, end = time_window("yesterday", self.now)
        self.assertEqual(dt.datetime.fromtimestamp(start).date(), dt.date(2026, 11, 1))
        self.assertEqual(time_window("the jazz thing", self.now)[0], 0.0)

    def test_recall_tool(self):
        box = ToolBox(load_config(), {"memory": self.mem})
        with mock.patch("time.time", return_value=self.now):
            out = box.run("recall_conversation", {"query": "that song you recommended"})
        self.assertIn("Eye of the Tiger", out)
        self.assertNotIn("jazz", out)

    def test_log_stays_bounded(self):
        mem = memory_in(tempfile.mkdtemp(), history_max_mb=0.001)        # about 1 KB
        for i in range(40):
            mem.log_exchange(f"request {i}", "reply " * 5, "x")
        self.assertLess(os.path.getsize(mem.history_path), 2048)
        with open(mem.history_path, encoding="utf-8") as f:
            self.assertIn("request 39", f.readlines()[-1])


if __name__ == "__main__":
    unittest.main()
