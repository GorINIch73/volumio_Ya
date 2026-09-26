"""Download a pinned public GitHub revision; never run repository build scripts."""
import hashlib
import http.client
import io
import json
from pathlib import PurePosixPath
import re
import stat
import time
import zipfile

REPOSITORY = "GorINIch73/volumio_Ya"
REPOSITORY_URL = "https://github.com/" + REPOSITORY
MAX_DOWNLOAD = 16 * 1024 * 1024
MAX_UNPACKED = 32 * 1024 * 1024


def fetch(host, path, limit):
    # Fixed callers/hosts, HTTPS verification on; redirects are deliberately not followed.
    if host not in ("api.github.com", "codeload.github.com"):
        raise ValueError("Недопустимый сервер обновлений")
    connection = http.client.HTTPSConnection(host, timeout=15)
    try:
        connection.request("GET", path, headers={"User-Agent": "Media-Str-Updater/0.1",
                           "Accept": "application/vnd.github+json" if host == "api.github.com" else "application/zip"})
        response = connection.getresponse()
        if response.status == 404:
            raise ValueError("Репозиторий или сборка не найдены. Нужен публичный репозиторий с опубликованным кодом")
        if response.status in (403, 429):
            raise ValueError("GitHub ограничил запросы. Повторите проверку позже")
        if response.status != 200:
            raise ValueError("GitHub вернул ошибку HTTP %s" % response.status)
        declared = response.getheader("Content-Length")
        if declared is not None and int(declared) > limit:
            raise ValueError("Ответ GitHub превышает допустимый размер")
        result = bytearray()
        deadline = time.monotonic() + 60
        while True:
            if time.monotonic() > deadline:
                raise ValueError("Превышено время скачивания обновления")
            block = response.read(min(65536, limit + 1 - len(result)))
            if not block:
                break
            result.extend(block)
            if len(result) > limit:
                raise ValueError("Ответ GitHub превышает допустимый размер")
        if declared is not None and len(result) != int(declared):
            raise ValueError("Загрузка с GitHub прервана")
        return bytes(result)
    except (OSError, http.client.HTTPException) as error:
        raise ValueError("Не удалось связаться с GitHub. Проверьте интернет и время на устройстве") from error
    finally:
        connection.close()


def latest():
    # Omitting sha selects the repository's default branch, not a hardcoded main/master.
    response = json.loads(fetch("api.github.com", "/repos/" + REPOSITORY + "/commits?per_page=1", 256 * 1024))
    if not isinstance(response, list) or not response or not isinstance(response[0], dict):
        raise ValueError("В репозитории пока нет доступной сборки")
    commit = response[0]
    sha = commit.get("sha", "")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("GitHub вернул некорректный идентификатор сборки")
    details = commit.get("commit", {})
    return {"repository": REPOSITORY, "commit": sha,
            "message": str(details.get("message", "")).split("\n")[0][:200],
            "url": REPOSITORY_URL + "/commit/" + sha}


def make_package(revision, output):
    sha = revision.get("commit", "")
    if revision.get("repository") != REPOSITORY or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Неизвестная сборка репозитория")
    data = fetch("codeload.github.com", "/" + REPOSITORY + "/zip/" + sha, MAX_DOWNLOAD)
    expected_root = REPOSITORY.split("/")[1] + "-" + sha
    contents = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 2048 or len({item.filename for item in entries}) != len(entries):
            raise ValueError("Некорректный список файлов репозитория")
        if sum(item.file_size for item in entries) > MAX_UNPACKED:
            raise ValueError("Архив репозитория больше 32 МБ после распаковки")
        for item in entries:
            name = item.filename.rstrip("/") if item.is_dir() else item.filename
            path = PurePosixPath(name)
            if (not path.parts or path.parts[0] != expected_root or ".." in path.parts
                    or "\\" in name or str(path) != name or path.is_absolute()):
                raise ValueError("Некорректный путь в архиве репозитория")
            if item.is_dir():
                continue
            relative = "/".join(path.parts[1:])
            if not relative.startswith("app/"):
                continue
            name = relative[4:]
            if name not in ("manifest.json", "app.py") and not name.startswith("web/"):
                continue
            if stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG):
                raise ValueError("В приложении обнаружена ссылка или специальный файл")
            if name == "manifest.json" and item.file_size > 65536:
                raise ValueError("Манифест репозитория слишком большой")
            contents[name] = archive.read(item)
        if not {"manifest.json", "app.py", "web/index.html"}.issubset(contents):
            raise ValueError("В репозитории ещё нет приложения: нужны app/manifest.json, app/app.py и app/web/index.html")
        if len(contents) > 256:
            raise ValueError("Слишком много файлов приложения")
    manifest = json.loads(contents.pop("manifest.json"))
    if not isinstance(manifest, dict):
        raise ValueError("Некорректный манифест репозитория")
    manifest["source"] = {"repository": REPOSITORY, "commit": sha}
    manifest["files"] = {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}
    # The stable supervisor constructs the package itself; repository scripts are not executed.
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("manifest.json", json.dumps(manifest))
        for name, value in contents.items():
            package.writestr(name, value)
