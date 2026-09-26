#!/usr/bin/env python3
"""Build a dependency-free release; checksums cover the exact payload."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def build(source, output):
    manifest = json.loads((source / "manifest.json").read_text())
    files = [source / "app.py"] + sorted(p for p in (source / "web").rglob("*") if p.is_file())
    manifest["files"] = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for path in files:
            archive.write(path, path.relative_to(source).as_posix())
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] / "app")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    version = json.loads((args.source / "manifest.json").read_text())["version"]
    output = args.output or Path(__file__).resolve().parents[1] / "dist" / ("media-str-" + version + ".zip")
    print(build(args.source, output))
