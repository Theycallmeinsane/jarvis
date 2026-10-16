import datetime as dt
import unittest

from jarvis.config import Section, load_config
from jarvis.routines import Scheduler, find, run_steps
from jarvis.tools import ToolBox


def _cfg(routines):
    cfg = load_config()
    cfg["routines"] = routines
    return cfg


class RoutineTests(unittest.TestCase):
    def test_find_is_fuzzy(self):
        cfg = _cfg({"Good Morning": {"steps": []}, "work mode": {"steps": []}})
        self.assertEqual(find(cfg, "good mornin")[0], "good morning")
        self.assertEqual(find(cfg, "WORK MODE")[0], "work mode")
        self.assertIsNone(find(cfg, "launch rockets"))

    def test_run_steps_reports_failures_without_stopping(self):
        cfg = _cfg({})
        box = ToolBox(cfg)
        out = run_steps(box, {"steps": ["get_datetime", {"no_such_tool": {}}, {"read_notes": {"count": 1}}]})
        self.assertTrue(out[0].startswith("get_datetime: "))
        self.assertEqual(out[1], "no_such_tool: not available")
        self.assertTrue(out[2].startswith("read_notes: "))

    def test_run_routine_tool(self):
        cfg = _cfg({"status": {"say": "All systems.", "steps": ["get_datetime"]}})
        box = ToolBox(cfg)
        self.assertIn("status", box.get("run_routine").schema(cfg)["description"])
        out = box.run("run_routine", {"name": "status"})
        self.assertIn("summarize", out)
        self.assertIn("get_datetime:", out)

    def test_scheduler_fires_once_on_matching_day(self):
        cfg = _cfg({"brief": {"steps": [], "schedule": {"time": "08:00", "days": ["mon", "tue"]}},
                    "never": {"steps": []}})
        s = Scheduler(cfg, lambda n: None)
        monday = dt.datetime(2026, 10, 19, 8, 0, 30)
        self.assertEqual(s.due(monday), ["brief"])
        self.assertEqual(s.due(monday + dt.timedelta(seconds=40)), [])           # already ran today
        self.assertEqual(s.due(dt.datetime(2026, 10, 21, 8, 0, 30)), [])        # wednesday
        self.assertEqual(s.due(dt.datetime(2026, 10, 20, 7, 59, 0)), [])        # too early
        self.assertEqual(s.due(dt.datetime(2026, 10, 20, 8, 1, 0)), ["brief"])  # tuesday


if __name__ == "__main__":
    unittest.main()
