"""System control tools. Read-only calls run for real; anything that changes the PC is mocked."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import router
from jarvis.config import load_config
from jarvis.tools import ToolBox
from jarvis.tools import system_ext as sx
from jarvis.tools.builtin import ToolFailed


class ReadOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = ToolBox(load_config())

    def test_stats_sections(self):
        self.assertIn("CPU", self.box.run("system_stats", {"section": "cpu"}))
        self.assertIn("memory", self.box.run("system_stats", {"section": "memory"}))
        self.assertIn("up for", self.box.run("system_stats", {"section": "uptime"}))

    def test_network_status_speaks(self):
        out = self.box.run("network_status", {})
        self.assertTrue(out.startswith(("Online", "No internet")))

    def test_startup_list_and_recycle_status(self):
        self.assertIn("start", self.box.run("list_startup_apps", {}).lower())
        self.assertIn("recycle bin", self.box.run("recycle_bin_status", {}).lower())

    def test_find_process(self):
        self.assertIn("python", self.box.run("find_process", {"name": "python"}).lower())
        self.assertIn("Nothing called", self.box.run("find_process", {"name": "zz-not-running-zz"}))


class SafetyTests(unittest.TestCase):
    def test_critical_processes_are_refused(self):
        fake = mock.MagicMock()
        fake.info = {"name": "lsass.exe", "pid": 700, "memory_info": None}
        with mock.patch.object(sx, "_procs_named", return_value=[fake]):
            with self.assertRaises(ToolFailed):
                sx.end_process("lsass")
        fake.terminate.assert_not_called()

    def test_end_process_confirms_with_names_and_count(self):
        a, b = mock.MagicMock(), mock.MagicMock()
        a.info = b.info = {"name": "chrome.exe"}
        with mock.patch.object(sx, "_procs_named", return_value=[a, b]):
            q = sx._end_question({"name": "chrome"})
        self.assertIn("chrome.exe (2 processes)", q)
        self.assertTrue(ToolBox(load_config()).get("end_process").dangerous)

    def test_critical_services_refused_and_admin_explained(self):
        svc = mock.MagicMock()
        svc.name.return_value, svc.display_name.return_value = "Dhcp", "DHCP Client"
        with mock.patch.object(sx, "_find_service", return_value=svc):
            with self.assertRaisesRegex(ToolFailed, "depends on it"):
                sx.service_control("dhcp", "stop")
        svc.name.return_value, svc.display_name.return_value = "Spooler", "Print Spooler"
        with mock.patch.object(sx, "_find_service", return_value=svc), mock.patch.object(sx, "is_admin", return_value=False):
            with self.assertRaisesRegex(ToolFailed, "administrator"):
                sx.service_control("spooler", "restart")

    def test_destructive_tools_all_ask_first(self):
        box = ToolBox(load_config())
        for name in ("end_process", "service_control", "set_startup_app", "empty_recycle_bin", "clear_temp_files",
                     "set_priority"):
            self.assertTrue(box.get(name).dangerous, name)

    def test_recycle_question_states_size(self):
        with mock.patch.object(sx, "_recycle_bin_info", return_value=(12, 5 * 2**20)):
            q = sx._empty_question({})
        self.assertIn("12 items", q)
        self.assertIn("5.0 MB", q)
        self.assertIn("can't be undone", q)

    def test_clear_temp_only_old_files_in_given_folder(self):
        with tempfile.TemporaryDirectory() as d:
            old, new = Path(d) / "old.tmp", Path(d) / "new.tmp"
            old.write_text("x" * 100)
            new.write_text("y")
            past = time.time() - 5 * 86400
            os.utime(old, (past, past))
            cfg = load_config()
            cfg["system"] = {"temp_dir": d}
            out = ToolBox(cfg).run("clear_temp_files", {})
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertIn("Removed 1", out)

    def test_airplane_mode_turns_radios_off(self):
        with mock.patch.object(sx, "_radios", return_value=[("Bluetooth", "bluetooth", "off")]) as r:
            out = sx.radio("airplane", "on")
        r.assert_called_once_with(None, "off")
        self.assertIn("Airplane mode on", out)
        with mock.patch.object(sx, "_radios", return_value=[]):
            with self.assertRaisesRegex(ToolFailed, "doesn't have a Wi-Fi adapter"):
                sx.radio("wifi", "on")


class RouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        cls.box = ToolBox(cls.cfg)

    def r(self, text):
        return router.route(text, self.box, self.cfg)

    def test_phrases(self):
        self.assertEqual(self.r("what's my CPU usage"), [("system_stats", {"section": "cpu"})])
        self.assertEqual(self.r("how hot is my GPU?"), [("system_stats", {"section": "gpu"})])
        self.assertEqual(self.r("how much RAM is left"), [("system_stats", {"section": "memory"})])
        self.assertEqual(self.r("system stats"), [("system_stats", {"section": "all"})])
        self.assertEqual(self.r("Am I online?"), [("network_status", {})])
        self.assertEqual(self.r("check my internet"), [("network_status", {})])
        self.assertEqual(self.r("brightness to 40"), [("brightness", {"action": "set", "percent": 40})])
        self.assertEqual(self.r("make the screen brighter"), [("brightness", {"action": "up"})])
        self.assertEqual(self.r("turn off bluetooth"), [("radio", {"radio": "bluetooth", "action": "off"})])
        self.assertEqual(self.r("airplane mode on"), [("radio", {"radio": "airplane", "action": "on"})])
        self.assertEqual(self.r("switch the sound to my headset"), [("audio_output", {"device": "headset"})])
        self.assertEqual(self.r("what's in the recycle bin"), [("recycle_bin_status", {})])
        self.assertEqual(self.r("list startup apps"), [("list_startup_apps", {})])
        self.assertIsNone(self.r("kill chrome"))            # risky -> brain + confirmation


if __name__ == "__main__":
    unittest.main()
