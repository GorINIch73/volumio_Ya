import gzip
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from ui_extension import update, START


class UIExtension(unittest.TestCase):
    def test_install_update_remove_preserve_other_ui_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = root / 'index.html'
            source = root / 'hook.js'
            original = '<html ng-app="volumio"><body><script src="app.js"></script></body></html>'
            index.write_text(original)
            compressed = root / 'index.html.gz'
            compressed.write_bytes(gzip.compress(original.encode()))
            source.write_text('/* hook v1 */')
            update(index, source)
            update(index, source)
            self.assertEqual(index.read_text().count(START), 1)
            source.write_text('/* hook v2 */')
            update(index, source)
            self.assertNotIn('hook v1', index.read_text())
            self.assertIn('hook v2', index.read_text())
            self.assertEqual(gzip.decompress(compressed.read_bytes()).decode(), index.read_text())
            update(index)
            self.assertEqual(index.read_text(), original)
            self.assertEqual(gzip.decompress(compressed.read_bytes()).decode(), original)

    def test_unsupported_ui_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / 'index.html'
            index.write_text('<html>Unknown UI</html>')
            with self.assertRaises(ValueError):
                update(index, index)
            self.assertEqual(index.read_text(), '<html>Unknown UI</html>')
