"""Direct Gigabyte RGB Fusion 2 control against a fake lighting chip (no hardware needed)."""
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis import fusion, router
from jarvis.config import Section, load_config
from jarvis.tools import ToolBox
from jarvis.tools import rgb_tools as rt
from jarvis.tools.builtin import ToolFailed


class FakeChip:
    """Answers the CC 60 info request like an IT5702 on a B660M board and records every report."""

    def __init__(self, flags=0x02, broken=False):
        self.sent, self.flags, self.broken, self.closed = [], flags, broken, False

    def send(self, report):
        if self.broken and report[1] != 0x60:
            raise OSError("the lighting chip refused a command (error 31)")
        self.sent.append(bytes(report))

    def read(self, report_id):
        info = bytearray(64)
        info[0], info[11] = report_id, self.flags
        info[12:12 + 17] = b"B660M GAMING DDR4"
        return bytes(info)

    def close(self):
        self.closed = True


def effects(sent):
    return [p for p in sent if 0x20 <= p[1] <= 0x27 or 0x90 <= p[1] <= 0x92]


class PacketTests(unittest.TestCase):
    def test_static_colour_packet(self):
        p = fusion.effect_packet(0x21, 0x02, fusion.Look("static", (255, 128, 0), brightness=50))
        self.assertEqual(len(p), 64)
        self.assertEqual(p[:2], b"\xcc\x21")
        self.assertEqual(struct.unpack_from("<II", p, 2), (0x02, 0))
        self.assertEqual((p[11], p[12], p[13]), (fusion.STATIC, 128, 0))       # mode, max/min brightness
        self.assertEqual(p[14:18], bytes([0, 128, 255, 0]))                    # sent blue, green, red
        self.assertEqual(set(p[34:]), {0})

    def test_off_is_dark_static(self):
        p = fusion.effect_packet(0x20, 1, fusion.Look("breathing", (255, 0, 0), on=False))
        self.assertEqual((p[11], p[12]), (fusion.STATIC, 0))
        self.assertEqual(p[14:18], bytes(4))

    def test_speed_changes_timing(self):
        slow = fusion.effect_packet(0x20, 1, fusion.Look("breathing", speed=1))
        fast = fusion.effect_packet(0x20, 1, fusion.Look("breathing", speed=10))
        self.assertGreater(struct.unpack_from("<H", slow, 22)[0], struct.unpack_from("<H", fast, 22)[0])
        cycle = fusion.effect_packet(0x20, 1, fusion.Look("color cycle"))
        self.assertEqual((cycle[11], cycle[30]), (fusion.COLOR_CYCLE, 7))
        double = fusion.effect_packet(0x20, 1, fusion.Look("double flash"))
        self.assertEqual((double[11], double[31], double[32]), (fusion.FLASHING, 1, 2))

    def test_zones_per_chip(self):
        self.assertEqual(len(fusion.led_slots(0x5702)), 8)
        self.assertEqual(fusion.led_slots(0x5711)[-1], (0x92, 1 << 10))
        self.assertEqual(fusion.apply_packet(0x5702)[:4], b"\xcc\x28\xff\x00")
        self.assertEqual(fusion.apply_packet(0x5711)[:4], b"\xcc\x28\xff\x07")

    def test_show_sends_every_zone_then_applies(self):
        chip = FakeChip()
        board = fusion.Fusion(chip, 0x5702)
        self.assertEqual(board.name, "B660M GAMING DDR4")
        chip.sent.clear()
        board.show(fusion.Look("static", (0, 140, 255)))
        self.assertEqual(chip.sent[0][:3], b"\xcc\x48\x00")                  # Dynamic Lighting off
        self.assertEqual(chip.sent[1][:3], b"\xcc\x31\x00")                  # music beat off
        self.assertEqual([p[1] for p in effects(chip.sent)], list(range(0x20, 0x28)))
        self.assertEqual(chip.sent[-1][:2], b"\xcc\x28")

    def test_effect_names(self):
        self.assertEqual(fusion.effect_name("rainbow"), "color cycle")
        self.assertEqual(fusion.effect_name("Breathing mode"), "breathing")
        self.assertIsNone(fusion.effect_name("fireworks"))


class ToolTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.chip = FakeChip()
        for patcher in (mock.patch.object(fusion, "STATE_FILE", Path(d.name) / "lights.json"),
                        mock.patch.object(rt, "_fusion_chip", side_effect=lambda: fusion.Fusion(self.chip, 0x5702)),
                        mock.patch.object(rt, "_openrgb_quietly")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def last(self):
        return effects(self.chip.sent)[-1]

    def test_colour_brightness_off_on(self):
        self.assertEqual(rt.rgb("color", "red"), "Lights set to red.")
        self.assertEqual(self.last()[14:17], bytes([0, 0, 255]))
        self.assertEqual(rt.rgb("brightness", "down"), "Lights at 75 percent.")
        self.assertEqual(self.last()[12], round(255 * 0.75))
        self.assertEqual(rt.rgb("off"), "Lights off.")
        self.assertEqual(self.last()[12], 0)
        self.assertEqual(rt.rgb("on"), "Lights on.")
        self.assertEqual(self.last()[14:17], bytes([0, 0, 255]))             # red again, as it was
        self.assertTrue(self.chip.closed)

    def test_effects_and_speed(self):
        self.assertEqual(rt.rgb("effect", "slow breathing"), "Breathing on.")
        self.assertEqual((self.last()[11], fusion.current().speed), (fusion.BREATHING, 2))
        rt.rgb("effect", "rainbow")
        self.assertEqual(self.last()[11], fusion.COLOR_CYCLE)
        rt.rgb("color", "purple")                      # a colour ends the rainbow
        self.assertEqual(fusion.current().effect, "static")
        with self.assertRaisesRegex(ToolFailed, "can't do fireworks"):
            rt.rgb("effect", "fireworks")

    def test_profiles(self):
        rt.rgb("color", "jarvis blue")
        self.assertIn("Saved", rt.rgb("save_profile", "Gaming"))
        rt.rgb("color", "red")
        self.assertEqual(rt.rgb("profile", "gaming"), "Loaded the Gaming lighting profile.")
        self.assertEqual(self.last()[14:17], bytes([255, 140, 0]))
        self.assertIn("B660M GAMING DDR4", rt.rgb("status"))

    def test_all_lights_also_tries_openrgb_devices(self):
        rt.rgb("color", "red")
        rt._openrgb_quietly.assert_called_once()
        rt._openrgb_quietly.reset_mock()
        rt.rgb("color", "red", "motherboard")
        rt._openrgb_quietly.assert_not_called()


class FallbackTests(unittest.TestCase):
    def test_a_broken_chip_falls_back_to_openrgb(self):
        chip = FakeChip(broken=True)
        with mock.patch.object(rt, "_fusion_chip", return_value=fusion.Fusion(chip, 0x5702)), \
                mock.patch.object(rt, "_openrgb", return_value="Lights set to red.") as orgb, \
                mock.patch.object(rt, "_client"):
            self.assertEqual(rt.rgb("color", "red"), "Lights set to red.")
        orgb.assert_called_once()

    def test_openrgb_backend_never_touches_the_chip(self):
        box = SimpleNamespace(cfg=Section({**load_config(), "rgb": {"backend": "openrgb"}}))
        with mock.patch.object(rt, "_fusion_chip") as chip, mock.patch.object(rt, "_client"), \
                mock.patch.object(rt, "_openrgb", return_value="ok"):
            rt.rgb("color", "red", ctx=box)
        chip.assert_not_called()

    def test_brightness_needs_the_board(self):
        with mock.patch.object(rt, "_fusion_chip", return_value=None):
            with self.assertRaisesRegex(ToolFailed, "brightness"):
                rt.rgb("brightness", "50")

    def test_not_windows_means_not_found(self):
        with mock.patch.object(fusion.sys, "platform", "linux"):
            with self.assertRaises(fusion.NotFound):
                fusion.find()


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = ToolBox(load_config())

    def r(self, text):
        return router.route(text, self.box)

    def test_phrases(self):
        self.assertEqual(self.r("open the lights panel"), [("rgb", {"action": "panel"})])
        self.assertEqual(self.r("open the RGB fusion controller"), [("rgb", {"action": "panel"})])
        self.assertEqual(self.r("dim the lights"), [("rgb", {"action": "brightness", "value": "down"})])
        self.assertEqual(self.r("set the lights brightness to 40"), [("rgb", {"action": "brightness", "value": "40"})])
        self.assertEqual(self.r("lights slow breathing"),
                         [("rgb", {"action": "effect", "value": "slow breathing", "device": ""})])
        self.assertEqual(self.r("brightness 50")[0][0], "brightness")                  # still the screen


if __name__ == "__main__":
    unittest.main()
