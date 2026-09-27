import base64
import hashlib
import http.client
import io
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
sys.path.insert(0, str(ROOT / "scripts"))
from build import build
from launcher import Launcher
from maintenance import atomic_json, credentials, unpack
import repository


class FullUpdates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = self.root / "app"
        self.service = self.root / "service"
        shutil.copytree(ROOT / "app", self.app)
        shutil.copytree(ROOT / "service", self.service, ignore=shutil.ignore_patterns("__pycache__"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.launcher = Launcher(self.root / "data", port=port, timeout=4)
        credentials(self.launcher.data)
        password = (self.launcher.data / "initial-password.txt").read_text().strip()
        self.auth = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()
        self.thread = None

    def tearDown(self):
        self.launcher.closed = True
        if self.thread:
            self.thread.join(timeout=12)
            self.assertFalse(self.thread.is_alive())
        self.launcher.stop()
        for handler in self.launcher.log.handlers[:]:
            handler.close()
            self.launcher.log.removeHandler(handler)
        self.tmp.cleanup()

    def package(self, version="0.3.0"):
        manifest = json.loads((self.app / "manifest.json").read_text())
        manifest["version"] = version
        (self.app / "manifest.json").write_text(json.dumps(manifest))
        return build(self.app, self.root / (version + ".zip"), self.service)

    def install_initial(self):
        release = self.launcher.stage(self.package())
        self.assertTrue(self.launcher.switch(release))
        return release

    def request(self, method, path, body=None, auth=True, headers=None):
        all_headers = {"Authorization": self.auth} if auth else {}
        all_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.launcher.port, timeout=3)
        try:
            connection.request(method, path, body, all_headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def wait_version(self, version, job="success"):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                code, body = self.request("GET", "/api/status")
                state = json.loads(body)
                if code == 200 and state["version"] == version and state["job"]["status"] == job:
                    return state
            except (OSError, ValueError, http.client.HTTPException):
                pass
            time.sleep(0.1)
        self.fail("Timed out waiting for full release " + version)

    def test_http_update_replaces_service_routes_and_rollback_preserves_settings(self):
        original = self.install_initial()
        account = self.launcher.data / "app-data/yandex-account.json"
        account.write_text(json.dumps({"token": "private-token", "account": {"uid": "42"}}))
        original_credentials = (self.launcher.data / "credentials.json").read_bytes()
        self.thread = threading.Thread(target=self.launcher.run, daemon=True)
        self.thread.start()
        path = self.service / "maintenance.py"
        code = path.read_text().replace('VERSION = "0.3.0"', 'VERSION = "9.1.0"')
        code = code.replace('if path == "/api/status":',
                            'if path == "/api/new-feature":\n            self.respond(200, {"new": True})\n        elif path == "/api/status":')
        path.write_text(code)
        package = self.package("9.1.0")
        self.assertEqual(self.request("POST", "/api/update", package.read_bytes(), auth=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/update", package.read_bytes())[0], 403)
        self.assertEqual(self.request("GET", "/runtime-health")[0], 403)
        response = self.request("POST", "/api/update", package.read_bytes(), headers={"X-MediaStr-Request": "1"})
        self.assertEqual(response[0], 202)
        state = self.wait_version("9.1.0")
        self.assertEqual(state["maintenance_version"], "9.1.0")
        self.assertTrue(state["full_updates"])
        self.assertTrue(state["healthy"])
        self.assertEqual(json.loads(self.request("GET", "/api/new-feature")[1]), {"new": True})
        self.assertEqual(state["previous"], original)
        self.assertNotIn("private-token", self.request("GET", "/api/yandex/status")[1].decode())
        response = self.request("POST", "/api/rollback", headers={"X-MediaStr-Request": "1"})
        self.assertEqual(response[0], 202)
        state = self.wait_version("0.3.0")
        self.assertEqual(state["maintenance_version"], "0.3.0")
        self.assertEqual(self.request("GET", "/api/new-feature")[0], 404)
        self.assertEqual(json.loads(account.read_text())["token"], "private-token")
        self.assertEqual((self.launcher.data / "credentials.json").read_bytes(), original_credentials)

    def test_bad_service_rolls_back_entire_release(self):
        original = self.install_initial()
        (self.service / "maintenance.py").write_text('raise RuntimeError("broken service")\n')
        candidate = self.launcher.stage(self.package("9.2.0"))
        self.assertFalse(self.launcher.switch(candidate))
        self.assertEqual(self.launcher.read_state()["current"], original)
        state = self.wait_version("0.3.0", "error")
        self.assertEqual(state["maintenance_version"], "0.3.0")
        self.assertIsNone(self.launcher.read_state()["trial"])

    def test_bad_app_rolls_back_service_too(self):
        original = self.install_initial()
        (self.app / "app.py").write_text('raise RuntimeError("broken app")\n')
        candidate = self.launcher.stage(self.package("9.3.0"))
        self.assertFalse(self.launcher.switch(candidate))
        self.assertEqual(self.launcher.read_state()["current"], original)
        self.assertTrue(self.launcher.healthy())

    def test_interrupted_trial_boots_confirmed_release(self):
        original = self.install_initial()
        candidate = self.launcher.stage(self.package("9.4.0"))
        self.launcher.stop()
        state = self.launcher.read_state()
        state["trial"] = candidate
        atomic_json(self.launcher.state_path, state)
        self.launcher.boot()
        self.assertEqual(self.launcher.active, original)
        self.assertIsNone(self.launcher.read_state()["trial"])
        self.assertEqual(json.loads(self.launcher.job_path.read_text())["status"], "error")

    def test_frozen_recovery_accepts_update_when_all_releases_are_broken(self):
        original = self.install_initial()
        self.launcher.stop()
        (self.launcher.release_path(original) / "service/maintenance.py").write_text('raise RuntimeError("broken")\n')
        self.launcher.boot()
        self.assertTrue(self.launcher.recovery)
        self.assertEqual(self.request("GET", "/maintenance")[0], 200)
        self.assertEqual(self.request("GET", "/maintenance", auth=False)[0], 401)
        self.thread = threading.Thread(target=self.launcher.run, daemon=True)
        self.thread.start()
        response = self.request("POST", "/api/update", self.package("9.5.0").read_bytes(), headers={"X-MediaStr-Request": "1"})
        self.assertEqual(response[0], 202)
        self.wait_version("9.5.0")
        self.assertFalse(self.launcher.recovery)

    def test_app_only_update_rejected_without_stopping_service(self):
        original = self.install_initial()
        package = build(self.app, self.root / "app-only.zip")
        response = self.request("POST", "/api/update", package.read_bytes(), headers={"X-MediaStr-Request": "1"})
        self.assertEqual(response[0], 202)
        self.wait_version("0.3.0", "error")
        self.assertTrue(self.launcher.healthy())
        self.assertEqual(self.launcher.read_state()["current"], original)

    def test_pending_switch_rejects_second_mutation_but_keeps_status_available(self):
        self.install_initial()
        state = self.launcher.read_state()
        state["pending"] = self.launcher.stage(self.package("9.6.0"))
        atomic_json(self.launcher.state_path, state)
        self.assertEqual(self.request("GET", "/api/status")[0], 200)
        self.assertEqual(self.request("POST", "/api/rollback", headers={"X-MediaStr-Request": "1"})[0], 409)
        self.assertEqual(self.request("POST", "/api/music/action", b'{"action":"stop"}',
                                      headers={"X-MediaStr-Request": "1"})[0], 409)

    def test_migration_preserves_legacy_settings_and_confirmed_release_survives_restart(self):
        legacy = {"current": "legacy-release", "previous": None}
        atomic_json(self.launcher.data / "state.json", legacy)
        account_dir = self.launcher.data / "app-data"
        account_dir.mkdir()
        account = account_dir / "yandex-account.json"
        account.write_text(json.dumps({"token": "saved-token", "account": {"uid": "42"}}))
        original = self.install_initial()
        self.launcher.stop()
        self.launcher.boot()
        self.assertEqual(self.launcher.active, original)
        self.assertEqual(json.loads((self.launcher.data / "state.json").read_text()), legacy)
        self.assertEqual(json.loads(account.read_text())["token"], "saved-token")
        self.assertTrue(json.loads(self.request("GET", "/api/yandex/status")[1])["configured"])

    def test_full_package_validates_service_hash_syntax_and_presence(self):
        for kind in ("hash", "syntax", "missing", "future-loader"):
            with self.subTest(kind=kind):
                package = self.package()
                with zipfile.ZipFile(package) as archive:
                    files = {name: archive.read(name) for name in archive.namelist()}
                manifest = json.loads(files["manifest.json"])
                if kind in ("hash", "syntax"):
                    files["service/maintenance.py"] = b"def invalid syntax\n"
                    if kind == "syntax":
                        manifest["files"]["service/maintenance.py"] = hashlib.sha256(files["service/maintenance.py"]).hexdigest()
                elif kind == "missing":
                    del files["service/repository.py"]
                    del manifest["files"]["service/repository.py"]
                else:
                    manifest["min_launcher"] = 2
                files["manifest.json"] = json.dumps(manifest).encode()
                with zipfile.ZipFile(package, "w") as archive:
                    for name, data in files.items():
                        archive.writestr(name, data)
                with self.assertRaises((ValueError, SyntaxError)):
                    self.launcher.stage(package)
                self.assertEqual(list(self.launcher.releases.iterdir()), [])

    def test_repository_archive_builds_full_service_package_at_pinned_commit(self):
        sha = "a" * 40
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for folder in (self.app, self.service):
                for path in folder.rglob("*"):
                    if path.is_file():
                        archive.writestr("volumio_Ya-" + sha + "/" + path.relative_to(self.root).as_posix(), path.read_bytes())
        with patch.object(repository, "fetch", return_value=buffer.getvalue()) as fetch:
            package = self.root / "repository.zip"
            repository.make_package({"repository": repository.REPOSITORY, "commit": sha}, package)
        fetch.assert_called_once_with("codeload.github.com", "/GorINIch73/volumio_Ya/zip/" + sha, repository.MAX_DOWNLOAD)
        release = self.launcher.stage(package)
        manifest = json.loads((self.launcher.release_path(release) / "manifest.json").read_text())
        self.assertEqual(manifest["protocol"], 2)
        self.assertEqual(manifest["source"]["commit"], sha)
        self.assertIn("service/maintenance.py", manifest["files"])
        self.assertNotIn("service/launcher.py", manifest["files"])


if __name__ == "__main__":
    unittest.main()
