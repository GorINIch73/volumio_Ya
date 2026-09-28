import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
import plugin_update as update

SHA = "a" * 40


class PluginUpdates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "plugin.zip"
        self.contents = {name: (ROOT / name).read_bytes() for name in update.REQUIRED}
        for directory in ("lib", "python"):
            for file in (ROOT / directory).rglob("*"):
                name = file.relative_to(ROOT).as_posix()
                if file.is_file() and update.selected(name):
                    self.contents[name] = file.read_bytes()

    def archive(self, contents=None, extras=None):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in (self.contents if contents is None else contents).items():
                archive.writestr("volumio_Ya-" + SHA + "/" + name, value)
            for name, value in (extras or {}).items():
                archive.writestr(name, value)
        return data.getvalue()

    def test_prepare_pins_archive_includes_dependencies_and_no_secrets(self):
        # Malicious build scripts must not run, and account data must not be included.
        contents = dict(self.contents, **{"scripts/build.py": b"raise RuntimeError('must not execute')",
                                        "app-data/yandex-account.json": b'{"token":"secret"}'})
        with patch.object(update, "fetch", side_effect=[json.dumps([{"sha": SHA}]).encode(), self.archive(contents)]) as fetch:
            result = update.prepare(ROOT, self.output)
        self.assertEqual(result["commit"], SHA)
        self.assertEqual(fetch.call_args.args[1], "/GorINIch73/volumio_Ya/zip/" + SHA)
        with zipfile.ZipFile(self.output) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("node_modules/kew/kew.js", archive.namelist())
            self.assertEqual(json.loads(archive.read("source.json"))["commit"], SHA)
            self.assertNotIn("scripts/build.py", archive.namelist())
            self.assertNotIn("app-data/yandex-account.json", archive.namelist())
            self.assertEqual(archive.getinfo("install.sh").external_attr >> 16 & 0o777, 0o755)

    def test_archive_traversal_duplicate_and_symlink_rejected(self):
        for name in ("../escape", "volumio_Ya-" + SHA + "/../escape", "/absolute"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                update.source_files(self.archive(extras={name: b"bad"}), SHA)
        data = io.BytesIO(self.archive())
        with zipfile.ZipFile(data, "a") as archive:
            info = zipfile.ZipInfo("volumio_Ya-" + SHA + "/lib/symlink.js")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
        with self.assertRaisesRegex(ValueError, "Ссылки"):
            update.source_files(data.getvalue(), SHA)

    def test_oversized_or_incomplete_source_rejected(self):
        with patch.object(update, "MAX_EXPANDED", 10), self.assertRaisesRegex(ValueError, "32 МБ"):
            update.source_files(self.archive(), SHA)
        with self.assertRaisesRegex(ValueError, "полного"):
            update.source_files(self.archive({"README.md": b"empty repo"}), SHA)

    def test_downgrade_wrong_plugin_dependency_change_and_syntax_rejected(self):
        for change, message in (({"version": "0.1.0"}, "старая"),
                                ({"name": "other"}, "другого"),
                                ({"dependencies": {"kew": "0.8.0"}}, "зависимости")):
            contents = dict(self.contents)
            package = json.loads(contents["package.json"])
            package.update(change)
            contents["package.json"] = json.dumps(package).encode()
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                update.validate(contents, ROOT)
        contents = dict(self.contents, **{"index.js": b"const invalid = ;"})
        with self.assertRaisesRegex(ValueError, "синтаксиса"):
            update.validate(contents, ROOT)

    def test_backup_is_installable_code_only(self):
        update.backup(ROOT, self.output)
        with zipfile.ZipFile(self.output) as archive:
            self.assertTrue(update.REQUIRED <= set(archive.namelist()))
            self.assertIn("node_modules/kew/kew.js", archive.namelist())
            self.assertFalse(any("account" in name or "journal" in name and not name.endswith(".js")
                                 for name in archive.namelist()))

    def test_matching_commit_skips_download(self):
        root = Path(self.temp.name)
        (root / "source.json").write_text(json.dumps({"repository": update.REPOSITORY, "commit": SHA}))
        with patch.object(update, "fetch", return_value=json.dumps([{"sha": SHA}]).encode()) as fetch:
            self.assertTrue(update.prepare(root, self.output)["current"])
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(self.output.exists())

    def test_invalid_commit_does_not_download_or_create_package(self):
        with patch.object(update, "fetch", return_value=b'[{"sha":"bad;command"}]') as fetch:
            with self.assertRaises(ValueError):
                update.prepare(ROOT, self.output)
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
