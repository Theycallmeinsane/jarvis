"""Windows control tools. Nothing here changes the PC: key presses, registry writes and windows are mocked,
except read-only calls (listing windows and processes)."""
import unittest
from unittest import mock

from jarvis.config import load_config
from jarvis.tools import ToolBox
from jarvis.tools import windows


class KeyParsingTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(windows.parse_keys("ctrl+shift+esc"), [0x11, 0x10, 0x1B])
        self.assertEqual(windows.parse_keys("Win + Left"), [0x5B, 0x25])
        self.assertEqual(windows.parse_keys("f5"), [0x74])
        self.assertEqual(windows.parse_keys("ctrl+c"), [0x11, ord("C")])
        with self.assertRaises(ValueError):
            windows.parse_keys("ctrl+banana")

    def test_send_keys_presses_then_releases_in_reverse(self):
        events = []
        with mock.patch.object(windows, "user32") as u32:
            u32.keybd_event.side_effect = lambda vk, scan, flags, extra: events.append((vk, flags & 2))
            windows.send_keys("win+shift+s")
        self.assertEqual(events, [(0x5B, 0), (0x10, 0), (ord("S"), 0), (ord("S"), 2), (0x10, 2), (0x5B, 2)])


class ReadOnlyWindowsTests(unittest.TestCase):
    def test_list_windows_and_processes(self):
        box = ToolBox(load_config())
        self.assertIsInstance(box.run("list_windows", {}), str)
        out = box.run("top_processes", {"sort": "memory", "count": 3})
        self.assertEqual(out.count(" MB"), 3)

    def test_closing_a_window_always_asks_and_names_it(self):
        box = ToolBox(load_config())
        self.assertNotIn("close", box.get("window_action").parameters["properties"]["action"]["enum"])
        tool = box.get("close_window")
        self.assertTrue(tool.dangerous)
        with mock.patch.object(windows, "find_window", return_value=(1, "report.docx - Word")):
            self.assertIn("report.docx - Word", tool.confirmation({"name": "word"}))
            self.assertIn("unsaved", tool.confirmation({"name": "word"}))

    def test_unknown_window(self):
        self.assertIn("No open window", ToolBox(load_config()).run(
            "window_action", {"name": "zz-no-such-window-zz", "action": "focus"}))


class ThemeTests(unittest.TestCase):
    def test_toggle_writes_both_values_and_broadcasts(self):
        import winreg

        writes = {}
        key = mock.MagicMock()
        key.__enter__.return_value = key
        with mock.patch.object(winreg, "OpenKey", return_value=key), \
                mock.patch.object(winreg, "QueryValueEx", return_value=(1, 4)), \
                mock.patch.object(winreg, "SetValueEx", side_effect=lambda k, n, r, t, v: writes.update({n: v})), \
                mock.patch.object(windows, "_broadcast_theme_change") as bc:
            self.assertEqual(windows.theme("toggle"), "Dark mode on.")
        self.assertEqual(writes, {"AppsUseLightTheme": 0, "SystemUsesLightTheme": 0})
        bc.assert_called_once()


class ReadAloudTests(unittest.TestCase):
    def test_reads_given_text_through_speak_hook(self):
        spoken = []
        box = ToolBox(load_config(), {"speak": spoken.append})
        out = box.run("read_aloud", {"source": "text", "text": "Hello there. General Kenobi."})
        self.assertEqual(spoken, ["Hello there. General Kenobi."])
        self.assertIn("Reading it aloud", out)

    def test_triggers(self):
        box = ToolBox(load_config())
        self.assertTrue(box.get("read_aloud").allowed_for("read my clipboard to me"))
        self.assertTrue(box.get("window_action").allowed_for("snap chrome to the left"))
        self.assertFalse(box.get("press_keys").allowed_for("what's the weather"))
        self.assertTrue(box.get("theme").allowed_for("switch to dark mode"))

    def test_disabled_tools_are_removed(self):
        cfg = load_config()
        cfg["tools"] = {**cfg["tools"], "disabled": ["wifi", "theme"]}
        names = {s["name"] for s in ToolBox(cfg).schemas()}
        self.assertNotIn("wifi", names)
        self.assertIn("open_settings", names)


if __name__ == "__main__":
    unittest.main()
