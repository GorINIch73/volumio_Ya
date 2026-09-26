import base64
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import stat
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
from maintenance import Handler, Manager, ThreadingHTTPServer, credentials, redact, unpack
from build import build
import repository
from repository_fixtures import SHA, fake_fetch


class Packages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        shutil.copytree(ROOT / "app", self.source)
        self.package = self.root / "release.zip"
        build(self.source, self.package)

    def tearDown(self):
        self.tmp.cleanup()

    def rewrite(self, callback):
        with zipfile.ZipFile(self.package) as archive:
            files = {item.filename: archive.read(item) for item in archive.infolist()}
        callback(files)
        with zipfile.ZipFile(self.package, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)

    def test_valid_package(self):
        target = self.root / "output"
        target.mkdir()
        manifest = unpack(self.package, target)
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertTrue((target / "web/index.html").is_file())

    def test_traversal_and_absolute_paths(self):
        for name in ("../outside", "/outside", "web/../../outside", "web\\outside", "web//outside"):
            with self.subTest(name=name):
                build(self.source, self.package)
                self.rewrite(lambda files: files.update({name: b"bad"}))
                with self.assertRaises(ValueError):
                    unpack(self.package, self.root / "output")
        self.assertFalse((self.root / "outside").exists())

    def test_modified_file(self):
        self.rewrite(lambda files: files.update({"app.py": b"print('tampered')"}))
        with self.assertRaisesRegex(ValueError, "сумма"):
            unpack(self.package, self.root / "output")

    def test_symlink(self):
        with zipfile.ZipFile(self.package, "a") as archive:
            item = zipfile.ZipInfo("web/link")
            item.create_system = 3
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(item, "/etc/passwd")
        with self.assertRaises(ValueError):
            unpack(self.package, self.root / "output")

    def test_oversized_expansion(self):
        with zipfile.ZipFile(self.package, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("web/huge", b"x" * (33 * 1024 * 1024))
        with self.assertRaisesRegex(ValueError, "32 МБ"):
            unpack(self.package, self.root / "output")

    def test_wrong_protocol(self):
        def change(files):
            manifest = json.loads(files["manifest.json"])
            manifest["protocol"] = 999
            files["manifest.json"] = json.dumps(manifest).encode()
        self.rewrite(change)
        with self.assertRaisesRegex(ValueError, "формат"):
            unpack(self.package, self.root / "output")

    def test_future_python(self):
        def change(files):
            manifest = json.loads(files["manifest.json"])
            manifest["min_python"] = [99, 0]
            files["manifest.json"] = json.dumps(manifest).encode()
        self.rewrite(change)
        with self.assertRaisesRegex(ValueError, "Python"):
            unpack(self.package, self.root / "output")


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = Manager(self.root / "data", startup_timeout=2)
        self.source = self.root / "source"
        shutil.copytree(ROOT / "app", self.source)
        self.package = self.root / "release.zip"
        build(self.source, self.package)
        self.manager.install(self.package)

    def tearDown(self):
        self.manager.stop()
        for handler in self.manager.log.handlers[:]:
            handler.close()
            self.manager.log.removeHandler(handler)
        self.tmp.cleanup()

    def next_version(self, broken=False):
        manifest = json.loads((self.source / "manifest.json").read_text())
        manifest["version"] = "0.2.0"
        (self.source / "manifest.json").write_text(json.dumps(manifest))
        if broken:
            (self.source / "app.py").write_text("raise RuntimeError('startup failed')")
        build(self.source, self.package)

    def test_update_and_manual_rollback(self):
        old = self.manager.state["current"]
        self.next_version()
        self.manager.install(self.package)
        self.assertTrue(self.manager.healthy())
        self.assertEqual(self.manager.status()["version"], "0.2.0")
        self.assertEqual(self.manager.state["previous"], old)
        self.manager.rollback()
        self.assertTrue(self.manager.healthy())
        self.assertEqual(self.manager.state["current"], old)
        self.assertEqual(self.manager.status()["version"], "0.1.0")

    def test_failed_start_restores_previous_process_and_state(self):
        old = dict(self.manager.state)
        self.next_version(broken=True)
        with self.assertRaises(RuntimeError):
            self.manager.install(self.package)
        self.assertEqual(self.manager.state, old)
        self.assertEqual(json.loads(self.manager.state_path.read_text()), old)
        self.assertTrue(self.manager.healthy())
        self.assertEqual(len(list(self.manager.releases.iterdir())), 1)

    def test_invalid_package_does_not_interrupt_player(self):
        pid = self.manager.process.pid
        self.package.write_bytes(b"bad zip")
        with self.assertRaises(zipfile.BadZipFile):
            self.manager.install(self.package)
        self.assertEqual(self.manager.process.pid, pid)
        self.assertTrue(self.manager.healthy())

    def test_restart_loads_saved_release(self):
        self.manager.stop()
        self.manager.boot()
        self.assertTrue(self.manager.healthy())
        self.assertEqual(self.manager.status()["version"], "0.1.0")

    def test_boot_rolls_back_broken_current(self):
        self.next_version()
        self.manager.install(self.package)
        self.manager.stop()
        (self.manager.release_path(self.manager.state["current"]) / "app.py").write_text("raise RuntimeError('bad boot')")
        self.manager.boot()
        self.assertTrue(self.manager.healthy())
        self.assertEqual(self.manager.status()["version"], "0.1.0")


class HTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = Manager(self.root / "data", startup_timeout=2)
        auth = credentials(self.manager.data)
        password = (self.manager.data / "initial-password.txt").read_text().strip()
        self.authorization = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.manager = self.manager
        self.server.credentials = auth
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        # Wait for asynchronous update before releasing resources.
        with self.manager.lock:
            self.manager.stop()
        for handler in self.manager.log.handlers[:]:
            handler.close()
            self.manager.log.removeHandler(handler)
        self.tmp.cleanup()

    def request(self, method, path, data=None, headers=None, auth=True):
        headers = dict(headers or {})
        if auth:
            headers["Authorization"] = self.authorization
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def wait_job(self):
        deadline = time.monotonic() + 5
        while self.manager.lock.locked() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.manager.lock.locked())

    def test_auth_required_for_ui_and_logs(self):
        for path in ("/", "/api/status", "/api/logs", "/maintenance"):
            self.assertEqual(self.request("GET", path, auth=False)[0], 401)

    def test_mutation_csrf_checks(self):
        self.assertEqual(self.request("POST", "/api/rollback")[0], 403)
        self.assertEqual(self.request("POST", "/api/rollback", headers={"X-MediaStr-Request": "1", "Origin": "https://evil.example"})[0], 403)

    def test_wrong_and_unicode_credentials(self):
        for user in ("admin:wrong", "пользователь:wrong"):
            authorization = "Basic " + base64.b64encode(user.encode()).decode()
            self.assertEqual(self.request("GET", "/api/status", headers={"Authorization": authorization}, auth=False)[0], 401)

    def test_upload_size_and_lock(self):
        self.assertEqual(self.request("POST", "/api/update", b"", {"X-MediaStr-Request": "1"})[0], 400)
        self.manager.lock.acquire()
        try:
            self.assertEqual(self.request("POST", "/api/update", b"zip", {"X-MediaStr-Request": "1"})[0], 409)
        finally:
            self.manager.lock.release()

    def test_install_through_browser_endpoint_and_recovery(self):
        package = build(ROOT / "app", self.root / "release.zip")
        status, data, _ = self.request("POST", "/api/update", package.read_bytes(), {"X-MediaStr-Request": "1"})
        self.assertEqual(status, 202)
        self.wait_job()
        self.assertEqual(self.manager.job["status"], "success")
        status, data, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("На вашей волне".encode(), data)
        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(self.request("GET", "/../../credentials.json")[0], 404)
        self.manager.stop()
        self.assertEqual(self.request("GET", "/maintenance")[0], 200)
        self.assertEqual(self.request("GET", "/api/logs/download")[0], 200)
        self.assertEqual(self.request("GET", "/")[0], 503)

    def test_repository_check_install_and_repeat(self):
        headers = {"X-MediaStr-Request": "1"}
        selection = json.dumps({"commit": SHA}).encode()
        self.assertEqual(self.request("POST", "/api/repository/install", selection, headers)[0], 400)
        with patch.object(repository, "fetch", side_effect=fake_fetch) as fetch:
            self.assertEqual(self.request("POST", "/api/repository/check", headers=headers)[0], 202)
            self.wait_job()
            state = json.loads(self.request("GET", "/api/status")[1])
            self.assertTrue(state["repository"]["available"])
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(self.request("POST", "/api/repository/install", json.dumps({"commit": "b" * 40}).encode(), headers)[0], 400)
            self.assertEqual(self.request("POST", "/api/repository/install", selection, headers)[0], 202)
            self.wait_job()
            state = json.loads(self.request("GET", "/api/status")[1])
            self.assertEqual(state["job"]["status"], "success")
            self.assertEqual(state["source"]["commit"], SHA)
            self.assertFalse(state["repository"]["available"])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(self.request("POST", "/api/repository/install", selection, headers)[0], 400)

    def test_repository_network_failure_preserves_running_app(self):
        package = build(ROOT / "app", self.root / "release.zip")
        self.manager.install(package)
        pid = self.manager.process.pid
        self.manager.repository_update = {"status": "checked", "repository": repository.REPOSITORY, "commit": SHA}
        with patch.object(repository, "fetch", side_effect=ValueError("Нет связи с GitHub")):
            self.assertEqual(self.request("POST", "/api/repository/install", json.dumps({"commit": SHA}).encode(), {"X-MediaStr-Request": "1"})[0], 202)
            self.wait_job()
        self.assertEqual(self.manager.job["status"], "error")
        self.assertEqual(self.manager.process.pid, pid)
        self.assertTrue(self.manager.healthy())
        self.assertFalse(list(self.manager.data.glob("repository-*.zip")))

    def test_account_proxy_and_persistence_across_update(self):
        package = build(ROOT / "app", self.root / "release.zip")
        self.manager.install(package)
        self.assertFalse(json.loads(self.request("GET", "/api/yandex/status")[1])["configured"])
        self.assertEqual(self.request("GET", "/api/yandex/status", auth=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/yandex/logout")[0], 403)
        # Seed persistent settings, without authenticating to a real account in tests.
        path = self.manager.data / "app-data/yandex-account.json"
        path.write_text(json.dumps({"token": "private-token", "account": {"uid": "42"}, "checked_at": "2026-01-01"}))
        self.manager.install(package)
        response = self.request("GET", "/api/yandex/status")
        self.assertTrue(json.loads(response[1])["configured"])
        self.assertNotIn(b"private-token", response[1])
        self.manager.rollback()
        self.assertTrue(json.loads(self.request("GET", "/api/yandex/status")[1])["configured"])
        result = self.request("POST", "/api/yandex/login", json.dumps({"token": "bad token"}).encode(), {"X-MediaStr-Request": "1"})
        self.assertEqual(result[0], 400)
        self.assertTrue(path.exists())
        self.assertEqual(self.request("POST", "/api/yandex/logout", headers={"X-MediaStr-Request": "1"})[0], 200)
        self.assertFalse(path.exists())
        self.assertNotIn("private-token", self.manager.logs())


class Secrets(unittest.TestCase):
    def test_redaction(self):
        for text, secret in [
            ('Authorization: OAuth topsecret', 'topsecret'),
            ('{"access_token": "topsecret"}', 'topsecret'),
            ('password=topsecret', 'topsecret'),
            ('Cookie: session=public; session2=topsecret', 'topsecret'),
            ('https://music.example/file?sign=topsecret&key=other', 'topsecret'),
        ]:
            self.assertNotIn(secret, redact(text))


if __name__ == "__main__":
    unittest.main()
