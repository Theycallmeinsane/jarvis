"""RGB control against a fake OpenRGB client (no SDK server or hardware needed)."""
import unittest
from types import SimpleNamespace
from unittest import mock

from jarvis import router
from jarvis.config import load_config
from jarvis.tools import ToolBox
from jarvis.tools import rgb_tools as rt
from jarvis.tools.builtin import ToolFailed


class FakeDevice:
    def __init__(self, name, modes, dtype=None):
        self.name, self.type = name, dtype
        self.modes = [SimpleNamespace(name=m) for m in modes]
        self.calls = []

    def set_mode(self, m):
        self.calls.append(("mode", m))

    def set_color(self, c):
        self.calls.append(("color", (c.red, c.green, c.blue)))

    def off(self):
        self.calls.append(("off",))


class FakeClient:
    def __init__(self):
        self.devices = [FakeDevice("B660M DS3H AX DDR4", ["Direct", "Static", "Breathing", "Spectrum Cycle"]),
                        FakeDevice("ENE DRAM", ["Direct", "Rainbow Wave", "Breathing"])]
        self.profiles = [SimpleNamespace(name="Movie Night")]
        self.loaded = None

    def load_profile(self, n):
        self.loaded = n

    def save_profile(self, n):
        self.loaded = n

    def disconnect(self):
        pass


class RGBTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        patcher = mock.patch.object(rt, "_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_colours(self):
        self.assertEqual(rt.parse_color("dim blue"), (0, 0, 76))
        self.assertEqual(rt.parse_color("#FF8800"), (255, 136, 0))
        self.assertEqual(rt.parse_color("purpel"), rt.COLORS["purple"])     # misheard
        with self.assertRaises(ValueError):
            rt.parse_color("happiness")

    def test_set_colour_all_devices(self):
        self.assertEqual(rt.rgb("color", "red"), "Lights set to red.")
        for d in self.client.devices:
            self.assertIn(("mode", "Direct"), d.calls)
            self.assertIn(("color", (255, 0, 0)), d.calls)

    def test_ram_only_and_effects(self):
        rt.rgb("effect", "rainbow", "ram")
        self.assertEqual(self.client.devices[1].calls, [("mode", "Rainbow Wave")])
        self.assertEqual(self.client.devices[0].calls, [])
        out = rt.rgb("effect", "rainbow")
        self.assertIn("2 devices", out)
        self.assertIn(("mode", "Spectrum Cycle"), self.client.devices[0].calls)
        with self.assertRaisesRegex(ToolFailed, "support"):
            rt.rgb("effect", "fireworks")

    def test_off_and_profiles(self):
        self.assertEqual(rt.rgb("off"), "Lights off.")
        self.assertEqual(rt.rgb("profile", "movie night"), "Loaded the Movie Night lighting profile.")
        self.assertEqual(self.client.loaded, "Movie Night")


class NotRunningTests(unittest.TestCase):
    def test_explains_conflict_with_vendor_app(self):
        with mock.patch("openrgb.OpenRGBClient", side_effect=ConnectionRefusedError), \
                mock.patch.object(rt, "_conflicting_apps", return_value=["Gigabyte RGB Fusion"]):
            with self.assertRaisesRegex(ToolFailed, "RGB Fusion is also running"):
                rt.rgb("color", "red")


class RouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        cls.box = ToolBox(cls.cfg)

    def r(self, t):
        return router.route(t, self.box, self.cfg)

    def test_phrases(self):
        self.assertEqual(self.r("set the lights to red"), [("rgb", {"action": "color", "value": "red", "device": ""})])
        self.assertEqual(self.r("make the RAM rgb purple"),
                         [("rgb", {"action": "color", "value": "purple", "device": "ram"})])
        self.assertEqual(self.r("rgb rainbow mode"), [("rgb", {"action": "effect", "value": "rainbow", "device": ""})])
        self.assertEqual(self.r("turn off the lights"), [("rgb", {"action": "off"})])
        self.assertEqual(self.r("lights off"), [("rgb", {"action": "off", "device": ""})])
        self.assertEqual(self.r("load the movie night lighting profile"),
                         [("rgb", {"action": "profile", "value": "movie night"})])


if __name__ == "__main__":
    unittest.main()
