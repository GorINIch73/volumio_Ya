import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app
import test_yandex_account


TRACK = {"id": 7, "title": "Track", "artists": [{"name": "Artist"}],
         "albums": [{"title": "Album"}], "durationMs": 123000, "available": True}
OPTION = {"codec": "mp3", "preview": False, "bitrateInKbps": 320,
          "downloadInfoUrl": "https://storage.music.yandex.net/info?sign=test"}
INFO = {"host": "cdn.music.yandex.net", "path": "/music/file.mp3", "ts": "65abcdef", "s": "salt"}


class Music(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.account = app.YandexAccount(self.tmp.name, validator=lambda token: {"uid": "42"})
        self.account.login("secret-token")
        self.music = app.Music(self.account)
        self.music.volumio_host = "192.168.1.117"

    def test_likes_pagination_preserves_order_duplicates_and_unavailable(self):
        ids = [{"id": str(i)} for i in range(55)]
        ids[52] = {"id": "50"}
        with patch.object(self.music, "yandex", side_effect=[
            {"library": {"tracks": ids}},
            [{**TRACK, "id": i} for i in (54, 53, 50)]
        ]) as request:
            page = self.music.library({"kind": ["likes"], "offset": ["50"]})
        self.assertEqual([t["id"] for t in page["tracks"]], ["50", "51", "50", "53", "54"])
        self.assertFalse(page["tracks"][1]["available"])
        self.assertEqual(page["total"], 55)
        self.assertIsNone(page["next_offset"])
        self.assertEqual(request.call_args.args[2], {"track-ids": "50,51,50,53,54"})
        self.assertNotIn("secret-token", json.dumps(page))

    def test_playlist_tracks_and_empty_library(self):
        with patch.object(self.music, "yandex", side_effect=[
            {"title": "My playlist", "tracks": [{"track": TRACK}]}, [TRACK]
        ]) as request:
            result = self.music.library({"kind": ["1000"]})
        self.assertEqual(result["tracks"][0]["duration"], 123)
        self.assertEqual(request.call_args_list[0].args[1], "/users/42/playlists/1000")
        with patch.object(self.music, "yandex", return_value=[]):
            self.assertEqual(self.music.library({}), {"playlists": []})

    def test_missing_account_and_bad_identifiers_do_not_contact_services(self):
        with patch.object(app, "json_request") as request:
            for value in ("http://evil", "../42", "1?token=x", None):
                with self.assertRaises(app.AccountError):
                    self.music.action({"action": "play", "id": value})
            with self.assertRaises(app.AccountError):
                self.music.action({"action": "clearQueue"})
            self.account.logout()
            with self.assertRaises(app.AccountError):
                self.music.library({})
            request.assert_not_called()

    def test_play_uses_server_metadata_and_never_passes_token_to_volumio(self):
        with patch.object(app, "json_request", side_effect=[
            {"result": [TRACK]}, {"result": [OPTION]}, INFO, {"response": "success"}
        ]) as request:
            result = self.music.action({"action": "play", "id": "7", "uri": "http://evil"})
        calls = request.call_args_list
        self.assertNotIn("token", calls[2].kwargs)
        self.assertEqual(calls[3].args, (self.music.volumio_host, "/api/v1/replaceAndPlay"))
        item = calls[3].kwargs["payload"]
        digest = hashlib.md5(b"XGRlBW9FXlekgbPrRHuSiAmusic/file.mp3salt").hexdigest()
        self.assertEqual(item["uri"], f"https://cdn.music.yandex.net/get-mp3/{digest}/65abcdef/music/file.mp3")
        self.assertEqual(item["title"], "Track")
        self.assertEqual(item["service"], "webradio")
        self.assertEqual(item["type"], "track")
        self.assertNotIn("secret-token", json.dumps(item))
        self.assertNotIn("uri", result)

    def test_preview_or_unsafe_url_does_not_replace_existing_queue(self):
        for option in ({**OPTION, "preview": True},
                       {**OPTION, "downloadInfoUrl": "http://127.0.0.1/private"},
                       {**OPTION, "downloadInfoUrl": "https://evil.yandex.net.example/info"}):
            with self.subTest(option=option), patch.object(app, "json_request", side_effect=[
                {"result": [TRACK]}, {"result": [option]}
            ]) as request:
                with self.assertRaises(app.AccountError):
                    self.music.action({"action": "play", "id": "7"})
                self.assertEqual(request.call_count, 2)

    def test_enqueue_and_commands(self):
        with patch.object(self.music, "yandex", return_value=[TRACK]), \
             patch.object(self.music, "stream", return_value="https://cdn.music.yandex.net/audio"), \
             patch.object(self.music, "volumio") as volumio:
            self.music.action({"action": "enqueue", "id": "7"})
            self.assertEqual(volumio.call_args.args[0], "addToQueue")
            self.assertEqual(volumio.call_args.args[1]["service"], "webradio")
            for command in ("toggle", "pause", "stop", "next", "prev"):
                self.music.action({"action": command})
                volumio.assert_called_with("commands?cmd=" + command)

    def test_state_filters_signed_urls(self):
        with patch.object(self.music, "volumio", return_value={"status": "play", "title": "Track", "uri": "secret-signed-url"}):
            result = self.music.state()
        self.assertEqual(result["status"], "play")
        self.assertNotIn("secret", json.dumps(result))

    def test_failed_volumio_or_network_does_not_leak_remote_body(self):
        with patch.object(app.http.client, "HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"error":"secret-signed-url"}'
            with self.assertRaises(app.AccountError) as caught:
                self.music.volumio("getState")
            self.assertNotIn("secret", str(caught.exception))
            connection.return_value.request.side_effect = OSError("secret-token")
            with self.assertRaises(app.AccountError) as caught:
                self.music.state()
            self.assertNotIn("secret", str(caught.exception))

    def test_timeout_and_connection_refusal_have_distinct_safe_messages(self):
        for error, expected in ((TimeoutError("private-url"), "10 секунд"),
                                (ConnectionRefusedError("private-url"), "192.168.1.117:80")):
            with self.subTest(error=type(error).__name__), patch.object(app.http.client, "HTTPConnection") as connection:
                connection.return_value.request.side_effect = error
                with self.assertRaises(app.AccountError) as caught:
                    self.music.volumio("replaceAndPlay", {"uri": "private-url"})
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("private-url", str(caught.exception))
                self.assertEqual(connection.return_value.request.call_count,
                                 2 if isinstance(error, ConnectionRefusedError) else 1)
                if isinstance(error, ConnectionRefusedError):
                    self.assertEqual([call.args[0] for call in connection.call_args_list],
                                     [self.music.volumio_host, "127.0.0.1"])


class MusicHTTP(test_yandex_account.AccountHTTP):
    def setUp(self):
        super().setUp()
        self.server.music = Mock()

    def test_bridge_and_query_forwarding(self):
        for path in ("/api/music/library", "/api/music/state"):
            self.assertEqual(self.request("GET", path, authorized=False)[0], 403)
        self.server.music.library.return_value = {"tracks": [], "total": 0}
        self.assertEqual(self.request("GET", "/api/music/library?kind=likes&offset=50")[0], 200)
        self.server.music.library.assert_called_with({"kind": ["likes"], "offset": ["50"]})
        self.assertEqual(self.request("POST", "/api/music/action", {"action": "stop"}, authorized=False)[0], 403)
        self.server.music.action.assert_not_called()

    def test_command_error_and_malformed_body(self):
        self.assertEqual(self.request("POST", "/api/music/action", ["stop"])[0], 400)
        self.server.music.action.side_effect = app.AccountError("Нет связи с Volumio")
        status, value = self.request("POST", "/api/music/action", {"action": "stop"})
        self.assertEqual(status, 400)
        self.assertEqual(value["error"], "Нет связи с Volumio")
