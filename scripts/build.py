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


def build_plugin(root, output):
    """Volumio ZIP: standard files at archive root, dependencies included."""
    package = json.loads((root / "package.json").read_text())
    dependency = root / "node_modules/kew/package.json"
    if not dependency.exists():
        raise ValueError("Run npm ci --ignore-scripts before building the plugin")
    if json.loads(dependency.read_text())["version"] != package["dependencies"]["kew"]:
        raise ValueError("Kew version mismatch; run npm ci --ignore-scripts")
    names = ["package.json", "package-lock.json", "index.js", "config.json", "UIConfig.json",
             "install.sh", "uninstall.sh", "app/app.py", "README.md"]
    for directory in ("lib", "python", "node_modules/kew"):
        names.extend(path.relative_to(root).as_posix() for path in sorted((root / directory).rglob("*"))
                     if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (root / name).read_bytes())
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] / "app")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--service", type=Path, default=Path(__file__).resolve().parents[1] / "service")
    parser.add_argument("--app-only", action="store_true", help="Build an app-only package for installations without the full-update launcher")
    parser.add_argument("--standalone", action="store_true", help="Build a legacy standalone update, not a Volumio plugin")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.standalone or args.app_only:
        version = json.loads((args.source / "manifest.json").read_text())["version"]
        output = args.output or root / "dist" / ("media-str-standalone-" + version + ".zip")
        print(build(args.source, output, None if args.app_only else args.service))
    else:
        version = json.loads((root / "package.json").read_text())["version"]
        output = args.output or root / "dist" / ("media_str-" + version + ".zip")
        print(build_plugin(root, output))
