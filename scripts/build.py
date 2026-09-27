#!/usr/bin/env python3
"""Build a dependency-free release; checksums cover the exact payload."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def build(source, output, service=None):
    manifest = json.loads((source / "manifest.json").read_text())
    files = [source / "app.py"] + sorted(p for p in (source / "web").rglob("*") if p.is_file())
    payload = {p.relative_to(source).as_posix(): p.read_bytes() for p in files}
    if service is not None:
        for path in sorted(service.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc" and path.name != "launcher.py":
                payload["service/" + path.relative_to(service).as_posix()] = path.read_bytes()
        if not {"service/maintenance.py", "service/repository.py"}.issubset(payload):
            raise ValueError("Full package requires the maintenance service")
        manifest.update(protocol=2, min_launcher=manifest.get("min_launcher", 1))
    manifest["files"] = {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for name, data in payload.items():
            archive.writestr(name, data)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] / "app")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--service", type=Path, default=Path(__file__).resolve().parents[1] / "service")
    args = parser.parse_args()
    version = json.loads((args.source / "manifest.json").read_text())["version"]
    output = args.output or Path(__file__).resolve().parents[1] / "dist" / ("media-str-" + version + ".zip")
    print(build(args.source, output, args.service))
