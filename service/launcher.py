#!/usr/bin/env python3
"""Fixed, unprivileged recovery launcher for complete Media Str releases.

Installed once in /opt alongside a frozen recovery copy of maintenance.py.
All normal service/application code runs from a versioned data directory.
"""
import argparse
import fcntl
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from maintenance import MAX_EXPANDED, SafeFormatter, atomic_json, credentials, sync_directory, unpack


class Launcher:
    def __init__(self, data, host="127.0.0.1", port=8099, timeout=20):
        self.data = Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.releases = self.data / "releases"
        self.releases.mkdir(exist_ok=True)
        self.state_path = self.data / "runtime.json"
        self.job_path = self.data / "runtime-job.json"
        self.host, self.port, self.timeout = host, port, timeout
        self.process = None
        self.nonce = None
        self.active = None
        self.recovery = False
        self.closed = False
        self.log = logging.getLogger("launcher-" + str(self.data))
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        handler = RotatingFileHandler(self.data / "launcher.log", maxBytes=512 * 1024, backupCount=2)
        handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(message)s"))
        self.log.addHandler(handler)

    def read_state(self):
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"current": None, "previous": None, "pending": None, "trial": None}

    def job(self, status, message):
        atomic_json(self.job_path, {"status": status, "message": message})

    def release_path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9.-]+", name) or name in (".", ".."):
            raise ValueError("Некорректная версия для запуска")
        return self.releases / name

    def stage(self, package):
        if shutil.disk_usage(self.data).free < MAX_EXPANDED * 3:
            raise ValueError("Недостаточно свободного места: требуется 96 МБ")
        with tempfile.TemporaryDirectory(prefix="stage-", dir=self.data) as directory:
            stage = Path(directory)
            manifest = unpack(Path(package), stage)
            if manifest["protocol"] != 2:
                raise ValueError("Требуется полный пакет приложения и сервиса")
            name = manifest["version"] + "-" + secrets.token_hex(6)
            os.replace(stage, self.release_path(name))
            sync_directory(self.releases)
            return name

    def stop(self):
        process, self.process = self.process, None
        if not process:
            return
        # Service and its application share one process group. Even an abruptly
        # crashed service must not leave an orphan application holding sockets.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)

    def healthy(self):
        if not self.process or self.process.poll() is not None:
            return False
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
        try:
            connection.request("GET", "/runtime-health", headers={"X-MediaStr-Runtime": self.nonce})
            response = connection.getresponse()
            body = json.loads(response.read(4096))
            return response.status == 200 and body.get("nonce") == self.nonce and body.get("healthy") is True
        except (OSError, ValueError, AttributeError, http.client.HTTPException):
            return False
        finally:
            connection.close()

    def start(self, release=None):
        self.stop()
        self.active, self.recovery = release, release is None
        script = (self.release_path(release) / "service/maintenance.py" if release else
                  Path(__file__).resolve().with_name("maintenance.py"))
        self.nonce = secrets.token_hex(24)
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
                       "MEDIA_STR_MANAGED": "1", "MEDIA_STR_RUNTIME_NONCE": self.nonce,
                       "MEDIA_STR_RUNTIME_RELEASE": release or "", "MEDIA_STR_RECOVERY": "1" if self.recovery else "0"}
        self.process = subprocess.Popen([sys.executable, str(script), "--data", str(self.data),
                                         "--host", self.host, "--port", str(self.port)],
                                        env=environment, start_new_session=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        process = self.process

        def capture():
            with process.stdout:
                for line in iter(lambda: process.stdout.readline(4096), b""):
                    self.log.info("service: %s", line.decode("utf-8", errors="replace").rstrip())
        threading.Thread(target=capture, daemon=True).start()
        deadline = time.monotonic() + self.timeout
        healthy_since = None
        while time.monotonic() < deadline:
            if self.closed:
                raise InterruptedError("Загрузчик остановлен")
            if self.healthy():
                healthy_since = healthy_since or time.monotonic()
                if time.monotonic() - healthy_since >= 1:
                    return
            else:
                healthy_since = None
            if process.poll() is not None:
                break
            time.sleep(0.1)
        self.stop()
        raise RuntimeError("Новый сервис или приложение не прошли проверку запуска")

    def switch(self, candidate):
        state = self.read_state()
        old = state.get("current")
        self.release_path(candidate)
        state.update(pending=None, trial=candidate)
        self.job("running", "Проверяем запуск приложения и сервиса обслуживания…")
        atomic_json(self.state_path, state)
        try:
            self.start(candidate)
        except Exception:
            self.log.exception("Не удалось запустить полный выпуск")
            if self.closed:
                # Leave trial recorded: next boot must restore the last known good release.
                raise
            state.update(pending=None, trial=None)
            atomic_json(self.state_path, state)
            self.job("error", "Обновление не запустилось. Восстанавливаем предыдущую версию…")
            self.restore(state)
            self.job("error", "Обновление не запустилось; восстановлена предыдущая версия" if not self.recovery
                     else "Обновление не запустилось; доступна резервная страница обслуживания")
            return False
        state.update(current=candidate, previous=old, pending=None, trial=None)
        atomic_json(self.state_path, state)
        self.job("success", "Приложение и сервис обслуживания обновлены и проверены")
        try:
            self.cleanup(state)
        except (OSError, ValueError, TypeError):
            self.log.warning("Не удалось очистить старые выпуски; новая версия работает")
        return True

    def restore(self, state):
        for release in dict.fromkeys((state.get("current"), state.get("previous"))):
            if not release:
                continue
            try:
                self.start(release)
                if release != state.get("current"):
                    state.update(current=release, previous=state.get("current"), pending=None, trial=None)
                    atomic_json(self.state_path, state)
                return
            except Exception:
                if self.closed:
                    raise
                self.log.exception("Не удалось восстановить выпуск %s", release)
        self.start(None)

    def boot(self):
        state = self.read_state()
        interrupted = bool(state.get("trial"))
        if interrupted:
            state.update(trial=None, pending=None)
            atomic_json(self.state_path, state)
            self.job("error", "Обновление было прервано. Восстанавливаем проверенную версию")
        before = state.get("current")
        self.restore(state)
        if self.recovery:
            self.job("error", "Открыта резервная страница. Установите полный пакет из Обслуживания")
        elif interrupted or self.active != before:
            self.job("error", "Восстановлена последняя рабочая версия приложения и сервиса")
        elif not state.get("pending") and self.job_path.exists():
            job = json.loads(self.job_path.read_text())
            if job.get("status") == "running":
                self.job("error", "Операция была прервана. Рабочая версия сохранена; повторите обновление")

    def cleanup(self, state):
        keep = {value for value in state.values() if isinstance(value, str)}
        legacy = self.data / "state.json"
        if legacy.exists():
            keep.update(json.loads(legacy.read_text()).values())
        for directory in self.releases.iterdir():
            if directory.is_dir() and directory.name not in keep:
                try:
                    shutil.rmtree(directory)
                except OSError:
                    self.log.warning("Не удалось удалить старый выпуск %s", directory.name)

    def run(self):
        failures = 0
        while not self.closed:
            state = self.read_state()
            if state.get("pending"):
                self.switch(state["pending"])
                failures = 0
            elif self.healthy():
                failures = 0
            else:
                failures += 1
                if failures >= 3:
                    self.job("error", "Потеряна связь с сервисом. Восстанавливаем рабочую версию…")
                    self.boot()
                    failures = 0
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--install", type=Path)
    parser.add_argument("--init-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    launcher = Launcher(args.data, args.host, args.port)
    lock = (launcher.data / "launcher.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    credentials(launcher.data)

    def shutdown(signum, frame):
        launcher.closed = True
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        if args.install:
            # Verify both the service and app on an unused loopback port before
            # the systemd unit is changed or its public listener is started.
            if args.init_only:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    launcher.port = sock.getsockname()[1]
                launcher.host = "127.0.0.1"
            if not launcher.switch(launcher.stage(args.install.resolve())):
                raise RuntimeError("Установка полного выпуска не прошла проверку")
        else:
            launcher.boot()
        if not args.init_only:
            launcher.run()
    finally:
        launcher.stop()


if __name__ == "__main__":
    main()
