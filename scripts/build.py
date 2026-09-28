#!/usr/bin/env python3
"""Build the Volumio plugin ZIP with its runtime dependencies."""
import argparse
import json
from pathlib import Path
import zipfile


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
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / "package.json").read_text())["version"]
    output = args.output or root / "dist" / ("yam-" + version + ".zip")
    print(build_plugin(root, output))
