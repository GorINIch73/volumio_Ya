import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "python"))
from build import build_plugin
from bridge import Backend, serve

class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_worker_protocol_account_persistence_and_private_permissions(self):
        backend = Backend(self.root)
        backend.account.validator = lambda token: {"uid": "42", "display_name": "Test"}
        source = io.StringIO('\n'.join(json.dumps(request) for request in [
            {"id": 1, "method": "login", "params": {"token": "secret-token"}},
            {"id": 2, "method": "status"},
            {"id": 3, "method": "invalid"}
        ]) + '\n')
        output = io.StringIO()
        serve(backend, source, output)
        self.assertNotIn("secret-token", output.getvalue())
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        self.assertTrue(replies[1]["result"]["configured"])
        self.assertIn("error", replies[2])
        self.assertEqual((self.root / "yandex-account.json").stat().st_mode & 0o777, 0o600)
        self.assertTrue(Backend(self.root).dispatch("status", {})["configured"])
        backend.dispatch("logout", {})
        self.assertFalse(backend.dispatch("status", {})["configured"])

    def test_worker_unexpected_errors_do_not_expose_secrets(self):
        backend = Backend(self.root)
        output = io.StringIO()
        with patch.object(backend, "dispatch", side_effect=RuntimeError("secret-token")):
            serve(backend, io.StringIO('{"id":1,"method":"status"}\n'), output)
        self.assertNotIn("secret-token", output.getvalue())
        self.assertIn("error", json.loads(output.getvalue()))

    def test_track_metadata_does_not_resolve_stream(self):
        backend = Backend(self.root)
        with patch.object(backend.music, "session", return_value=("token", "42")), \
                patch.object(backend.music, "yandex", return_value=[{"id": "123", "title": "Song"}]), \
                patch.object(backend.music, "stream", return_value="https://test.yandex.net/audio") as stream:
            self.assertEqual(backend.dispatch("track", {"id": "123"})["title"], "Song")
            stream.assert_not_called()
            backend.dispatch("stream", {"id": "123"})
            stream.assert_called_once_with("token", "123")

    def test_worker_exits_on_stdin_eof(self):
        result = subprocess.run([sys.executable, str(ROOT / "python/bridge.py"), "--data", str(self.root)],
                                input='{"id":1,"method":"status"}\n', text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["result"]["configured"])

    def test_plugin_package_is_self_contained(self):
        package = build_plugin(ROOT, self.root / "plugin.zip")
        with zipfile.ZipFile(package) as archive:
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
            self.assertTrue({"package.json", "index.js", "UIConfig.json", "config.json", "install.sh",
                             "uninstall.sh", "app/app.py", "python/bridge.py", "node_modules/kew/kew.js"} <= names)
            allowed = {"lib", "python", "app", "node_modules"}
            self.assertTrue(all("/" not in name or name.split("/")[0] in allowed for name in names))
            self.assertEqual({name for name in names if name.startswith("app/")}, {"app/app.py"})
            self.assertFalse(any("yandex-account" in name for name in names))
            self.assertEqual(archive.getinfo("install.sh").external_attr >> 16 & 0o777, 0o755)
            archive.extractall(self.root / "extracted")
        result = subprocess.run(["node", "-e", "require('./index.js')"], cwd=self.root / "extracted",
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, "python/bridge.py", "--data", str(self.root / "data")],
                                cwd=self.root / "extracted", input='{"id":1,"method":"status"}\n',
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("result", json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
