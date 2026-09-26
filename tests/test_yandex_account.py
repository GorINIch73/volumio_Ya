import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app

PROFILE = {"uid": "42", "login": "listener", "display_name": "Listener"}


class Account(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.validator = Mock(return_value=PROFILE)
        self.account = app.YandexAccount(self.tmp.name, validator=self.validator)

    def tearDown(self):
        self.tmp.cleanup()

    def test_login_persists_without_exposing_token(self):
        result = self.account.login("  secret-token  ")
        self.assertTrue(result["configured"])
        self.assertEqual(result["account"], PROFILE)
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertNotIn("token", self.account.status())
        self.assertEqual(self.account.path.stat().st_mode & 0o777, 0o600)
        reopened = app.YandexAccount(self.tmp.name, validator=self.validator)
        self.assertEqual(reopened.status(), result)
        self.assertEqual(json.loads(self.account.path.read_text())["token"], "secret-token")

    def test_invalid_replacement_preserves_working_account(self):
        self.account.login("working-token")
        original = self.account.path.read_bytes()
        self.validator.side_effect = app.AccountError("Invalid token")
        with self.assertRaises(app.AccountError):
            self.account.login("bad-token")
        self.assertEqual(self.account.path.read_bytes(), original)

    def test_check_and_logout(self):
        self.account.login("secret-token")
        self.account.check()
        self.validator.assert_called_with("secret-token")
        result = self.account.logout()
        self.assertFalse(result["configured"])
        self.assertFalse(self.account.path.exists())
        with self.assertRaises(app.AccountError):
            self.account.check()

    def test_header_injection_and_empty_token(self):
        for token in ("", None, "bad\r\nInjected: yes", "OAuth some-token", "x" * 8193):
            with self.assertRaises(app.AccountError):
                self.account.login(token)
        self.validator.assert_not_called()


class YandexProtocol(unittest.TestCase):
    def test_oauth_endpoint_and_profile(self):
        with patch.object(app.http.client, "HTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps({"result": {"account": {"uid": 42, "login": "listener", "displayName": "Listener"}}}).encode()
            result = app.validate_yandex_token("secret-token")
            self.assertEqual(result, PROFILE)
            request = connection.return_value.request.call_args
            self.assertEqual(request.args, ("GET", "/account/status"))
            self.assertEqual(request.kwargs["headers"]["Authorization"], "OAuth secret-token")

    def test_rejected_token_and_network_error_are_sanitized(self):
        with patch.object(app.http.client, "HTTPSConnection") as connection:
            connection.return_value.getresponse.return_value.status = 401
            with self.assertRaisesRegex(app.AccountError, "отклонил"):
                app.validate_yandex_token("secret-token")
            connection.return_value.request.side_effect = OSError("secret-token")
            with self.assertRaises(app.AccountError) as caught:
                app.validate_yandex_token("secret-token")
            self.assertNotIn("secret-token", str(caught.exception))


class AccountHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"MEDIA_STR_HEALTH_NONCE": "bridge-secret"})
        self.environment.start()
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.server.account = app.YandexAccount(self.tmp.name, validator=lambda token: PROFILE)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.environment.stop()
        self.tmp.cleanup()

    def request(self, method, path, data=None, authorized=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"X-MediaStr-Bridge": "bridge-secret"} if authorized else {}
        connection.request(method, path, body=json.dumps(data).encode() if data is not None else None, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_login_status_check_logout(self):
        status, result = self.request("POST", "/api/yandex/login", {"token": "secret-token"})
        self.assertEqual(status, 200)
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertTrue(self.request("GET", "/api/yandex/status")[1]["configured"])
        self.assertEqual(self.request("POST", "/api/yandex/check")[0], 200)
        self.assertFalse(self.request("POST", "/api/yandex/logout")[1]["configured"])

    def test_direct_loopback_request_requires_bridge_key(self):
        self.assertEqual(self.request("GET", "/api/yandex/status", authorized=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/yandex/login", {"token": "secret-token"}, authorized=False)[0], 403)

