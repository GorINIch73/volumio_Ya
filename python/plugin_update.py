#!/usr/bin/env python3
"""Build a native Volumio package from a pinned revision of the fixed repository."""
import argparse
import ast
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

REPOSITORY = "GorINIch73/volumio_Ya"
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
REQUIRED = {"package.json", "package-lock.json", "index.js", "config.json", "UIConfig.json",
            "install.sh", "uninstall.sh", "app/app.py", "python/bridge.py", "lib/backend.js"}
ROOT_FILES = REQUIRED | {"README.md", "source.json"}


def fetch(host, resource, limit):
    if host not in ("api.github.com", "codeload.github.com"):
        raise ValueError("Недопустимый сервер обновлений")
    connection = http.client.HTTPSConnection(host, timeout=15)
    try:
        connection.request("GET", resource, headers={"User-Agent": "YaM-Plugin-Updater",
                           "Accept": "application/vnd.github+json" if host == "api.github.com" else "application/zip"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("GitHub вернул HTTP %s. Проверьте доступность публичного репозитория или повторите позже." % response.status)
        size = response.getheader("Content-Length")
        if size is not None and int(size) > limit:
            raise ValueError("Слишком большой ответ GitHub")
        result = bytearray()
        deadline = time.monotonic() + 60
        while True:
            if time.monotonic() > deadline:
                raise ValueError("Превышено время загрузки обновления")
            block = response.read(min(65536, limit + 1 - len(result)))
            if not block:
                break
            result.extend(block)
            if len(result) > limit:
                raise ValueError("Слишком большой ответ GitHub")
        if size is not None and len(result) != int(size):
            raise ValueError("Загрузка обновления прервана")
        return bytes(result)
    except (OSError, http.client.HTTPException):
        raise ValueError("Не удалось связаться с GitHub. Проверьте интернет и время на устройстве") from None
    finally:
        connection.close()


def selected(name):
    return name in ROOT_FILES or (name.startswith(("lib/", "python/")) and
                                  name.endswith((".js", ".py", ".json")) and "__pycache__" not in name)


def source_files(raw, sha):
    prefix = REPOSITORY.split("/")[1] + "-" + sha
    result = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 4096 or len({item.filename for item in entries}) != len(entries):
            raise ValueError("Некорректный список файлов обновления")
        if sum(item.file_size for item in entries) > MAX_EXPANDED:
            raise ValueError("Распакованное обновление превышает 32 МБ")
        for item in entries:
            name = item.filename.rstrip("/") if item.is_dir() else item.filename
            parts = PurePosixPath(name)
            if (not parts.parts or parts.parts[0] != prefix or ".." in parts.parts or "\\" in name
                    or str(parts) != name or parts.is_absolute()):
                raise ValueError("Недопустимый путь в архиве обновления")
            mode = stat.S_IFMT(item.external_attr >> 16)
            if mode not in (0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG):
                raise ValueError("Ссылки и специальные файлы в обновлении запрещены")
            relative = "/".join(parts.parts[1:])
            if not item.is_dir() and selected(relative):
                result[relative] = archive.read(item)
    if not REQUIRED.issubset(result):
        raise ValueError("В репозитории нет полного штатного плагина Volumio")
    return result


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Некорректная версия плагина")
    return tuple(int(part) for part in value.split("."))


def validate(contents, root):
    package = json.loads(contents["package.json"])
    installed = json.loads((root / "package.json").read_text())
    if package.get("name") != "yam" or package.get("volumio_info", {}).get("plugin_type") != "music_service":
        raise ValueError("Обновление предназначено для другого плагина")
    if version(package.get("version")) < version(installed["version"]):
        raise ValueError("В репозитории более старая версия. Сначала опубликуйте текущие изменения.")
    if package.get("dependencies") != installed.get("dependencies"):
        raise ValueError("Изменились зависимости. Для этой версии требуется установка нового готового ZIP.")
    if package.get("dependencies") != {"kew": "0.7.0"}:
        raise ValueError("Неподдерживаемые зависимости плагина")
    lock = json.loads(contents["package-lock.json"])
    if lock.get("packages", {}).get("node_modules/kew", {}).get("version") != "0.7.0":
        raise ValueError("Версия зависимости в lock-файле не совпадает")
    # Parse/check syntax only. Repository build/install code is not executed here.
    with tempfile.TemporaryDirectory(prefix="yam-validate-") as directory:
        for name, data in contents.items():
            if name.endswith(".py"):
                ast.parse(data, filename=name)
            elif name.endswith(".json"):
                json.loads(data)
            elif name.endswith(".js") or name.endswith(".sh"):
                file = Path(directory) / Path(name).name
                file.write_bytes(data)
                command = ["node", "--check"] if name.endswith(".js") else ["sh", "-n"]
                checked = subprocess.run(command + [str(file)], capture_output=True, timeout=10)
                if checked.returncode:
                    raise ValueError("Ошибка синтаксиса обновления: " + name)
    return package


def dependencies(root):
    directory = root / "node_modules/kew"
    contents = {}
    for file in directory.rglob("*"):
        if file.is_symlink():
            raise ValueError("Ссылка в установленной зависимости")
        if file.is_file():
            contents[file.relative_to(root).as_posix()] = file.read_bytes()
    if "node_modules/kew/kew.js" not in contents:
        raise ValueError("Нет установленной зависимости Kew")
    return contents


def write_package(contents, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(contents.items()):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def prepare(root, output):
    commits = json.loads(fetch("api.github.com", "/repos/" + REPOSITORY + "/commits?per_page=1", 256 * 1024))
    sha = commits[0].get("sha") if isinstance(commits, list) and commits and isinstance(commits[0], dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Не удалось определить сборку репозитория")
    try:
        installed = json.loads((root / "source.json").read_text())
    except (OSError, ValueError):
        installed = {}
    if installed.get("repository") == REPOSITORY and installed.get("commit") == sha:
        return {"current": True, "commit": sha}
    raw = fetch("codeload.github.com", "/" + REPOSITORY + "/zip/" + sha, MAX_ARCHIVE)
    contents = source_files(raw, sha)
    package = validate(contents, root)
    contents.update(dependencies(root))
    contents["source.json"] = json.dumps({"repository": REPOSITORY, "commit": sha}).encode()
    write_package(contents, output)
    return {"version": package["version"], "commit": sha, "repository": REPOSITORY}


def backup(root, output):
    contents = {}
    for name in ROOT_FILES:
        file = root / name
        if file.is_file():
            contents[name] = file.read_bytes()
    for directory in ("lib", "python"):
        for file in (root / directory).rglob("*"):
            name = file.relative_to(root).as_posix()
            if file.is_file() and selected(name):
                contents[name] = file.read_bytes()
    if not REQUIRED.issubset(contents):
        raise ValueError("Не удалось сохранить предыдущий пакет плагина")
    package = json.loads(contents["package.json"])
    package["category"] = "music_service"  # Required by Volumio's CLI update helper.
    contents["package.json"] = json.dumps(package, ensure_ascii=False).encode()
    contents.update(dependencies(root))
    write_package(contents, output)
    return {"saved": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "backup"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = (prepare if args.action == "prepare" else backup)(args.root, args.output)
    except ValueError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        sys.exit(1)
    except Exception as error:
        print(json.dumps({"error": "Не удалось подготовить пакет (" + type(error).__name__ + "). Проверьте репозиторий и свободное место."}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False))
