"""Yandex Music account, library and stream client for the Volumio plugin."""
import json
import os
import hashlib
import hmac
import base64
import http.client
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlencode, parse_qs


class AccountError(Exception):
    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


FILE_INFO_CLIENT_ID = "YandexMusicDesktopAppWindows/5.25.1"


class BufferedResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {str(key).lower(): str(value) for key, value in headers.items()}
        self.body = body

    def read(self, _limit=None):
        return self.body

    def getheader(self, name):
        return self.headers.get(name.lower())


def node_file_info(host, path, token, client_id):
    helper = Path(__file__).resolve().parents[1] / "lib" / "yandex-file-info.js"
    request = json.dumps({"host": host, "path": path, "token": token, "clientId": client_id})
    try:
        completed = subprocess.run(["node", str(helper)], input=request, text=True,
                                   capture_output=True, timeout=12, check=False)
    except subprocess.TimeoutExpired:
        raise TimeoutError() from None
    if completed.returncode != 0:
        raise OSError("Node/Axios request failed") from None
    try:
        value = json.loads(completed.stdout)
        status = int(value["status"])
        headers = value.get("headers") or {}
        body = str(value.get("body") or "").encode("utf-8")
        if len(body) > 8 * 1024 * 1024:
            raise ValueError()
        return BufferedResponse(status, headers, body)
    except (ValueError, TypeError, KeyError):
        raise OSError("Invalid Node/Axios response") from None


