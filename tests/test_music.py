import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app


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
                    app.music_id(value)
            self.account.logout()
            with self.assertRaises(app.AccountError):
                self.music.library({})
            request.assert_not_called()

    def test_stream_signature_and_no_token_sent_to_cdn(self):
        with patch.object(app, "json_request", side_effect=[{"result": [OPTION]}, INFO]) as request:
            uri = self.music.stream("secret-token", "7")
        digest = hashlib.md5(b"XGRlBW9FXlekgbPrRHuSiAmusic/file.mp3salt").hexdigest()
        self.assertEqual(uri, f"https://cdn.music.yandex.net/get-mp3/{digest}/65abcdef/music/file.mp3")
        self.assertNotIn("token", request.call_args.kwargs)

    def test_preview_and_unsafe_download_urls_are_rejected(self):
        for option in ({**OPTION, "preview": True},
                       {**OPTION, "downloadInfoUrl": "http://127.0.0.1/private"},
                       {**OPTION, "downloadInfoUrl": "https://evil.yandex.net.example/info"}):
            with self.subTest(option=option), patch.object(app, "json_request", return_value={"result": [option]}) as request:
                with self.assertRaises(app.AccountError):
                    self.music.stream("secret-token", "7")
                self.assertEqual(request.call_count, 1)

    def test_network_errors_do_not_expose_secrets(self):
        for error in (TimeoutError("secret-token"), ConnectionRefusedError("secret-token"), OSError("secret-token")):
            with self.subTest(error=type(error).__name__), patch.object(app.http.client, "HTTPSConnection") as connection:
                connection.return_value.request.side_effect = error
                with self.assertRaises(app.AccountError) as caught:
                    app.json_request("api.music.yandex.net", "/tracks", token="secret-token")
                self.assertNotIn("secret-token", str(caught.exception))
