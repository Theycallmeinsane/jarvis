"""Movie search (fake files) and VLC control (a real VLC playing a short silent clip, if VLC is installed)."""
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from jarvis import router
from jarvis.config import load_config
from jarvis.tools import ToolBox
from jarvis.tools import vlc_tools as vt


class FindVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        names = ["The.Dark.Knight.2008.1080p.BluRay.x264-[YTS].mkv", "Inception (2010) [720p] [BluRay].mp4",
                 "Interstellar.2014.2160p.WEB-DL.mkv", "Friends/Season 2/Friends.S02E03.720p.mkv",
                 "Friends/Season 2/Friends.S02E04.720p.mkv", "notes.txt"]
        for n in names:
            p = root / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        cls.files = [p for p in root.rglob("*") if p.suffix.lower() in vt.VIDEO_EXT]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def find(self, q):
        best, _ = vt.find_video(q, self.files)
        return best.name if best else None

    def test_titles_survive_release_junk_and_whisper(self):
        self.assertEqual(self.find("the dark knight"), "The.Dark.Knight.2008.1080p.BluRay.x264-[YTS].mkv")
        self.assertEqual(self.find("inception"), "Inception (2010) [720p] [BluRay].mp4")
        self.assertEqual(self.find("the movie interstellar"), "Interstellar.2014.2160p.WEB-DL.mkv")
        self.assertEqual(self.find("interstella"), "Interstellar.2014.2160p.WEB-DL.mkv")   # misheard
        self.assertIsNone(self.find("the godfather"))

    def test_episodes(self):
        self.assertEqual(self.find("friends season 2 episode 4"), "Friends.S02E04.720p.mkv")
        self.assertEqual(self.find("friends s2e3"), "Friends.S02E03.720p.mkv")
        self.assertIsNone(self.find("friends season 9 episode 1"))

    def test_cleaning(self):
        self.assertEqual(vt._clean("The.Dark.Knight.2008.1080p.BluRay.x264-[YTS].mkv"), "the dark knight")


class RouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        cls.box = ToolBox(cls.cfg)

    def r(self, t):
        return router.route(t, self.box, self.cfg)

    def test_play_and_control_phrases(self):
        self.assertEqual(self.r("play the movie inception"), [("play_movie", {"title": "inception"})])
        self.assertEqual(self.r("watch friends season 2 episode 3"),
                         [("play_movie", {"title": "friends season 2 episode 3"})])
        with mock.patch.object(type(vt.vlc), "running", new=mock.PropertyMock(return_value=True)):
            self.assertEqual(self.r("pause"), [("vlc_control", {"action": "pause", "value": ""})])
            self.assertEqual(self.r("skip 30 seconds"), [("vlc_control", {"action": "forward", "value": "30"})])
            self.assertEqual(self.r("rewind 2 minutes"), [("vlc_control", {"action": "back", "value": "2 min"})])
            self.assertEqual(self.r("turn on subtitles"), [("vlc_control", {"action": "subtitles", "value": "on"})])
            self.assertEqual(self.r("next episode"), [("vlc_control", {"action": "next", "value": ""})])
            self.assertEqual(self.r("close the movie"), [("vlc_control", {"action": "close", "value": ""})])
        # with no movie open, "pause" is still the music
        self.assertEqual(self.r("pause")[0][0], "media_control")


@unittest.skipUnless(vt.vlc_path(), "VLC not installed")
class LiveVLCTests(unittest.TestCase):
    """Starts a real (silent, windowed) VLC on a 30-second clip and drives it over its web interface."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.clip = Path(cls.tmp.name) / "silence.wav"
        with wave.open(str(cls.clip), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(np.zeros(8000 * 30, dtype=np.int16).tobytes())
        vt.vlc.start(vt.vlc_path(), cls.clip, fullscreen=False, playlist=[])

    @classmethod
    def tearDownClass(cls):
        vt.vlc.stop()
        time.sleep(0.5)
        cls.tmp.cleanup()

    def test_control_round_trip(self):
        self.assertTrue(vt.vlc.running)
        self.assertEqual(vt.vlc.status().get("state"), "playing")
        self.assertIn("Paused", vt.vlc_control("pause"))
        self.assertEqual(vt.vlc.status().get("state"), "paused")
        self.assertIn("Playing", vt.vlc_control("resume"))
        self.assertIn("Skipped forward 10 seconds", vt.vlc_control("forward", "10"))
        self.assertIn("Jumped to 0:20", vt.vlc_control("seek_to", "0:20"))
        self.assertIn("Movie volume 50%", vt.vlc_control("volume", "50"))
        self.assertIn("of 0:30", vt.vlc_control("status"))


if __name__ == "__main__":
    unittest.main()
