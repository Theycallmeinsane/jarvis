"""Spotify Web API control, offline: a fake HTTP session stands in for api.spotify.com."""
import base64
import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from jarvis import router
from jarvis import spotify as sp
from jarvis.tools import ToolBox
from jarvis.tools.builtin import ToolFailed


class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.content = b"" if data is None else json.dumps(data).encode()
        self.text = self.content.decode()

    def json(self):
        if self._data is None:
            raise ValueError("no body")
        return self._data


class FakeHTTP:
    """routes: {(METHOD, path): Resp | [Resp, ...] (served in order, last repeats) | fn(params, body) -> Resp}"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, params=None, json=None, timeout=None, headers=None):
        path = url.removeprefix(sp.API)
        self.calls.append((method, path, dict(params or {}), json, (headers or {}).get("Authorization")))
        r = self.routes.get((method, path))
        if r is None:
            return Resp(404, {"error": {"status": 404, "message": "Not found"}})
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if callable(r):
            r = r(params or {}, json)
        return r

    def sent(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]


def track(name, artist, tid=None, ms=200_000):
    tid = tid or name.lower().replace(" ", "")
    return {"name": name, "uri": f"spotify:track:{tid}", "id": tid, "duration_ms": ms,
            "artists": [{"name": artist}], "type": "track"}


def player(item, playing=True, progress=60_000, volume=40, device="DESKTOP", shuffle=False):
    return {"is_playing": playing, "item": item, "progress_ms": progress, "shuffle_state": shuffle,
            "device": {"id": "d1", "name": device, "volume_percent": volume, "supports_volume": True}}


NO_DEVICE = Resp(404, {"error": {"status": 404, "message": "Player command failed: No active device found",
                                 "reason": "NO_ACTIVE_DEVICE"}})


class SpotifyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token_file = Path(self.tmp.name) / "spotify_token.json"
        self.cfg = {"spotify": {"token_file": str(self.token_file)}}
        self.env = mock.patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "cid123"})
        self.env.start()
        self.token_file.write_text(json.dumps({"access_token": "AT", "refresh_token": "RT",
                                               "expires_at": time.time() + 3600}))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def client(self, routes, launch=None):
        self.http = FakeHTTP(routes)
        return sp.Spotify(self.cfg, http=self.http, sleep=lambda s: None, launch=launch or (lambda: None))


class AuthTests(SpotifyCase):
    def test_pkce_challenge_is_sha256_of_verifier(self):
        verifier, challenge = sp.pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, expect)
        url = sp.authorize_url(challenge, "st8")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["client_id"], ["cid123"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["redirect_uri"], [sp.DEFAULT_REDIRECT])
        self.assertIn("user-library-modify", q["scope"][0])
        self.assertIn("playlist-modify-private", q["scope"][0])

    def test_login_catches_the_redirect_and_saves_the_token(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        redirect = f"http://127.0.0.1:{port}/callback"

        def browser(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

            def visit():
                time.sleep(0.3)
                urllib.request.urlopen(f"{redirect}?code=CODE1&state={q['state'][0]}", timeout=5).read()
            threading.Thread(target=visit, daemon=True).start()
            return True

        posted = {}

        def post(url, data=None, timeout=None, headers=None):
            posted.update(data)
            return Resp(200, {"access_token": "NEW", "refresh_token": "R2", "expires_in": 3600, "scope": "x"})

        with mock.patch.dict(os.environ, {"SPOTIFY_REDIRECT_URI": redirect}), \
                mock.patch.object(sp.requests, "post", side_effect=post):
            self.token_file.unlink()
            msg = sp.login(self.cfg, open_browser=browser, timeout=10)
        self.assertIn("connected", msg)
        self.assertEqual(posted["code"], "CODE1")
        self.assertEqual(posted["grant_type"], "authorization_code")
        self.assertEqual(posted["redirect_uri"], redirect)
        self.assertTrue(posted["code_verifier"])
        saved = json.loads(self.token_file.read_text())
        self.assertEqual((saved["access_token"], saved["refresh_token"]), ("NEW", "R2"))

    def test_login_without_client_id_explains_setup(self):
        with mock.patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": ""}):
            with self.assertRaises(sp.NotConnected) as e:
                sp.login(self.cfg, open_browser=lambda u: None)
        self.assertIn("SPOTIFY_CLIENT_ID", str(e.exception))

    def test_expired_token_is_refreshed_and_keeps_refresh_token(self):
        self.token_file.write_text(json.dumps({"access_token": "OLD", "refresh_token": "RT", "expires_at": 0}))
        c = self.client({("GET", "/me/player"): Resp(204)})
        with mock.patch.object(sp.requests, "post",
                               return_value=Resp(200, {"access_token": "FRESH", "expires_in": 3600})) as post:
            c.state()
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(post.call_args.kwargs["data"]["client_id"], "cid123")
        self.assertEqual(self.http.calls[0][4], "Bearer FRESH")
        self.assertEqual(json.loads(self.token_file.read_text())["refresh_token"], "RT")

    def test_401_refreshes_once_and_retries(self):
        c = self.client({("GET", "/me/player"): [Resp(401, {"error": {"status": 401, "message": "expired"}}),
                                                   Resp(200, player(track("A", "B")))]})
        with mock.patch.object(sp.requests, "post", return_value=Resp(200, {"access_token": "AT2"})):
            self.assertEqual(c.state()["item"]["name"], "A")
        self.assertEqual(self.http.calls[1][4], "Bearer AT2")

    def test_not_signed_in(self):
        self.token_file.unlink()
        c = self.client({})
        self.assertFalse(c.connected())
        with self.assertRaises(sp.NotConnected):
            c.pause()


class PlayTests(SpotifyCase):
    def test_play_song_by_artist_picks_the_right_track(self):
        hit = track("Glimpse of Us", "Joji")
        searches = []

        def search(params, body):
            searches.append(params)
            return Resp(200, {"tracks": {"items": [track("Glimpse of Us (cover)", "Someone", "x"), hit]}})
        c = self.client({("GET", "/search"): search, ("PUT", "/me/player/play"): Resp(204),
                         ("GET", "/me/player"): Resp(200, player(hit))})
        self.assertEqual(c.play("glimpse of us by joji"), "Playing Glimpse of Us by Joji on Spotify.")
        self.assertEqual(searches[0]["q"], "glimpse of us artist:joji")
        self.assertLessEqual(searches[0]["limit"], 10)            # Spotify's 2026 search limit
        self.assertEqual(self.http.sent("PUT", "/me/player/play")[0][3], {"uris": [hit["uri"]]})

    def test_says_so_when_spotify_plays_something_else(self):
        hit, other = track("Song", "Band"), track("Other Thing", "Else")
        c = self.client({("GET", "/search"): Resp(200, {"tracks": {"items": [hit]}}),
                         ("PUT", "/me/player/play"): Resp(204), ("GET", "/me/player"): Resp(200, player(other))})
        self.assertIn("but it's playing Other Thing", c.play("song", "track"))

    def test_no_active_device_starts_spotify_and_plays_here(self):
        launched = []
        hit = track("Song", "Band")
        devices = [Resp(200, {"devices": []}), Resp(200, {"devices": []}),
                   Resp(200, {"devices": [{"id": "pc1", "name": socket.gethostname(), "type": "Computer",
                                           "is_active": False}]})]
        c = self.client({("GET", "/search"): Resp(200, {"tracks": {"items": [hit]}}),
                         ("PUT", "/me/player/play"): [NO_DEVICE, Resp(204)],
                         ("GET", "/me/player/devices"): devices, ("GET", "/me/player"): Resp(200, player(hit))},
                        launch=lambda: launched.append(1))
        self.assertIn("Playing Song", c.play("song"))
        self.assertEqual(launched, [1])
        self.assertEqual(self.http.sent("PUT", "/me/player/play")[-1][2], {"device_id": "pc1"})

    def test_my_playlist_beats_public_search(self):
        mine = {"name": "Gym Bangers", "uri": "spotify:playlist:mine", "id": "mine"}
        c = self.client({("GET", "/me/playlists"): Resp(200, {"items": [{"name": "Chill", "id": "c"}, mine],
                                                               "next": None}),
                         ("PUT", "/me/player/play"): Resp(204)})
        self.assertEqual(c.play("my gym bangers", "playlist"), "Playing the playlist Gym Bangers.")
        self.assertEqual(self.http.sent("PUT", "/me/player/play")[0][3], {"context_uri": "spotify:playlist:mine"})
        self.assertFalse(self.http.sent("GET", "/search"))

    def test_artist_and_album(self):
        c = self.client({("GET", "/search"): lambda p, b: Resp(200, {
            "artists": {"items": [{"name": "Drake", "uri": "spotify:artist:d"}]},
            "albums": {"items": [{"name": "Nevermind", "uri": "spotify:album:n", "artists": [{"name": "Nirvana"}]}]},
        }), ("PUT", "/me/player/play"): Resp(204)})
        self.assertEqual(c.play("drake"), "Playing Drake on Spotify.")       # auto: an exact artist name
        self.assertEqual(c.play("nevermind", "album"), "Playing the album Nevermind by Nirvana.")
        bodies = [x[3] for x in self.http.sent("PUT", "/me/player/play")]
        self.assertEqual(bodies, [{"context_uri": "spotify:artist:d"}, {"context_uri": "spotify:album:n"}])

    def test_liked_songs(self):
        items = [{"track": track(f"T{i}", "A")} for i in range(3)]
        c = self.client({("GET", "/me/tracks"): Resp(200, {"items": items}), ("PUT", "/me/player/play"): Resp(204)})
        self.assertEqual(c.play("", "liked"), "Playing your liked songs (3 most recent).")
        self.assertEqual(len(self.http.sent("PUT", "/me/player/play")[0][3]["uris"]), 3)

    def test_not_found(self):
        c = self.client({("GET", "/search"): Resp(200, {"tracks": {"items": []}})})
        with self.assertRaises(sp.SpotifyError) as e:
            c.play("asdfghjkl", "track")
        self.assertIn("couldn't find", str(e.exception))

    def test_premium_required(self):
        c = self.client({("GET", "/me/player"): Resp(200, player(track("A", "B"), playing=False)),
                         ("PUT", "/me/player/play"): Resp(403, {"error": {"status": 403, "message": "Premium required",
                                                                         "reason": "PREMIUM_REQUIRED"}})})
        with self.assertRaises(sp.PremiumRequired):
            c.resume()


class PlayerTests(SpotifyCase):
    def test_pause_resume_skip_status(self):
        a, b = track("First", "X"), track("Second", "Y")
        c = self.client({("GET", "/me/player"): [Resp(200, player(a)), Resp(200, player(a)), Resp(200, player(b))],
                         ("PUT", "/me/player/pause"): Resp(204), ("POST", "/me/player/next"): Resp(204)})
        self.assertEqual(c.pause(), "Paused Spotify.")
        self.assertEqual(c.skip("next"), "Now playing Second by Y.")
        self.assertEqual(c.now_playing(), "Playing Second by Y on DESKTOP, 1:00 of 3:20.")

    def test_pause_when_nothing_plays(self):
        c = self.client({("GET", "/me/player"): Resp(204)})
        self.assertEqual(c.pause(), "Spotify is already paused.")
        self.assertEqual(c.now_playing(), "Nothing is playing on Spotify.")

    def test_volume(self):
        c = self.client({("GET", "/me/player"): Resp(200, player(track("A", "B"), volume=40)),
                         ("PUT", "/me/player/volume"): Resp(204)})
        self.assertEqual(c.volume("up"), "Spotify volume 55%.")
        self.assertEqual(c.volume("30%"), "Spotify volume 30%.")
        self.assertEqual(c.volume("down 50"), "Spotify volume 0%.")
        self.assertEqual(c.volume("get"), "Spotify's volume is 40%.")
        self.assertEqual([x[2]["volume_percent"] for x in self.http.sent("PUT", "/me/player/volume")], [55, 30, 0])

    def test_shuffle_repeat(self):
        c = self.client({("PUT", "/me/player/shuffle"): Resp(204), ("PUT", "/me/player/repeat"): Resp(204)})
        self.assertEqual(c.shuffle("on"), "Shuffle is on.")
        self.assertEqual(c.shuffle("off"), "Shuffle is off.")
        self.assertEqual(c.repeat("track"), "Repeating this song.")
        self.assertEqual(c.repeat("playlist"), "Repeating this playlist.")
        self.assertEqual(c.repeat("off"), "Repeat is off.")
        self.assertEqual([x[2]["state"] for x in self.http.sent("PUT", "/me/player/shuffle")], ["true", "false"])
        self.assertEqual([x[2]["state"] for x in self.http.sent("PUT", "/me/player/repeat")],
                         ["track", "context", "off"])

    def test_seek(self):
        c = self.client({("GET", "/me/player"): Resp(200, player(track("A", "B"), progress=60_000)),
                         ("PUT", "/me/player/seek"): Resp(204)})
        self.assertEqual(c.seek("1:30"), "At 1:30.")
        self.assertEqual(c.seek("30", relative=1), "At 1:30.")
        self.assertEqual(c.seek("2 minutes", relative=-1), "At 0:00.")
        self.assertEqual(c.seek("10 minutes"), "At 3:19.")       # never past the end of the song
        self.assertEqual([x[2]["position_ms"] for x in self.http.sent("PUT", "/me/player/seek")],
                         [90_000, 90_000, 0, 199_000])

    def test_queue(self):
        hit = track("Levitating", "Dua Lipa")
        c = self.client({("GET", "/search"): Resp(200, {"tracks": {"items": [hit]}}),
                         ("POST", "/me/player/queue"): Resp(204),
                         ("GET", "/me/player/queue"): Resp(200, {"currently_playing": None,
                                                                 "queue": [hit, track("B", "C")]})})
        self.assertEqual(c.queue_add("levitating"), "Added Levitating by Dua Lipa to the queue.")
        self.assertEqual(self.http.sent("POST", "/me/player/queue")[0][2], {"uri": hit["uri"]})
        self.assertEqual(c.queue_show(), "Up next: Levitating by Dua Lipa; B by C.")

    def test_devices_and_transfer(self):
        devs = [{"id": "pc", "name": "DESKTOP", "type": "Computer", "is_active": True},
                {"id": "ph", "name": "Muhammad's iPhone", "type": "Smartphone", "is_active": False}]
        c = self.client({("GET", "/me/player/devices"): Resp(200, {"devices": devs}), ("PUT", "/me/player"): Resp(204)})
        self.assertEqual(c.list_devices(), "Spotify devices: DESKTOP (playing), Muhammad's iPhone.")
        self.assertEqual(c.transfer("phone"), "Moved Spotify to Muhammad's iPhone.")
        self.assertEqual(self.http.sent("PUT", "/me/player")[0][3], {"device_ids": ["ph"], "play": True})
        self.assertEqual(c.transfer("desktop"), "Spotify is already playing on DESKTOP.")
        with self.assertRaises(sp.SpotifyError):
            c.transfer("kitchen speaker")


class LibraryTests(SpotifyCase):
    def test_like_unlike_use_the_2026_library_endpoint(self):
        now = track("Now", "Playing")
        c = self.client({("GET", "/me/player"): Resp(200, player(now)), ("PUT", "/me/library"): Resp(200),
                         ("DELETE", "/me/library"): Resp(200), ("GET", "/me/library/contains"): Resp(200, [True])})
        self.assertEqual(c.like(), "Added Now by Playing to your liked songs.")
        self.assertEqual(self.http.sent("PUT", "/me/library")[0][2], {"uris": "spotify:track:now"})
        self.assertEqual(c.like("this song", unlike=True), "Removed Now by Playing from your liked songs.")
        self.assertEqual(c.is_liked(), "Yes, Now by Playing is in your liked songs.")

    def test_like_a_named_song(self):
        hit = track("Yellow", "Coldplay")
        c = self.client({("GET", "/search"): Resp(200, {"tracks": {"items": [hit]}}), ("PUT", "/me/library"): Resp(200)})
        self.assertEqual(c.like("yellow by coldplay"), "Added Yellow by Coldplay to your liked songs.")

    def test_nothing_playing(self):
        c = self.client({("GET", "/me/player"): Resp(204)})
        with self.assertRaises(sp.SpotifyError) as e:
            c.like()
        self.assertIn("Nothing is playing", str(e.exception))


class PlaylistTests(SpotifyCase):
    LISTS = {"items": [{"name": "Gym", "id": "g1", "uri": "spotify:playlist:g1"},
                       {"name": "Late Night Drive", "id": "l1", "uri": "spotify:playlist:l1"}], "next": None}

    def test_add_and_remove_current_song(self):
        now = track("Now", "Playing")
        c = self.client({("GET", "/me/playlists"): Resp(200, self.LISTS), ("GET", "/me/player"): Resp(200, player(now)),
                         ("POST", "/playlists/l1/items"): Resp(201, {"snapshot_id": "s"}),
                         ("DELETE", "/playlists/l1/items"): Resp(200, {"snapshot_id": "s2"})})
        self.assertEqual(c.add_to_playlist("late night drive"), "Added Now by Playing to Late Night Drive.")
        self.assertEqual(self.http.sent("POST", "/playlists/l1/items")[0][3], {"uris": ["spotify:track:now"]})
        self.assertEqual(c.remove_from_playlist("my late night drive playlist"),
                         "Removed Now by Playing from Late Night Drive.")
        self.assertEqual(self.http.sent("DELETE", "/playlists/l1/items")[0][3], {"items": [{"uri": "spotify:track:now"}]})

    def test_unknown_playlist(self):
        c = self.client({("GET", "/me/playlists"): Resp(200, self.LISTS)})
        with self.assertRaises(sp.SpotifyError) as e:
            c.add_to_playlist("road trip")
        self.assertIn("couldn't find a playlist", str(e.exception))

    def test_create_list_delete(self):
        c = self.client({("GET", "/me/playlists"): Resp(200, self.LISTS),
                         ("POST", "/me/playlists"): Resp(201, {"id": "n1", "name": "Road Trip"}),
                         ("DELETE", "/me/library"): Resp(200)})
        self.assertEqual(c.create_playlist("Road Trip"), "Created the playlist Road Trip.")
        body = self.http.sent("POST", "/me/playlists")[0][3]
        self.assertEqual((body["name"], body["public"]), ("Road Trip", False))
        self.assertEqual(c.create_playlist("gym"), "You already have a playlist called Gym.")
        self.assertEqual(c.list_playlists(), "You have 2 playlists: Gym, Late Night Drive.")
        self.assertIn("Deleted the playlist Gym", c.delete_playlist("gym"))
        self.assertEqual(self.http.sent("DELETE", "/me/library")[0][2], {"uris": "spotify:playlist:g1"})


class HelperTests(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(sp.parse_time("1:30"), 90)
        self.assertEqual(sp.parse_time("1:02:03"), 3723)
        self.assertEqual(sp.parse_time("45"), 45)
        self.assertEqual(sp.parse_time("2 minutes"), 120)
        self.assertIsNone(sp.parse_time("soon"))

    def test_likeness(self):
        self.assertGreater(sp.likeness("late night drive", "Late Night Drive"), 0.99)
        self.assertGreater(sp.likeness("gym", "Gym Bangers"), 0.75)
        self.assertLess(sp.likeness("road trip", "Late Night Drive"), 0.5)


class AllTools:
    def get(self, name):
        return object()


class NoSpotifyTools:
    def get(self, name):
        return None if name.startswith("spotify_") else object()


class RouterTests(unittest.TestCase):
    def r(self, text, box=None):
        return router.route(text, box or AllTools())

    def test_spotify_phrases(self):
        ctl = lambda a, v="": [("spotify_control", {"action": a, "value": v})]  # noqa: E731
        pl = lambda a, p="", s="": [("spotify_playlist", {"action": a, "playlist": p, "song": s})]  # noqa: E731
        cases = {
            "connect spotify": ctl("connect"),
            "play my liked songs": [("spotify_play", {"query": "liked songs", "kind": "liked"})],
            "shuffle my liked songs": [("spotify_play", {"query": "liked songs", "kind": "liked"})] + ctl("shuffle", "on"),
            "play my workout playlist": [("spotify_play", {"query": "workout", "kind": "playlist"})],
            "play the playlist called late night drive": [("spotify_play", {"query": "late night drive",
                                                                            "kind": "playlist"})],
            "play the album nevermind": [("spotify_play", {"query": "nevermind", "kind": "album"})],
            "play some music by daft punk": [("spotify_play", {"query": "daft punk", "kind": "artist"})],
            "play blinding lights on spotify": [("spotify_play", {"query": "blinding lights", "kind": "auto"})],
            "like this song": ctl("like"),
            "add this song to my liked songs": ctl("like"),
            "remove this song from my liked songs": ctl("unlike"),
            "is this song in my liked songs": ctl("is_liked"),
            "add levitating to the queue": ctl("queue", "levitating"),
            "queue up yellow by coldplay": ctl("queue", "yellow by coldplay"),
            "what's in my queue": ctl("up_next"),
            "add this song to my gym playlist": pl("add", "gym"),
            "add yellow to my road trip playlist": pl("add", "road trip", "yellow"),
            "remove this song from the gym playlist": pl("remove", "gym"),
            "create a playlist called road trip": pl("create", "road trip"),
            "make a new chill playlist": pl("create", "chill"),
            "what playlists do i have": pl("list"),
            "shuffle on": ctl("shuffle", "on"),
            "turn off shuffle": ctl("shuffle", "off"),
            "repeat this song": ctl("repeat", "track"),
            "repeat the playlist": ctl("repeat", "playlist"),
            "turn off repeat": ctl("repeat", "off"),
            "skip ahead 30 seconds": ctl("forward", "30"),
            "go back 1 minute": ctl("back", "60"),
            "skip to 1:30": ctl("seek", "1:30"),
            "play it on my phone": ctl("transfer", "phone"),
            "move the music to the living room speaker": ctl("transfer", "living room speaker"),
            "what spotify devices are available": ctl("devices"),
        }
        for text, expect in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.r(text), expect)

    def test_plain_media_commands_are_unchanged(self):
        self.assertEqual(self.r("pause spotify"), [("media_control", {"action": "pause", "app": "spotify"})])
        self.assertEqual(self.r("next song"), [("media_control", {"action": "next", "app": ""})])
        self.assertEqual(self.r("play some music"), [("media_control", {"action": "play", "app": ""})])
        self.assertEqual(self.r("spotify volume to 30"), [("app_volume", {"app": "spotify", "action": "set",
                                                                          "percent": 30})])
        self.assertEqual(self.r("open spotify"), [("open_app", {"name": "spotify"})])
        self.assertIsNone(self.r("repeat that"))         # not a Spotify command

    def test_without_spotify_setup_songs_use_the_app(self):
        box = NoSpotifyTools()
        self.assertEqual(self.r("play blinding lights", box), [("play_song", {"song": "blinding lights"})])
        self.assertIsNone(router.route("like this song", box))


class ToolTests(SpotifyCase):
    def box(self):
        from jarvis.config import Section

        return ToolBox(Section({**self.cfg, "tools": {}}))

    def test_tools_appear_only_once_configured(self):
        self.assertIsNotNone(self.box().get("spotify_play"))
        self.assertTrue(self.box().get("spotify_delete_playlist").dangerous)
        with mock.patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": ""}):
            self.assertIsNone(self.box().get("spotify_control"))
        with mock.patch.dict(self.cfg["spotify"], {"enabled": False}):
            self.assertIsNone(self.box().get("spotify_play"))

    def test_basic_playback_falls_back_when_not_signed_in(self):
        self.token_file.unlink()
        box = self.box()
        with mock.patch.object(sp, "_client", None), \
                mock.patch("jarvis.media.control", return_value=(True, "Paused Spotify.")) as ctl:
            self.assertEqual(box.run("spotify_control", {"action": "pause"}), "Paused Spotify.")
        ctl.assert_called_once_with("pause", "spotify")
        with mock.patch.object(sp, "_client", None), \
                mock.patch("jarvis.media.play_song", return_value=(True, "Playing Yellow by Coldplay on Spotify.")):
            self.assertEqual(box.run("spotify_play", {"query": "yellow"}), "Playing Yellow by Coldplay on Spotify.")
        with mock.patch.object(sp, "_client", None), self.assertRaises(ToolFailed) as e:
            box.run("spotify_control", {"action": "like"})
        self.assertIn("connect Spotify", str(e.exception))

    def test_tool_uses_the_api_when_connected(self):
        fake = mock.Mock(spec=sp.Spotify)
        fake.play.return_value = "Playing the playlist Gym."
        with mock.patch.object(sp, "client", return_value=fake):
            out = self.box().run("spotify_play", {"query": "gym", "kind": "playlist"})
        self.assertEqual(out, "Playing the playlist Gym.")
        fake.play.assert_called_once_with("gym", "playlist")

    def test_play_song_tool_prefers_the_api(self):
        fake = mock.Mock(spec=sp.Spotify)
        fake.connected.return_value = True
        fake.play.return_value = "Playing Yellow by Coldplay on Spotify."
        with mock.patch.object(sp, "client", return_value=fake), mock.patch("jarvis.media.play_song") as old:
            self.assertEqual(self.box().run("play_song", {"song": "yellow"}), "Playing Yellow by Coldplay on Spotify.")
        old.assert_not_called()

    def test_connect_runs_in_the_background(self):
        with mock.patch.object(sp, "login", return_value="ok") as login:
            out = self.box().run("spotify_control", {"action": "connect"})
            for _ in range(50):
                if login.called:
                    break
                time.sleep(0.02)
        self.assertIn("sign-in page", out)
        login.assert_called_once()


if __name__ == "__main__":
    unittest.main()
