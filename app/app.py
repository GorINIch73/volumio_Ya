#!/usr/bin/env python3
"""Replaceable UI process. Only the maintenance service exposes it to the LAN."""
import json
import os
import hmac
import hashlib
import http.client
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlencode, parse_qs

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


def music_id(value):
    value = str(value)
    if not re.fullmatch(r"[0-9]{1,24}(?::[0-9]{1,24})?", value):
        raise AccountError("Некорректный идентификатор музыки")
    return value


def json_request(host, path, *, token=None, payload=None, local=False):
    connection = (http.client.HTTPConnection if local else http.client.HTTPSConnection)(host, timeout=10)
    headers = {"Accept": "application/json"}
    if token:
        headers.update({"Authorization": "OAuth " + token,
                        "X-Yandex-Music-Client": "YandexMusicDesktopAppWindows/5.25.1"})
    if payload is not None:
        headers["Content-Type"] = "application/json" if local else "application/x-www-form-urlencoded"
    try:
        body = (json.dumps(payload) if local else urlencode(payload)) if payload is not None else None
        connection.request("POST" if payload is not None else "GET", path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status in (401, 403) and not local:
            raise AccountError("Яндекс отклонил запрос. Проверьте токен и подписку")
        if response.status == 429:
            raise AccountError("Слишком много запросов к Яндексу. Попробуйте позже")
        if response.status != 200:
            raise AccountError("Volumio недоступен" if local else "Яндекс не смог выполнить запрос")
        raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError()
        value = json.loads(raw)
        if isinstance(value, dict) and ("error" in value or "Error" in value or value.get("success") is False):
            raise AccountError("Volumio отклонил команду" if local else "Яндекс отклонил запрос")
        return value
    except (OSError, http.client.HTTPException):
        raise AccountError("Нет связи с Volumio" if local else "Нет связи с Яндекс Музыкой") from None
    except (ValueError, TypeError):
        raise AccountError("Некорректный ответ музыкального сервиса") from None
    finally:
        connection.close()


class Music:
    """Account library and playback through the local Volumio REST API."""
    def __init__(self, account):
        self.account = account
        self.playback_lock = threading.Lock()

    def session(self):
        with self.account.lock:
            value = self.account.read()
        if not value.get("token"):
            raise AccountError("Сначала войдите в аккаунт Яндекса")
        return value["token"], music_id(value["account"]["uid"])

    @staticmethod
    def yandex(token, path, payload=None):
        value = json_request("api.music.yandex.net", path, token=token, payload=payload)
        if not isinstance(value, dict) or "result" not in value:
            raise AccountError("Некорректный ответ Яндекс Музыки")
        return value["result"]

    @staticmethod
    def track(value):
        albums = value.get("albums") or []
        return {"id": music_id(value["id"]), "title": str(value.get("title") or "Без названия")[:500],
                "artist": ", ".join(str(a.get("name", "")) for a in value.get("artists", []))[:500],
                "album": str(albums[0].get("title", ""))[:500] if albums else "",
                "duration": max(0, int(value.get("durationMs") or 0) // 1000),
                "available": value.get("available") is not False}

    def library(self, query):
        token, uid = self.session()
        kind = query.get("kind", [""])[0]
        if not kind:
            values = self.yandex(token, f"/users/{uid}/playlists/list")
            return {"playlists": [{"kind": music_id(p["kind"]), "title": str(p.get("title", "Без названия"))[:500],
                                   "count": p.get("trackCount", 0)} for p in values]}
        offset = int(query.get("offset", ["0"])[0])
        if not 0 <= offset <= 100000:
            raise AccountError("Некорректная страница")
        if kind == "likes":
            collection = self.yandex(token, f"/users/{uid}/likes/tracks")["library"]
            title = "Мне нравится"
        else:
            collection = self.yandex(token, f"/users/{uid}/playlists/{music_id(kind)}")
            title = str(collection.get("title", "Плейлист"))[:500]
        entries = collection.get("tracks") or []
        page = entries[offset:offset + 50]
        ids = [music_id(t.get("id") or t.get("track", {}).get("id")) for t in page]
        details = self.yandex(token, "/tracks", {"track-ids": ",".join(ids)}) if ids else []
        by_id = {str(t["id"]): t for t in details}
        tracks = []
        for identifier in ids:
            detail = by_id.get(identifier) or by_id.get(identifier.split(":")[0])
            tracks.append(self.track(detail) if detail else {"id": identifier, "title": "Трек недоступен",
                          "artist": "", "album": "", "duration": 0, "available": False})
        return {"title": title, "tracks": tracks, "total": len(entries),
                "next_offset": offset + 50 if offset + 50 < len(entries) else None}

    @staticmethod
    def media_host(host):
        return bool(host and re.fullmatch(r"[a-zA-Z0-9.-]+", host) and
                    any(host.endswith("." + domain) for domain in ("yandex.net", "yandex.ru")))

    def stream(self, token, identifier):
        options = self.yandex(token, f"/tracks/{identifier}/download-info")
        options = [o for o in options if o.get("codec") == "mp3" and not o.get("preview")]
        if not options:
            raise AccountError("Полная версия трека недоступна. Проверьте подписку")
        option = max(options, key=lambda o: int(o.get("bitrateInKbps", 0)))
        parsed = urlsplit(option["downloadInfoUrl"])
        if parsed.scheme not in ("http", "https") or not self.media_host(parsed.hostname) or parsed.port not in (None, 443) or parsed.username:
            raise AccountError("Некорректный адрес аудиофайла")
        query = parse_qs(parsed.query)
        query["format"] = ["json"]
        # Download metadata is signed; never send the account token to a CDN host.
        info = json_request(parsed.hostname, parsed.path + "?" + urlencode(query, doseq=True))
        host, path, stamp, salt = (info[k] for k in ("host", "path", "ts", "s"))
        if not self.media_host(host) or not re.fullmatch(r"/[A-Za-z0-9/_.%-]+", path) or not re.fullmatch(r"[0-9a-fA-F]+", str(stamp)):
            raise AccountError("Некорректный адрес аудиофайла")
        signature = hashlib.md5(("XGRlBW9FXlekgbPrRHuSiA" + path[1:] + salt).encode()).hexdigest()
        return f"https://{host}/get-mp3/{signature}/{stamp}{path}"

    @staticmethod
    def volumio(path, payload=None):
        return json_request("127.0.0.1", "/api/v1/" + path, payload=payload, local=True)

    def state(self):
        value = self.volumio("getState")
        # Do not expose signed media URLs, raw queue entries or plugin internals.
        return {key: value.get(key) for key in ("status", "title", "artist", "album", "seek", "duration", "volume")}

    def action(self, payload):
        action = payload.get("action")
        if action not in ("play", "enqueue", "toggle", "pause", "stop", "next", "prev"):
            raise AccountError("Неизвестная команда плеера")
        if not self.playback_lock.acquire(blocking=False):
            raise AccountError("Дождитесь выполнения команды плеера")
        try:
            if action in ("play", "enqueue"):
                token, _ = self.session()
                identifier = music_id(payload.get("id"))
                values = self.yandex(token, "/tracks", {"track-ids": identifier})
                if not values or values[0].get("available") is False:
                    raise AccountError("Трек недоступен")
                track = self.track(values[0])
                uri = self.stream(token, identifier)
                item = {"uri": uri, "service": "mpd", "type": "song", "name": track["title"],
                        "title": track["title"], "artist": track["artist"], "album": track["album"],
                        "duration": track["duration"], "trackType": "mp3"}
                self.volumio("replaceAndPlay" if action == "play" else "addToQueue", item)
            else:
                self.volumio("commands?" + urlencode({"cmd": action}))
            return {"message": "Трек добавлен в очередь" if action == "enqueue" else "Команда отправлена в Volumio"}
        finally:
            self.playback_lock.release()


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
        if path in ("/api/yandex/status", "/api/music/library", "/api/music/state"):
            if not self.bridge_authorized():
                self.account_response(403, {"error": "Нет доступа"})
                return
            try:
                if path == "/api/music/library":
                    value = self.server.music.library(parse_qs(urlsplit(self.path).query))
                elif path == "/api/music/state":
                    value = self.server.music.state()
                else:
                    value = self.server.account.status()
                self.account_response(200, value)
            except AccountError as error:
                self.account_response(400, {"error": str(error)})
            except (ValueError, TypeError, KeyError, AttributeError, IndexError):
                self.account_response(502, {"error": "Некорректный ответ музыкального сервиса"})
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
            if path == "/api/music/action":
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 16384:
                    raise AccountError("Некорректный запрос плеера")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise AccountError("Некорректный запрос плеера")
                value = self.server.music.action(payload)
            elif path == "/api/yandex/login":
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
        except (ValueError, OSError, TypeError, KeyError, AttributeError, IndexError):
            self.account_response(400, {"error": "Не удалось обработать запрос"})

    def log_message(self, fmt, *args):
        # Avoid URLs, query strings and authorization data in logs.
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ["MEDIA_STR_APP_PORT"])), Handler)
    server.account = YandexAccount(os.environ["MEDIA_STR_APP_DATA"])
    server.music = Music(server.account)
    print("Interface process started", flush=True)
    server.serve_forever()
