import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
import repository
from maintenance import unpack
from repository_fixtures import SHA, archive, fake_fetch


class Repository(unittest.TestCase):
    def test_latest_uses_default_branch(self):
        with patch.object(repository, "fetch", side_effect=fake_fetch) as fetch:
            revision = repository.latest()
        self.assertEqual(revision["commit"], SHA)
        self.assertEqual(revision["message"], "Test update")
        self.assertEqual(fetch.call_args.args[1], "/repos/GorINIch73/volumio_Ya/commits?per_page=1")

    def test_pinned_source_produces_valid_package(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "release.zip"
            output = Path(folder) / "output"
            output.mkdir()
            with patch.object(repository, "fetch", side_effect=fake_fetch) as fetch:
                revision = repository.latest()
                repository.make_package(revision, package)
            self.assertEqual(fetch.call_args.args[1], "/GorINIch73/volumio_Ya/zip/" + SHA)
            manifest = unpack(package, output)
            self.assertEqual(manifest["source"], {"repository": repository.REPOSITORY, "commit": SHA})
            self.assertTrue((output / "web/index.html").is_file())

    def test_invalid_sha_is_rejected_before_download(self):
        with patch.object(repository, "fetch") as fetch:
            with self.assertRaises(ValueError):
                repository.make_package({"repository": repository.REPOSITORY, "commit": "../../main"}, Path("unused"))
            fetch.assert_not_called()

    def test_empty_repository(self):
        with patch.object(repository, "fetch", return_value=b"[]"):
            with self.assertRaises(ValueError):
                repository.latest()

    def test_archive_without_app(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as output:
            output.writestr("volumio_Ya-" + SHA + "/README.md", "no app yet")
        with patch.object(repository, "fetch", return_value=buffer.getvalue()):
            with self.assertRaisesRegex(ValueError, "ещё нет приложения"):
                repository.make_package({"repository": repository.REPOSITORY, "commit": SHA}, Path("unused"))

    def test_malicious_archive_paths_and_symlinks(self):
        for name, mode in [("../escape", stat.S_IFREG), ("web/link", stat.S_IFLNK)]:
            buffer = io.BytesIO(archive())
            with zipfile.ZipFile(buffer, "a") as output:
                info = zipfile.ZipInfo("volumio_Ya-" + SHA + "/app/" + name)
                info.external_attr = (mode | 0o777) << 16
                output.writestr(info, "bad")
            with patch.object(repository, "fetch", return_value=buffer.getvalue()):
                with self.assertRaises(ValueError):
                    repository.make_package({"repository": repository.REPOSITORY, "commit": SHA}, Path("unused"))

    def test_github_http_error_is_readable(self):
        with patch.object(repository.http.client, "HTTPSConnection") as connection:
            connection.return_value.getresponse.return_value.status = 429
            with self.assertRaisesRegex(ValueError, "ограничил"):
                repository.fetch("api.github.com", "/repos/test", 4096)

    def test_untrusted_host_rejected(self):
        with self.assertRaisesRegex(ValueError, "сервер"):
            repository.fetch("example.com", "/archive.zip", 4096)


if __name__ == "__main__":
    unittest.main()
