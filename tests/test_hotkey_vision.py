import ctypes
import time
import unittest
from unittest import mock

from jarvis.config import load_config
from jarvis.hotkey import WM_HOTKEY, Hotkey, parse


class HotkeyTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse("ctrl+alt+j"), (0x2 | 0x1, ord("J")))
        self.assertEqual(parse("Win+Shift+F9"), (0x8 | 0x4, 0x78))
        with self.assertRaises(ValueError):
            parse("hyper+j")

    def test_message_sets_event(self):
        # an unusual combo so we don't clash with the user's real shortcuts; we post WM_HOTKEY ourselves
        hk = Hotkey("ctrl+alt+shift+f12").start()
        deadline = time.time() + 2
        while hk._thread_id is None and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)
        ctypes.windll.user32.PostThreadMessageW(hk._thread_id, WM_HOTKEY, 1, 0)
        self.assertTrue(hk.pressed.wait(2))
        hk.stop()


class VisionTests(unittest.TestCase):
    def test_ollama_request_carries_image(self):
        from jarvis import vision

        resp = mock.MagicMock()
        resp.json.return_value = {"message": {"content": "A blank screen."}}
        with mock.patch.object(vision.requests, "post", return_value=resp) as post:
            out = vision.describe(load_config(), b"\xff\xd8fakejpeg", "what is this?")
        self.assertEqual(out, "A blank screen.")
        body = post.call_args.kwargs["json"]
        self.assertTrue(body["messages"][0]["images"][0])
        self.assertIn("what is this?", body["messages"][0]["content"])

    def test_screen_tool_needs_screen_words(self):
        from jarvis.tools import ToolBox

        t = ToolBox(load_config()).get("look_at_screen")
        self.assertTrue(t.allowed_for("what does this error on my screen say"))
        self.assertFalse(t.allowed_for("set a timer for ten minutes"))


if __name__ == "__main__":
    unittest.main()
