#!/usr/bin/env python3
"""Stable authenticated installer/supervisor. Python 3.9+, standard library only."""
import argparse
import base64
import hashlib
import hmac
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import zipfile
import repository

MAX_PACKAGE = 16 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MAX_FILES = 256
VERSION = "0.2.0"


def redact(value):
    value = str(value)
    value = re.sub(r"(?im)((?:set-cookie|cookie)\s*:\s*)[^\r\n]+", r"\1[hidden]", value)
    value = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:Bearer|OAuth|Basic)?\s*[^\s,;]+", r"\1[hidden]", value)
    value = re.sub(r'''(?i)((?:[\w-]*token|password|cookie|secret|api[_-]?key)\s*["']?\s*[:=]\s*["']?)[^\s,;"'&]+''', r"\1[hidden]", value)
    # Signed media URLs can carry credentials under arbitrary parameter names.
    return re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[hidden]", value)


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(value, file)
        file.flush()
        os.fsync(file.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    sync_directory(path.parent)


def unpack(package, destination):
    """Validate everything before writing; archives are never extractall'ed."""
    if package.stat().st_size > MAX_PACKAGE:
        raise ValueError("Пакет больше 16 МБ")
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        if len(entries) > MAX_FILES or len(names) != len(set(names)):
            raise ValueError("Слишком много файлов или повторяющиеся пути")
        if sum(item.file_size for item in entries) > MAX_EXPANDED:
            raise ValueError("Распакованный пакет больше 32 МБ")
        for item in entries:
            name = item.filename
            path = PurePosixPath(name)
            mode = item.external_attr >> 16
            if (path.is_absolute() or ".." in path.parts or "\\" in name or str(path) != name
                    or not name or item.is_dir() or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))):
                raise ValueError("Недопустимый путь или тип файла")
            if name not in ("manifest.json", "app.py") and not name.startswith("web/"):
                raise ValueError("Файл за пределами приложения")
        if "manifest.json" not in names or archive.getinfo("manifest.json").file_size > 65536:
            raise ValueError("Отсутствует корректный манифест")
        manifest = json.loads(archive.read("manifest.json"))
        if not isinstance(manifest, dict):
            raise ValueError("Некорректный манифест")
        if manifest.get("name") != "media-str" or manifest.get("protocol") != 1:
            raise ValueError("Несовместимый формат пакета")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", str(manifest.get("version", ""))):
            raise ValueError("Некорректная версия")
        minimum = manifest.get("min_python")
        if not isinstance(minimum, list) or len(minimum) != 2 or any(type(x) is not int for x in minimum):
            raise ValueError("Не указана версия Python")
        if tuple(minimum) > sys.version_info[:2]:
            raise ValueError("Требуется более новая версия Python")
        hashes = manifest.get("files")
        if not isinstance(hashes, dict) or set(hashes) != set(names) - {"manifest.json"}:
            raise ValueError("Состав пакета не совпадает с манифестом")
        if not {"app.py", "web/index.html"}.issubset(hashes):
            raise ValueError("В пакете нет приложения или интерфейса")
        contents = {}
        for name, expected in hashes.items():
            data = archive.read(name)
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("Контрольная сумма файла не совпадает")
            contents[name] = data
        compile(contents["app.py"], "app.py", "exec")
        for name, data in contents.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
        atomic_json(destination / "manifest.json", manifest)
        for directory in sorted((p for p in destination.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            sync_directory(directory)
        sync_directory(destination)
        return manifest


class Manager:
    def __init__(self, data, startup_timeout=12):
        self.data = Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.releases = self.data / "releases"
        self.releases.mkdir(exist_ok=True)
        self.state_path = self.data / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"current": None, "previous": None}
        self.lock = threading.Lock()
        self.process = None
        self.port = None
        self.nonce = None
        self.timeout = startup_timeout
        self.job = {"status": "idle", "message": "Готово"}
        self.repository_update = {"status": "unchecked", "message": "Проверка ещё не выполнялась"}
        self.log = logging.getLogger("media-str-" + str(self.data))
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        handler = RotatingFileHandler(self.data / "service.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(message)s"))
        self.log.addHandler(handler)

    def release_path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9.-]+", name) or name in (".", ".."):
            raise ValueError("Некорректный идентификатор выпуска")
        return self.releases / name

    def healthy(self):
        process, port, nonce = self.process, self.port, self.nonce
        if not process or process.poll() is not None:
            return False
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            body = json.loads(response.read(4096))
            return response.status == 200 and isinstance(body, dict) and body.get("nonce") == nonce
        except (OSError, ValueError, http.client.HTTPException):
            return False
        finally:
            connection.close()

    def stop(self):
        process, self.process = self.process, None
        if process:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)

    def start(self, release):
        self.stop()
        root = self.release_path(release)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.nonce = secrets.token_hex(24)
        app_data = self.data / "app-data"
        app_data.mkdir(exist_ok=True, mode=0o700)
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
                       "MEDIA_STR_APP_PORT": str(self.port), "MEDIA_STR_HEALTH_NONCE": self.nonce,
                       "MEDIA_STR_APP_DATA": str(app_data)}
        self.process = subprocess.Popen([sys.executable, str(root / "app.py")], cwd=str(root), env=environment,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        process = self.process

        def capture():
            with process.stdout:
                while True:
                    line = process.stdout.readline(4096)
                    if not line:
                        break
                    self.log.info("app: %s", line.decode("utf-8", errors="replace").rstrip())
        threading.Thread(target=capture, daemon=True).start()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return
            if process.poll() is not None:
                break
            time.sleep(0.1)
        self.stop()
        raise RuntimeError("Новая версия не прошла проверку запуска")

    def boot(self):
        current = self.state.get("current")
        if not current:
            return
        try:
            self.start(current)
        except Exception:
            self.log.exception("Не удалось запустить текущую версию")
            previous = self.state.get("previous")
            if previous:
                try:
                    self.start(previous)
                    self.state = {"current": previous, "previous": current}
                    atomic_json(self.state_path, self.state)
                    self.job = {"status": "error", "message": "После запуска выполнен откат"}
                except Exception:
                    self.log.exception("Откат при запуске не удался; обслуживание доступно")

    def activate(self, candidate):
        old = self.state.get("current")
        try:
            self.start(candidate)
            new_state = {"current": candidate, "previous": old}
            atomic_json(self.state_path, new_state)
            self.state = new_state
        except Exception:
            if old:
                try:
                    self.start(old)
                    self.log.warning("Предыдущая версия восстановлена")
                except Exception:
                    self.log.exception("Восстановление не удалось; обслуживание доступно")
            raise
        self.log.info("Активирована версия %s", candidate)
        keep = set(self.state.values())
        for directory in self.releases.iterdir():
            if directory.is_dir() and directory.name not in keep:
                try:
                    shutil.rmtree(directory)
                except OSError:
                    # Cleanup failure must never remove or undo the successful release.
                    self.log.warning("Не удалось удалить старый выпуск %s", directory.name)

    def install(self, package):
        if shutil.disk_usage(self.data).free < MAX_EXPANDED * 3:
            raise ValueError("Недостаточно свободного места: требуется 96 МБ")
        with tempfile.TemporaryDirectory(prefix="stage-", dir=self.data) as temp:
            stage = Path(temp)
            manifest = unpack(package, stage)
            candidate = manifest["version"] + "-" + secrets.token_hex(6)
            os.replace(stage, self.release_path(candidate))
            sync_directory(self.releases)
            try:
                self.activate(candidate)
            except Exception:
                shutil.rmtree(self.release_path(candidate), ignore_errors=True)
                raise

    def rollback(self):
        previous = self.state.get("previous")
        if not previous:
            raise ValueError("Предыдущая версия отсутствует")
        self.activate(previous)

    def perform(self, action, package=None, revision=None):
        try:
            self.job = {"status": "running", "message": "Проверка и запуск версии…"}
            if action == "repository-check":
                self.job = {"status": "running", "message": "Проверяем репозиторий GitHub…"}
                revision = repository.latest()
                current = self.current_manifest().get("source", {})
                self.repository_update = dict(revision, status="checked",
                                              available=current != {"repository": revision["repository"], "commit": revision["commit"]})
                self.job = {"status": "success", "message": "Найдена сборка " + revision["commit"][:8]}
                return
            if action == "repository-install":
                self.job = {"status": "running", "message": "Скачиваем сборку " + revision["commit"][:8] + " с GitHub…"}
                descriptor, name = tempfile.mkstemp(prefix="repository-", suffix=".zip", dir=self.data)
                os.close(descriptor)
                package = Path(name)
                repository.make_package(revision, package)
                self.job = {"status": "running", "message": "Проверяем и запускаем скачанное обновление…"}
                self.install(package)
            elif action == "update":
                self.install(package)
            else:
                self.rollback()
            self.job = {"status": "success", "message": "Версия запущена и проверена"}
        except Exception as error:
            self.job = {"status": "error", "message": redact(str(error))}
            if action == "repository-check":
                self.repository_update = {"status": "error", "message": redact(str(error))}
            self.log.exception("Операция %s завершилась ошибкой", action)
        finally:
            if package:
                package.unlink(missing_ok=True)
            self.lock.release()

    def current_manifest(self):
        if self.state.get("current"):
            try:
                return json.loads((self.release_path(self.state["current"]) / "manifest.json").read_text())
            except (OSError, ValueError, KeyError):
                pass
        return {}

    def status(self):
        manifest = self.current_manifest()
        repository_update = dict(self.repository_update)
        if repository_update.get("status") == "checked":
            repository_update["available"] = manifest.get("source") != {"repository": repository.REPOSITORY, "commit": repository_update["commit"]}
        return {"version": manifest.get("version"), "source": manifest.get("source"),
                "repository": repository_update, "maintenance_version": VERSION, "healthy": self.healthy(),
                "previous": self.state.get("previous"), "job": self.job,
                "python": ".".join(map(str, sys.version_info[:3])), "player_connected": False}

    def logs(self):
        path = self.data / "service.log"
        if not path.exists():
            return ""
        with path.open("rb") as file:
            file.seek(max(0, path.stat().st_size - 128 * 1024))
            return redact(file.read().decode("utf-8", errors="replace"))


RECOVERY = b'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Media Str recovery</title><body><h1>Media Str maintenance</h1><p>This page works independently of the music interface.</p><p><a href="/">Interface</a> | <a href="/api/status">Status</a> | <a href="/api/logs/download">Download logs</a></p><form action="/api/update" method="post" enctype="application/octet-stream"><input type="file" id="package" accept=".zip"><button type="button" id="upload">Update trusted package</button><button type="button" id="rollback">Rollback</button></form><p><button type="button" id="repo-check">Check GitHub</button><button type="button" id="repo-install" disabled>Install from GitHub</button></p><pre id="result"></pre><script src="/maintenance.js"></script></body></html>'''
RECOVERY_JS = b'''let revision=null;const result=document.querySelector('#result');async function action(path,body){try{const r=await fetch(path,{method:'POST',headers:{'X-MediaStr-Request':'1','Content-Type':'application/octet-stream'},body});result.textContent=JSON.stringify(await r.json(),null,2)}catch(e){result.textContent=String(e)}}document.querySelector('#upload').onclick=()=>{const f=document.querySelector('#package').files[0];if(f&&confirm('Install this trusted package?'))action('/api/update',f)};document.querySelector('#rollback').onclick=()=>{if(confirm('Restore previous version?'))action('/api/rollback')};document.querySelector('#repo-check').onclick=()=>action('/api/repository/check');document.querySelector('#repo-install').onclick=()=>{const chosen=revision;if(chosen&&chosen.available&&confirm('Install GitHub revision '+chosen.commit.slice(0,8)+'?'))action('/api/repository/install',JSON.stringify({commit:chosen.commit}))};setInterval(async()=>{try{const state=await(await fetch('/api/status')).json();revision=state.repository;document.querySelector('#repo-check').disabled=state.job.status==='running';document.querySelector('#repo-install').disabled=state.job.status==='running'||!revision.available;result.textContent=JSON.stringify(state,null,2)}catch(e){}},2000);'''


class Handler(BaseHTTPRequestHandler):
    server_version = "MediaStr"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def authorized(self):
        try:
            scheme, encoded = self.headers.get("Authorization", "").split(" ", 1)
            if scheme != "Basic" or len(encoded) > 2048:
                return False
            username, password = base64.b64decode(encoded, validate=True).decode().split(":", 1)
            credentials = self.server.credentials
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(credentials["salt"]), 200000).hex()
            return hmac.compare_digest(username.encode(), b"admin") and hmac.compare_digest(digest, credentials["hash"])
        except (ValueError, UnicodeError):
            return False

    def respond(self, status, data, kind="application/json", extra=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def authenticate(self):
        if self.authorized():
            return True
        self.close_connection = True
        self.respond(401, {"error": "Требуется вход"}, extra={"WWW-Authenticate": 'Basic realm="Media Str", charset="UTF-8"'})
        return False

    def proxy_account(self, method, path, body=None):
        manager = self.server.manager
        process, port, nonce = manager.process, manager.port, manager.nonce
        if not process or process.poll() is not None:
            self.respond(503, {"error": "Приложение недоступно. Дождитесь завершения обновления"})
            return
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=45)
        try:
            connection.request(method, path, body=body, headers={"X-MediaStr-Bridge": nonce, "Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("Ответ слишком большой")
            # Do not forward HTML/internal tracebacks or log account request/response bodies.
            value = json.loads(data)
            self.respond(response.status, value)
        except (OSError, ValueError, http.client.HTTPException):
            self.respond(502, {"error": "Не удалось получить ответ приложения. Попробуйте ещё раз"})
        finally:
            connection.close()

    def do_GET(self):
        if not self.authenticate():
            return
        manager = self.server.manager
        path = urlsplit(self.path).path
        if path == "/api/status":
            self.respond(200, manager.status())
        elif path in ("/api/yandex/status", "/api/music/library", "/api/music/state"):
            self.proxy_account("GET", self.path)
        elif path in ("/api/logs", "/api/logs/download"):
            data = manager.logs()
            if path.endswith("download"):
                self.respond(200, data.encode(), "text/plain", {"Content-Disposition": 'attachment; filename="media-str.log"'})
            else:
                self.respond(200, {"text": data})
        elif path == "/maintenance":
            self.respond(200, RECOVERY, "text/html")
        elif path == "/maintenance.js":
            self.respond(200, RECOVERY_JS, "text/javascript")
        elif path.startswith("/api/"):
            self.respond(404, {"error": "Неизвестный запрос"})
        else:
            process, port = manager.process, manager.port
            if not process or process.poll() is not None:
                self.respond(503, RECOVERY, "text/html")
                return
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", self.path)
                response = connection.getresponse()
                data = response.read(MAX_EXPANDED + 1)
                if len(data) > MAX_EXPANDED:
                    raise ValueError("Ответ слишком большой")
                self.respond(response.status, data, response.getheader("Content-Type", "text/plain").split(";")[0])
            except (OSError, ValueError, http.client.HTTPException):
                self.respond(503, RECOVERY, "text/html")
            finally:
                connection.close()

    def do_POST(self):
        self.close_connection = True
        if not self.authenticate():
            return
        origin = self.headers.get("Origin")
        if (self.headers.get("X-MediaStr-Request") != "1" or
                (origin and origin != "http://" + self.headers.get("Host", ""))):
            self.respond(403, {"error": "Запрос отклонён"})
            return
        path = urlsplit(self.path).path
        if path in ("/api/yandex/login", "/api/yandex/check", "/api/yandex/logout", "/api/music/action"):
            manager = self.server.manager
            if not manager.lock.acquire(blocking=False):
                self.respond(409, {"error": "Дождитесь завершения обновления или другой операции"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 <= length <= 16384:
                    raise ValueError()
                body = self.rfile.read(length) if length else None
                self.proxy_account("POST", path, body)
            except (ValueError, OSError):
                self.respond(400, {"error": "Некорректный запрос аккаунта"})
            finally:
                manager.lock.release()
            return
        if path not in ("/api/update", "/api/rollback", "/api/repository/check", "/api/repository/install"):
            self.respond(404, {"error": "Неизвестная операция"})
            return
        manager = self.server.manager
        if not manager.lock.acquire(blocking=False):
            self.respond(409, {"error": "Другая операция уже выполняется"})
            return
        package = None
        revision = None
        try:
            if path == "/api/update":
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Требуется Content-Length")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_PACKAGE:
                    raise ValueError("Размер пакета должен быть от 1 байта до 16 МБ")
                fd, name = tempfile.mkstemp(prefix="upload-", suffix=".zip", dir=manager.data)
                package = Path(name)
                with os.fdopen(fd, "wb") as file:
                    remaining = length
                    while remaining:
                        block = self.rfile.read(min(65536, remaining))
                        if not block:
                            raise ValueError("Загрузка пакета прервана")
                        file.write(block)
                        remaining -= len(block)
            if path == "/api/repository/install":
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 256:
                    raise ValueError("Требуется идентификатор проверенной сборки")
                selection = json.loads(self.rfile.read(length))
                if manager.repository_update.get("status") != "checked":
                    raise ValueError("Сначала проверьте обновления в репозитории")
                revision = dict(manager.repository_update)
                if not isinstance(selection, dict) or selection.get("commit") != revision["commit"]:
                    raise ValueError("Результат проверки изменился. Проверьте обновления ещё раз")
                if not manager.status()["repository"]["available"]:
                    raise ValueError("Эта сборка уже установлена")
                action = "repository-install"
            elif path == "/api/repository/check":
                action = "repository-check"
            else:
                action = "update" if package else "rollback"
            manager.job = {"status": "running", "message": "Пакет получен, проверяем…"}
            threading.Thread(target=manager.perform, args=(action, package, revision), daemon=True).start()
        except Exception as error:
            if package:
                package.unlink(missing_ok=True)
            manager.lock.release()
            self.respond(400, {"error": redact(str(error))})
            return
        self.respond(202, {"message": "Операция началась"})

    def log_message(self, fmt, *args):
        pass


def credentials(data):
    path = data / "credentials.json"
    if not path.exists():
        password = secrets.token_urlsafe(18)
        salt = secrets.token_hex(16)
        atomic_json(path, {"salt": salt, "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200000).hex()})
        access = data / "initial-password.txt"
        descriptor = os.open(str(access), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as file:
            file.write(password + "\n")
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--install", type=Path)
    parser.add_argument("--init-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    manager = Manager(args.data)
    # One supervisor per state directory, including first-install operations.
    import fcntl
    lock = (manager.data / "supervisor.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    auth = credentials(manager.data)
    if args.install:
        try:
            manager.install(args.install.resolve())
        except BaseException:
            manager.stop()
            raise
    if args.init_only:
        manager.stop()
        return
    if not manager.process:
        manager.boot()
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except BaseException:
        manager.stop()
        raise
    server.manager = manager
    server.credentials = auth
    manager.log.info("Сервис обслуживания %s запущен", VERSION)
    print("Media Str: http://%s:%s | login: admin | initial password: %s" % (args.host, args.port, manager.data / "initial-password.txt"), flush=True)

    def shutdown(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with manager.lock:
            manager.stop()


if __name__ == "__main__":
    main()
