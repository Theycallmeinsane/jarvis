"""Fast-path router, honest replies and verified media control (with a fake media session)."""
import threading
import time
import unittest
from unittest import mock

from jarvis import media, router
from jarvis.config import load_config
from jarvis.llm import ToolCall
from jarvis.llm.base import Turn
from jarvis.tools import ToolBox


class RouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        cls.box = ToolBox(cls.cfg)

    def r(self, text):
        return router.route(text, self.box, self.cfg)

    def test_media(self):
        self.assertEqual(self.r("Stop the song on Spotify."), [("media_control", {"action": "pause", "app": "spotify"})])
        self.assertEqual(self.r("Hey Jarvis, pause the music"), [("media_control", {"action": "pause", "app": ""})])
        self.assertEqual(self.r("Can you play the song on Spotify?"),
                         [("media_control", {"action": "play", "app": "spotify"})])
        self.assertEqual(self.r("resume"), [("media_control", {"action": "play", "app": ""})])
        self.assertEqual(self.r("next song please"), [("media_control", {"action": "next", "app": ""})])
        self.assertEqual(self.r("skip this"), [("media_control", {"action": "next", "app": ""})])
        self.assertEqual(self.r("previous track"), [("media_control", {"action": "previous", "app": ""})])
        self.assertEqual(self.r("What's playing?"), [("media_control", {"action": "status"})])
        self.assertIsNone(self.r("stop"))                      # bare "stop" = cancel, not pause
        # from the owner's real session
        self.assertEqual(self.r("Switch to the next music on Spotify please"),
                         [("media_control", {"action": "next", "app": "spotify"})])
        self.assertEqual(self.r("change the song"), [("media_control", {"action": "next", "app": ""})])
        self.assertEqual(self.r("Instead, play A Glimpse of Us by Joji, please."),
                         [("play_song", {"song": "a glimpse of us by joji"})])
        self.assertEqual(self.r("play some music"), [("media_control", {"action": "play", "app": ""})])

    def test_app_volume(self):
        self.assertEqual(self.r("Spotify volume to 30"), [("app_volume", {"app": "spotify", "action": "set", "percent": 30})])
        self.assertEqual(self.r("set spotify's volume to 40%"),
                         [("app_volume", {"app": "spotify", "action": "set", "percent": 40})])
        self.assertEqual(self.r("turn spotify down"), [("app_volume", {"app": "spotify", "action": "down"})])
        self.assertEqual(self.r("turn up the music"), [("app_volume", {"app": "music", "action": "up"})])
        self.assertEqual(self.r("make spotify quieter"), [("app_volume", {"app": "spotify", "action": "down"})])
        self.assertEqual(self.r("mute discord"), [("app_volume", {"app": "discord", "action": "mute"})])
        self.assertEqual(self.r("what's spotify's volume"), [("app_volume", {"app": "spotify", "action": "get"})])
        self.assertEqual(self.r("volume up"), [("volume", {"action": "up"})])     # plain = master volume

    def test_volume_time_timer_lock(self):
        self.assertEqual(self.r("set the volume to 30%"), [("volume", {"action": "set", "percent": 30})])
        self.assertEqual(self.r("turn it up a bit"), [("volume", {"action": "up"})])
        self.assertEqual(self.r("mute"), [("volume", {"action": "mute"})])
        self.assertEqual(self.r("What time is it?"), [("get_datetime", {})])
        self.assertEqual(self.r("set a timer for 10 minutes"), [("set_timer", {"seconds": 600})])
        self.assertEqual(self.r("remind me in an hour to call mom"),
                         [("set_timer", {"seconds": 3600, "message": "call mom"})])
        self.assertEqual(self.r("lock the computer"), [("lock_computer", {})])

    def test_apps_routines_and_fallthrough(self):
        self.assertEqual(self.r("open spotify"), [("open_app", {"name": "spotify"})])
        self.assertEqual(self.r("open downloads"), [("open_folder", {"folder": "downloads"})])
        self.assertIsNone(self.r("open bluetooth settings"))          # brain picks the settings page
        self.assertEqual(self.r("work mode"), [("run_routine", {"name": "work mode"})])
        self.assertIsNone(self.r("good morning"))                     # summarized routine -> brain
        self.assertIsNone(self.r("switch to the male voice"))         # not a window
        self.assertIsNone(self.r("So, whether it's okay."))           # garbled -> brain asks
        self.assertIsNone(self.r("tell me a joke about robots"))
        self.assertIsNone(self.r("close chrome"))                     # risky -> brain + confirmation


class ScriptedProvider:
    name, label, max_tool_rounds = "scripted", "scripted", 1

    def __init__(self, turns):
        self.turns, self.messages, self.calls = list(turns), [], 0

    def add_user(self, t):
        self.messages.append({"role": "user", "content": t})

    def add_assistant_text(self, t):
        self.messages.append({"role": "assistant", "content": t})

    def add_tool_results(self, rs):
        self.messages.append({"role": "tool", "content": [r.content for r in rs]})

    def step(self, use_tools=True, on_text=None):
        self.calls += 1
        return self.turns.pop(0)

    def check(self):
        return None


