"""Offline tests for memory, Google tools (fake API), confirmations and the tray helpers."""
import base64
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis.config import Section, load_config
from jarvis.memory import Memory
from jarvis.tools import ToolBox


def _cfg_with_tmp(tmp: Path) -> Section:
    cfg = load_config()
    cfg["memory"] = {"enabled": True, "facts_file": str(tmp / "m.json"), "history_file": str(tmp / "h.jsonl"),
                     "restore_turns": 4, "restore_within_hours": 1}
    return cfg


class MemoryTests(unittest.TestCase):
    def test_facts_persist_and_forget(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg_with_tmp(Path(d))
            m = Memory(cfg)
            m.remember("his sister is called Sara.")
            self.assertIn("already", m.remember("His sister is called Sara"))
            self.assertIn("Sara", Memory(cfg).facts_prompt())          # survives a restart
            self.assertIn("Forgotten 1", Memory(cfg).forget("sara"))
            self.assertEqual(Memory(cfg).facts, [])

    def test_history_seeds_new_conversation(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg_with_tmp(Path(d))
            m = Memory(cfg)
            for i in range(6):
                m.log_exchange(f"q{i}", f"a{i}", "test")
            seed = Memory(cfg).seed_messages()
            self.assertEqual([x["content"] for x in seed][:2], ["q2", "a2"])   # last 4 exchanges
            self.assertEqual(len(seed), 8)

    def test_old_history_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg_with_tmp(Path(d))
            m = Memory(cfg)
            m.log_exchange("old", "reply", "test")
            rows = m.history_path.read_text(encoding="utf-8").replace(str(int(time.time()))[:5], "10000")
            m.history_path.write_text(rows, encoding="utf-8")
            self.assertEqual(Memory(cfg).seed_messages(), [])

    def test_memory_tools(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg_with_tmp(Path(d))
            box = ToolBox(cfg, {"memory": Memory(cfg)})
            box.run("remember", {"fact": "likes his coffee black"})
            self.assertIn("coffee", box.run("list_memories", {}))


# ---------------------------------------------------------------- fake Google API

class _Req:
    def __init__(self, result, log, name, kwargs):
        self.result, self.log, self.name, self.kwargs = result, log, name, kwargs

    def execute(self):
        self.log.append((self.name, self.kwargs))
        return self.result(self.kwargs) if callable(self.result) else self.result


class _Res:
    def __init__(self, routes, log, prefix=""):
        self.routes, self.log, self.prefix = routes, log, prefix

    def __getattr__(self, attr):
        path = f"{self.prefix}.{attr}" if self.prefix else attr

        def call(**kwargs):
            if path in self.routes:
                return _Req(self.routes[path], self.log, path, kwargs)
            return _Res(self.routes, self.log, path)
        return call


def _b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode()


class GoogleToolTests(unittest.TestCase):
    def setUp(self):
        from jarvis.tools import google_tools

        self.g = google_tools
        self.log = []
        msg = {"id": "m1", "snippet": "Lunch tomorrow?", "payload": {
            "headers": [{"name": "From", "value": "Sara Khan <sara@example.com>"},
                        {"name": "To", "value": "me@example.com"}, {"name": "Subject", "value": "Lunch"},
                        {"name": "Date", "value": "Fri"}],
            "mimeType": "multipart/alternative",
            "parts": [{"mimeType": "text/plain", "body": {"data": _b64("Are you free at 1pm?")}}]}}
        google_tools._services.clear()
        google_tools._services["gmail"] = _Res({
            "users.messages.list": {"messages": [{"id": "m1"}]},
            "users.messages.get": msg,
            "users.messages.send": {"id": "sent"},
            "users.drafts.create": {"id": "d1"},
        }, self.log)
        google_tools._services["calendar"] = _Res({
            "events.list": {"items": [{"summary": "Standup", "start": {"dateTime": "2026-10-17T09:30:00+05:00"}}]},
            "events.insert": lambda kw: kw["body"],
        }, self.log)
        cfg = load_config()
        self.box = ToolBox(cfg, {})
        # google tools are hidden without credentials; register them directly for the test
        from jarvis.tools.registry import _TOOLS
        for name in ("gmail_unread", "gmail_read", "gmail_send", "gmail_draft", "gmail_find_address",
                     "calendar_upcoming", "calendar_add_event"):
            self.box.tools[name] = _TOOLS[name]

    def tearDown(self):
        self.g._services.clear()

    def test_unread_and_read(self):
        self.assertIn("From Sara Khan: Lunch", self.box.run("gmail_unread", {}))
        self.assertIn("Are you free at 1pm?", self.box.run("gmail_read", {"message_id": "m1"}))

    def test_find_address(self):
        self.assertIn("sara@example.com", self.box.run("gmail_find_address", {"name": "Sara"}))

    def test_send_builds_mime_and_needs_confirmation(self):
        tool = self.box.get("gmail_send")
        self.assertTrue(tool.dangerous)
        q = tool.confirmation({"to": "sara@example.com", "subject": "Lunch", "body": "Yes, 1pm works."})
        self.assertIn("sara@example.com", q)
        self.assertIn("1pm works", q)
        self.box.run("gmail_send", {"to": "sara@example.com", "subject": "Lunch", "body": "Yes"})
        raw = next(kw for name, kw in self.log if name == "users.messages.send")["body"]["raw"]
        self.assertIn(b"To: sara@example.com", base64.urlsafe_b64decode(raw).replace(b"to:", b"To:"))

    def test_send_rejects_names(self):
        with self.assertRaises(ValueError):
            self.box.run("gmail_send", {"to": "Sara", "subject": "x", "body": "y"})

    def test_calendar(self):
        self.assertIn("Standup", self.box.run("calendar_upcoming", {"days": 2}))
        out = self.box.run("calendar_add_event", {"title": "Dentist", "start": "2026-10-20 15:00",
                                                  "duration_minutes": 30})
        self.assertIn("Dentist", out)
        body = next(kw for name, kw in self.log if name == "events.insert")["body"]
        self.assertTrue(body["end"]["dateTime"].startswith("2026-10-20T15:30"))

    def test_hidden_without_credentials(self):
        names = [s["name"] for s in ToolBox(load_config()).schemas()]
        if not self.g._paths(load_config())[0].exists():
            self.assertNotIn("gmail_send", names)


class TrayTests(unittest.TestCase):
    def test_icon_and_launch_command(self):
        from jarvis import tray

        for state in tray.STATE_COLOURS:
            self.assertEqual(tray._icon_image(state).size, (64, 64))
        cmd = tray._launch_command()
        self.assertIn("pythonw.exe", cmd)
        self.assertIn("jarvis_tray.pyw", cmd)


if __name__ == "__main__":
    unittest.main()
