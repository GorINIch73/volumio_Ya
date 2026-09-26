#!/usr/bin/env python3
"""Replaceable UI process. Only the maintenance service exposes it to the LAN."""
import json
import os
import hmac
import http.client
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"


class AccountError(Exception):
    pass


def validate_yandex_token(token):
    """Use the same OAuth header/account endpoint as the achechulin plugin."""
    connection = http.client.HTTPSConnection("api.music.yandex.net", timeout=10)
    try:
        connection.request("GET", "/account/status", headers={
            "Authorization": "OAuth " + token,
            "X-Yandex-Music-Client": "YandexMusicDesktopAppWindows/5.25.1",
            "Accept-Language": "ru", "Accept": "application/json"})
        response = connection.getresponse()
        if response.status in (401, 403):
            raise AccountError("Яндекс отклонил токен. Проверьте его или получите новый")
        if response.status == 429:
            raise AccountError("Яндекс временно ограничил запросы. Попробуйте позже")
        if response.status != 200:
            raise AccountError("Не удалось проверить аккаунт: Яндекс временно недоступен")
        raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise AccountError("Неожиданный ответ Яндекса")
        body = json.loads(raw)
        account = body.get("result", {}).get("account", {})
        uid = account.get("uid")
        if not uid or account.get("serviceAvailable") is False:
            raise AccountError("Токен не даёт доступа к аккаунту Яндекс Музыки")
        return {"uid": str(uid), "login": str(account.get("login") or ""),
                "display_name": str(account.get("displayName") or account.get("fullName") or account.get("login") or uid)}
    except AccountError:
        raise
    except (OSError, http.client.HTTPException):
        # Never include request/headers or a remote response in exceptions/logs.
        raise AccountError("Не удалось связаться с Яндексом. Проверьте интернет и время на устройстве") from None
    except (ValueError, TypeError, AttributeError):
        raise AccountError("Неожиданный ответ Яндекса при проверке аккаунта") from None
    finally:
        connection.close()


class YandexAccount:
    def __init__(self, data, validator=validate_yandex_token):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.data / "yandex-account.json"
        self.lock = threading.Lock()
        self.validator = validator

    def read(self):
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text())
            if not isinstance(value, dict) or not isinstance(value.get("token"), str):
                raise ValueError()
            return value
        except (ValueError, OSError):
            raise AccountError("Не удалось прочитать настройки аккаунта. Войдите заново") from None

    @staticmethod
    def public(value):
        return {"configured": bool(value.get("token")), "account": value.get("account"),
                "checked_at": value.get("checked_at")}

    def status(self):
        with self.lock:
            return self.public(self.read())

    def save(self, token):
        account = self.validator(token)
        value = {"token": token, "account": account,
                 "checked_at": datetime.now(timezone.utc).isoformat()}
        temp = self.path.with_suffix(".tmp")
        try:
            descriptor = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as file:
                json.dump(value, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
            self.sync()
        except OSError:
            temp.unlink(missing_ok=True)
            raise AccountError("Не удалось сохранить аккаунт на устройстве") from None
        return self.public(value)

    def sync(self):
        descriptor = os.open(str(self.data), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def login(self, token):
        if not isinstance(token, str):
            raise AccountError("Введите токен Яндекс Музыки")
        token = token.strip()
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,8192}", token):
            raise AccountError("Введите токен без пробелов и префикса OAuth")
        with self.lock:
            return self.save(token)

    def check(self):
        with self.lock:
            token = self.read().get("token")
            if not token:
                raise AccountError("Сначала сохраните токен")
            return self.save(token)

    def logout(self):
        with self.lock:
            self.path.unlink(missing_ok=True)
            self.path.with_suffix(".tmp").unlink(missing_ok=True)
            self.sync()
            return self.public({})


class Handler(BaseHTTPRequestHandler):
    def account_response(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def bridge_authorized(self):
        supplied = self.headers.get("X-MediaStr-Bridge", "")
        expected = os.environ.get("MEDIA_STR_HEALTH_NONCE", "")
        return bool(expected) and hmac.compare_digest(supplied.encode(), expected.encode())

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path == "/api/yandex/status":
            if not self.bridge_authorized():
                self.account_response(403, {"error": "Нет доступа"})
                return
            try:
                self.account_response(200, self.server.account.status())
            except AccountError as error:
                self.account_response(400, {"error": str(error)})
            return
        elif path == "/health":
            data = json.dumps({"nonce": os.environ["MEDIA_STR_HEALTH_NONCE"]}).encode()
            kind = "application/json"
        else:
            target = (WEB / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
            if WEB not in target.parents or not target.is_file():
                self.send_error(404)
                return
            data = target.read_bytes()
            kind = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.close_connection = True
        if not self.bridge_authorized():
            self.account_response(403, {"error": "Нет доступа"})
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/yandex/login":
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 16384:
                    raise AccountError("Некорректный запрос входа")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise AccountError("Некорректный запрос входа")
                value = self.server.account.login(payload.get("token"))
                print("Yandex account token validated and saved", flush=True)
            elif path == "/api/yandex/logout":
                value = self.server.account.logout()
                print("Yandex account disconnected", flush=True)
            elif path == "/api/yandex/check":
                value = self.server.account.check()
                print("Yandex account token validated", flush=True)
            else:
                self.account_response(404, {"error": "Неизвестная операция"})
                return
            self.account_response(200, value)
        except AccountError as error:
            self.account_response(400, {"error": str(error)})
        except (ValueError, OSError):
            self.account_response(400, {"error": "Не удалось обработать запрос аккаунта"})

    def log_message(self, fmt, *args):
        # Avoid URLs, query strings and authorization data in logs.
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ["MEDIA_STR_APP_PORT"])), Handler)
    server.account = YandexAccount(os.environ["MEDIA_STR_APP_DATA"])
    print("Interface process started", flush=True)
    server.serve_forever()