class HonestReplyTests(unittest.TestCase):
    def _agent(self, provider):
        from jarvis.agent import Agent

        a = Agent.__new__(Agent)
        a.cfg = load_config()
        a.confirm = lambda q: True
        a.toolbox = ToolBox(a.cfg, {})
        a.memory = None
        a._pending_switch = None
        a._lock = threading.RLock()
        a._last_activity = time.monotonic()
        a.provider = provider
        return a

    def test_failed_media_command_is_reported_not_claimed(self):
        p = ScriptedProvider([Turn(tool_calls=[ToolCall("1", "media_control", {"action": "play", "app": "spotify"})]),
                              Turn(text="I've started the music!")])     # the lie it must never get to say
        with mock.patch.object(media, "control", return_value=(False, "Spotify didn't accept play.")), \
                mock.patch.object(media, "play_song", side_effect=AssertionError("must not run for real")):
            reply = self._agent(p).handle("could you get the music going on spotify again")
        self.assertEqual(reply, "Spotify didn't accept play.")
        self.assertEqual(p.calls, 1)                                       # no second model call

    def test_put_on_reaches_the_media_tool(self):
        p = ScriptedProvider([Turn(tool_calls=[ToolCall("1", "media_control", {"action": "play"})])])
        with mock.patch.object(media, "control", return_value=(True, "Playing Innerbloom on Spotify.")):
            self.assertEqual(self._agent(p).handle("put on some music"), "Playing Innerbloom on Spotify.")

    def test_fast_path_skips_the_brain(self):
        p = ScriptedProvider([])
        with mock.patch.object(media, "control", return_value=(True, "Paused Spotify.")) as ctl:
            reply = self._agent(p).handle("pause the music on spotify")
        self.assertEqual(reply, "Paused Spotify.")
        self.assertEqual(p.calls, 0)
        ctl.assert_called_once_with("pause", "spotify", default_app="spotify")
        self.assertEqual([m["role"] for m in p.messages], ["user", "assistant"])   # context kept

    def test_time_is_said_directly(self):
        p = ScriptedProvider([])
        self.assertTrue(self._agent(p).handle("what time is it").startswith("It's "))


class _FakeSession:
    def __init__(self, app, title, status):
        self.app, self.title, self.status = app, title, status


class MediaControlTests(unittest.TestCase):
    def _sessions(self, *states):
        """Each call to media.sessions() returns the next state."""
        seq = [[media.Session(a, t, "Artist", s, _raw=object()) for a, t, s in st] for st in states]
        return mock.patch.object(media, "sessions", side_effect=lambda: seq.pop(0) if len(seq) > 1 else seq[0])

    def setUp(self):
        self.avail = mock.patch.object(media, "available", return_value=True)
        self.avail.start()
        self.cmd = mock.patch.object(media, "_command", new=mock.AsyncMock(return_value=True))
        self.cmd.start()
        self.sleep = mock.patch.object(media.time, "sleep")
        self.sleep.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_pause_is_verified(self):
        sp = "SpotifyAB.SpotifyMusic_x!Spotify"
        with self._sessions([(sp, "Innerbloom", "playing")], [(sp, "Innerbloom", "paused")]):
            ok, msg = media.control("stop", "spotify")
        self.assertTrue(ok)
        self.assertEqual(msg, "Paused Spotify.")

    def test_already_playing_is_not_toggled_off(self):
        sp = "SpotifyAB.SpotifyMusic_x!Spotify"
        with self._sessions([(sp, "Innerbloom", "playing")]):
            ok, msg = media.control("play", "spotify")
        self.assertTrue(ok)
        self.assertIn("already playing", msg)
        media._command.assert_not_called()

    def test_unconfirmed_change_is_admitted(self):
        sp = "SpotifyAB.SpotifyMusic_x!Spotify"
        with self._sessions([(sp, "Song", "paused")]):
            ok, msg = media.control("play", "spotify")
        self.assertIn("couldn't confirm", msg)

    def test_nothing_playing_without_app(self):
        with self._sessions([]):
            ok, msg = media.control("pause")
        self.assertFalse(ok)
        self.assertIn("Nothing is playing", msg)

    def test_closed_spotify_is_launched(self):
        sp = "SpotifyAB.SpotifyMusic_x!Spotify"
        with self._sessions([], [(sp, "Song", "paused")], [(sp, "Song", "paused")], [(sp, "Song", "playing")]), \
                mock.patch.object(media.os, "startfile") as start:
            ok, msg = media.control("play", "spotify")
        start.assert_called_once_with("spotify:")
        self.assertTrue(ok)
        self.assertEqual(msg, "Playing Song on Spotify.")


if __name__ == "__main__":
    unittest.main()