def api_error_summary(raw):
    """Extract only short, non-secret error fields from a Yandex response."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(value, dict):
        return ""
    result = value.get("result") if isinstance(value.get("result"), dict) else {}
    invocation = value.get("invocationInfo") if isinstance(value.get("invocationInfo"), dict) else {}
    error = value.get("error") or value.get("Error")
    if isinstance(error, dict):
        parts = [error.get(key) for key in ("name", "code", "status", "message")]
    else:
        # get-file-info errors are commonly wrapped as
        # result.name/result.message instead of top-level error/message.
        parts = [result.get("name"), result.get("message"), error, value.get("message")]
    request_id = result.get("requestId") or result.get("request-id") or invocation.get("req-id")
    if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9-]{8,100}", request_id):
        parts.append("requestId=" + request_id)
    safe = []
    for part in parts:
        if not isinstance(part, (str, int)):
            continue
        text = re.sub(r"[\r\n\t]+", " ", str(part)).strip()
        text = re.sub(r"(?i)(oauth\s+)[^\s,;]+", r"\1[redacted]", text)
        text = re.sub(r"https?://\S+", "[url]", text)
        if text and text not in safe:
            safe.append(text[:120])
    return "; ".join(safe[:3])


def has_plus_subscription(result, account):
    # Yandex has returned this flag under different wrappers over time
    # (result.plus, result.subscription, or account.plus). Search the
    # account-status payload recursively, but only interpret explicit flags.
    pending = [result, account]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key in ("hasPlus", "has_plus"):
                if key in value:
                    active = value[key]
                    if isinstance(active, str):
                        return active.strip().lower() in ("true", "1", "yes")
                    return active is True or active == 1
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return None


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
        result = body.get("result", {})
        account = result.get("account", {})
        uid = account.get("uid")
        if not uid or account.get("serviceAvailable") is False:
            raise AccountError("Токен не даёт доступа к аккаунту Яндекс Музыки")
        return {"uid": str(uid), "login": str(account.get("login") or ""),
                "display_name": str(account.get("displayName") or account.get("fullName") or account.get("login") or uid),
                "plus_active": has_plus_subscription(result, account)}
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
        previous = self.read()
        old_account = previous.get("account") or {}
        # Keep the last confirmed subscription state if this response omits
        # the Plus block. Do not carry it over when switching accounts.
        if (account.get("plus_active") is None and
                account.get("uid") == old_account.get("uid") and
                isinstance(old_account.get("plus_active"), bool)):
            account["plus_active"] = old_account["plus_active"]
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


def json_request(host, path, *, token=None, payload=None, client_id="YandexMusicDesktopAppWindows/5.25.1"):
    endpoint = path.split("?", 1)[0]
    connection = None
    # Match the reference plugin's axios-based Yandex client request headers.
    headers = {"Accept": "application/json, text/plain, */*"}
    if token:
        headers.update({"Authorization": "OAuth " + token,
                        "X-Yandex-Music-Client": client_id,
                        "Accept-Language": "ru",
                        "User-Agent": "axios/0.27.2"})
    if payload is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        if endpoint == "/get-file-info" and token:
            response = node_file_info(host, path, token, client_id)
        else:
            connection = http.client.HTTPSConnection(host, timeout=10)
            body = urlencode(payload) if payload is not None else None
            connection.request("POST" if payload is not None else "GET", path, body=body, headers=headers)
            response = connection.getresponse()
        raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError()
        detail = api_error_summary(raw)
        diagnostic = "HTTP " + str(response.status) + " " + endpoint
        if endpoint == "/get-file-info":
            diagnostic += "; transport=node-https; client=" + client_id
            # Keep the temporary signed target for request comparison. Never log credentials.
            diagnostic += "; target=" + path[:1000]
            for header in ("Content-Type", "Server", "X-Request-Id", "X-Yandex-Request-Id"):
                value_header = response.getheader(header)
                if value_header:
                    diagnostic += "; " + header.lower() + "=" + re.sub(r"[^a-zA-Z0-9._:/ -]", "", value_header)[:120]
        if detail:
            diagnostic += "; API: " + detail
        elif endpoint == "/get-file-info" and raw:
            # A 403 may come from a gateway and contain useful plain text/HTML
            # rather than the usual JSON error envelope. Keep only a short,
            # printable snippet; never include request URLs or authorization data.
            snippet = re.sub(r"<[^>]*>", " ", raw[:1024].decode("utf-8", "replace"))
            snippet = re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7e\u0400-\u04ff]", " ", snippet)).strip()
            if snippet:
                diagnostic += "; body=" + snippet[:180]
        if response.status in (401, 403):
            raise AccountError("Яндекс отклонил запрос. Проверьте токен и подписку", diagnostic)
        if response.status == 429:
            raise AccountError("Слишком много запросов к Яндексу. Попробуйте позже", diagnostic)
        if response.status != 200:
            raise AccountError("Яндекс не смог выполнить запрос", diagnostic)
        value = json.loads(raw)
        if isinstance(value, dict) and ("error" in value or "Error" in value or value.get("success") is False):
            raise AccountError("Яндекс отклонил запрос", diagnostic)
        return value
    except TimeoutError:
        raise AccountError("Яндекс Музыка не ответила за 10 секунд. Попробуйте позже") from None
    except (OSError, http.client.HTTPException):
        raise AccountError("Нет связи с Яндекс Музыкой") from None
    except (ValueError, TypeError):
        raise AccountError("Некорректный ответ музыкального сервиса") from None
    finally:
        if connection:
            connection.close()


class Music:
    """Yandex library and stream resolution; playback belongs to Volumio."""
    def __init__(self, account):
        self.account = account

    def quality(self):
        path = self.account.data / "audio-quality.json"
        try:
            value = json.loads(path.read_text()).get("quality")
        except (OSError, ValueError, AttributeError):
            value = None
        if value in ("standard", "high", "lossless"):
            return value
        try:
            account = self.account.status().get("account") or {}
            if isinstance(account.get("plus_active"), bool):
                value = "lossless" if account["plus_active"] else "high"
                self.set_quality(value)
                return value
        except AccountError:
            pass
        return "high"

    def set_quality(self, value):
        if value not in ("standard", "high", "lossless"):
            raise AccountError("Выберите обычное, максимальное или Lossless качество")
        path = self.account.data / "audio-quality.json"
        temp = path.with_suffix(".tmp")
        try:
            descriptor = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as file:
                json.dump({"quality": value}, file)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        except OSError:
            temp.unlink(missing_ok=True)
            raise AccountError("Не удалось сохранить качество звука") from None
        return {"quality": value}

    def session(self):
        with self.account.lock:
            value = self.account.read()
        if not value.get("token"):
            raise AccountError("Сначала войдите в аккаунт Яндекса")
        return value["token"], music_id(value["account"]["uid"])

    @staticmethod
    def yandex(token, path, payload=None, client_id="YandexMusicDesktopAppWindows/5.25.1"):
        value = json_request("api.music.yandex.net", path, token=token, payload=payload,
                             client_id=client_id)
        if not isinstance(value, dict) or "result" not in value:
            raise AccountError("Некорректный ответ Яндекс Музыки")
        return value["result"]

    @staticmethod
    def cover_url(value):
        albums = value.get("albums") or []
        cover = value.get("cover") or {}
        candidates = ([value.get("coverUri"), cover.get("uri")]
                      + (cover.get("itemsUri") or [])
                      + [album.get("coverUri") for album in albums])
        for cover in candidates:
            if not isinstance(cover, str) or not cover.strip():
                continue
            cover = cover.strip().replace("%%", "600x600")
            if cover.startswith("//"):
                cover = "https:" + cover
            elif "://" not in cover:
                cover = "https://" + cover
            try:
                parsed = urlsplit(cover)
                if (parsed.scheme in ("http", "https") and parsed.hostname
                        and not parsed.username and not parsed.password
                        and parsed.port in (None, 80, 443)
                        and not any(c.isspace() for c in cover)):
                    return parsed._replace(scheme="https").geturl()
            except ValueError:
                pass
        return ""

    @staticmethod
    def track(value):
        albums = value.get("albums") or []
        return {"id": music_id(value["id"]), "title": str(value.get("title") or "Без названия")[:500],
                "artist": ", ".join(str(a.get("name", "")) for a in value.get("artists", []))[:500],
                "album": str(albums[0].get("title", ""))[:500] if albums else "",
                "duration": max(0, int(value.get("durationMs") or 0) // 1000),
                "albumart": Music.cover_url(value),
                "available": value.get("available") is not False}

    @staticmethod
    def catalog_item(value, kind):
        if kind == "album":
            uri = "yam/album/" + music_id(value["id"])
        else:
            owner = value.get("uid") or (value.get("owner") or {}).get("uid")
            uri = "yam/playlist/" + music_id(owner) + "/" + music_id(value["kind"])
        return {"uri": uri, "title": str(value.get("title") or "Без названия")[:500],
                "albumart": Music.cover_url(value)}

    def catalog(self):
        token, _ = self.session()
        blocks = ("personalplaylists,promotions,new-releases,new-playlists,mixes,chart,"
                  "playlists,play_contexts")
        result = self.yandex(token, "/landing3?" + urlencode({"blocks": blocks}))
        definitions = [
            ("personal-playlists", "Подобрано для вас", "playlist"),
            ("promotions", "В центре внимания", "mixed"),
            ("new-releases", "Новые релизы", "album"),
            ("new-playlists", "Популярные плейлисты", "playlist"),
            ("mixes", "Миксы", "playlist"),
            ("chart", "Чарт Яндекс Музыки", "playlist"),
            ("playlists", "Плейлисты Яндекса", "playlist"),
            ("play-contexts", "Недавно слушали", "context"),
        ]
        aliases = {"personalplaylists": "personal-playlists", "play_contexts": "play-contexts"}
        grouped = {}
        for block in result.get("blocks") or []:
            block_type = aliases.get(block.get("type"), block.get("type"))
            grouped.setdefault(block_type, []).extend(block.get("entities") or [])
        sections = []
        for block_type, title, kind in definitions:
            items, seen = [], set()
            for entity in grouped.get(block_type, []):
                try:
                    value = entity.get("data") or {}
                    item_kind = kind
                    if block_type == "personal-playlists":
                        value = value.get("data", value)
                    if kind == "context":
                        item_kind = value.get("context")
                        if item_kind not in ("album", "playlist"):
                            continue
                        value = value.get("payload") or {}
                    elif kind == "mixed":
                        # Promo and editorial blocks can wrap playable entities in
                        # one or more `data` fields, or provide a typed payload.
                        item_kind = None
                        for _ in range(3):
                            if value.get("context") in ("album", "playlist"):
                                item_kind = value["context"]
                                value = value.get("payload") or {}
                                break
                            nested = value.get("data")
                            if not isinstance(nested, dict):
                                break
                            value = nested
                        if item_kind is None:
                            owner = value.get("uid") or (value.get("owner") or {}).get("uid")
                            if value.get("kind") is not None and owner is not None:
                                item_kind = "playlist"
                            elif value.get("id") is not None:
                                item_kind = "album"
                            else:
                                continue
                    elif kind == "playlist":
                        # Ignore chart/editorial cards that are not directly
                        # playable playlists (for example artist or promo links).
                        owner = value.get("uid") or (value.get("owner") or {}).get("uid")
                        if value.get("kind") is None or owner is None:
                            continue
                    item = self.catalog_item(value, item_kind)
                except (AccountError, KeyError, TypeError, AttributeError, ValueError):
                    # Unsupported promotional entities must not hide the library.
                    continue
                if item["uri"] not in seen:
                    items.append(item)
                    seen.add(item["uri"])
            if items:
                sections.append({"title": title, "items": items})
        return {"sections": sections}

    def library(self, query):
        token, uid = self.session()
        kind = query.get("kind", [""])[0]
        album = query.get("album", [""])[0]
        if not kind and not album:
            values = self.yandex(token, f"/users/{uid}/playlists/list")
            return {"playlists": [{"kind": music_id(p["kind"]), "title": str(p.get("title", "Без названия"))[:500],
                                   "count": p.get("trackCount", 0), "albumart": self.cover_url(p)} for p in values]}
        offset = int(query.get("offset", ["0"])[0])
        if not 0 <= offset <= 100000:
            raise AccountError("Некорректная страница")
        if album:
            collection = self.yandex(token, f"/albums/{music_id(album)}/with-tracks")
            entries = [track for volume in collection.get("volumes") or [] for track in volume]
            tracks = []
            for value in entries[offset:offset + 50]:
                value = dict(value)
                if not value.get("albums"):
                    value["albums"] = [collection]
                tracks.append(self.track(value))
            return {"title": str(collection.get("title") or "Альбом")[:500], "tracks": tracks,
                    "total": len(entries), "next_offset": offset + 50 if offset + 50 < len(entries) else None}
        if kind == "likes":
            collection = self.yandex(token, f"/users/{uid}/likes/tracks")["library"]
            title = "Мне нравится"
        else:
            owner = music_id(query.get("owner", [uid])[0])
            collection = self.yandex(token, f"/users/{owner}/playlists/{music_id(kind)}")
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

    def lossless_stream(self, token, identifier):
        identifier = identifier.split(":", 1)[0]
        timestamp = int(time.time() * 1000)
        codecs = "flac,mp3,flac-mp4"
        message = f"{timestamp}{identifier}lossless{codecs}raw"
        secret = b"kzqU4XhfCaY6B6JTHODeq5"
        signature = base64.b64encode(hmac.new(secret, message.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        query = urlencode({"ts": timestamp, "trackId": identifier, "quality": "lossless",
                           "codecs": codecs, "transports": "raw", "sign": signature}, safe=",")
        result = self.yandex(token, "/get-file-info?" + query, client_id=FILE_INFO_CLIENT_ID)
        info = result.get("downloadInfo") if isinstance(result, dict) else None
        if not isinstance(info, dict):
            return None
        codec = str(info.get("codec") or "unknown")
        transport = str(info.get("transport") or "unknown")
        if codec != "flac" or transport not in ("raw", "encraw"):
            return {"codec": codec, "transport": transport, "unavailable": True}
        key = info.get("key") if transport == "encraw" else None
        if transport == "encraw" and (not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", key)):
            return {"codec": codec, "transport": transport, "unavailable": True}
        url = info.get("url")
        parsed = urlsplit(url or "")
        if (parsed.scheme != "https" or not self.media_host(parsed.hostname)
                or parsed.port not in (None, 443) or parsed.username or parsed.password):
            raise AccountError("Некорректный адрес FLAC-потока")
        return {"uri": url, "codec": "flac", "transport": transport, "key": key}

    def stream(self, token, identifier, quality="high"):
        lossless_result = ""
        lossless_debug = ""
        if quality == "lossless":
            try:
                result = self.lossless_stream(token, identifier)
                if result and not result.get("unavailable"):
                    return result
                if result:
                    lossless_result = "Lossless API: " + result["codec"] + "/" + result["transport"]
                else:
                    lossless_result = "Lossless API не вернул данные потока"
            except AccountError as error:
                # Keep playback available when lossless is absent for this track
                # or the account/API does not return an unencrypted raw stream.
                lossless_result = "Ошибка Lossless API: " + str(error)
                if error.diagnostic:
                    lossless_debug = error.diagnostic
                    lossless_result += (" (" + error.diagnostic +
                                        "; quality=lossless; codecs=flac,mp3,flac-mp4; transports=raw)")
        options = self.yandex(token, f"/tracks/{identifier}/download-info")
        options = [o for o in options if o.get("codec") == "mp3" and not o.get("preview")]
        if not options:
            raise AccountError("Полная версия трека недоступна. Проверьте подписку")
        if quality == "standard":
            standard = [o for o in options if int(o.get("bitrateInKbps", 0)) <= 192]
            option = (max(standard, key=lambda o: int(o.get("bitrateInKbps", 0))) if standard else
                      min(options, key=lambda o: int(o.get("bitrateInKbps", 0))))
        else:
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
        return {"uri": f"https://{host}/get-mp3/{signature}/{stamp}{path}",
                "codec": "mp3", "bitrate_kbps": int(option.get("bitrateInKbps", 0)),
                "transport": "raw", "lossless_result": lossless_result,
                "lossless_debug": lossless_debug}
