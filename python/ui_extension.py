#!/usr/bin/env python3
"""Install/remove the small YaM browser hook without replacing Volumio's bundle."""
import argparse
import gzip
from pathlib import Path
import re

START = '<!-- yam-now-playing:start -->'
END = '<!-- yam-now-playing:end -->'


def update(index, source=None):
    original = index.read_text()
    cleaned = re.sub(re.escape(START) + r'.*?' + re.escape(END), '', original, flags=re.S)
    if source is not None:
        if 'ng-app="volumio"' not in cleaned or '</body>' not in cleaned:
            raise ValueError('Unsupported Volumio UI: Angular volumio page expected')
        script = source.read_text()
        if '</script' in script.lower():
            raise ValueError('Invalid YaM UI script')
        cleaned = cleaned.replace('</body>', START + '<script>\n' + script + '\n</script>' + END + '</body>', 1)
    if cleaned != original:
        index.write_text(cleaned)
        compressed = index.with_name(index.name + '.gz')
        if compressed.exists():
            compressed.write_bytes(gzip.compress(cleaned.encode(), mtime=0))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'remove'))
    parser.add_argument('--index', type=Path, default=Path('/volumio/http/www/index.html'))
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / 'lib/now-playing.js'
    if args.action == 'remove' and not args.index.exists():
        raise SystemExit(0)
    update(args.index, source if args.action == 'install' else None)
