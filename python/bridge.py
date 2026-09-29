#!/usr/bin/env python3
"""Private JSON-lines worker owned by the Volumio plugin; no HTTP listener."""
import argparse
import json
import os
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from app import AccountError, Music, YandexAccount, music_id


class Backend:
    def __init__(self, data):
        self.account = YandexAccount(data)
        self.music = Music(self.account)

    def dispatch(self, method, params):
        if method == "ping":
            return {"ready": True}
        if method == "status":
            return self.account.status()
        if method == "login":
            return self.account.login(params.get("token"))
        if method == "check":
            return self.account.check()
        if method == "logout":
            return self.account.logout()
        if method == "catalog":
            return self.music.catalog()
        if method == "library":
            return self.music.library({key: [str(value)] for key, value in params.items()
                                       if key in ("kind", "offset", "owner", "album")})
        if method in ("track", "stream"):
            token, _ = self.music.session()
            identifier = music_id(params.get("id"))
            if method == "stream":
                return {"uri": self.music.stream(token, identifier)}
            values = self.music.yandex(token, "/tracks", {"track-ids": identifier})
            if not values or values[0].get("available") is False:
                raise AccountError("Трек недоступен")
            return self.music.track(values[0])
        raise AccountError("Неизвестная команда плагина")


def serve(backend, source, destination):
    for line in source:
        request_id = None
        try:
            if len(line) > 16384:
                raise ValueError()
            request = json.loads(line)
            request_id = request["id"]
            result = backend.dispatch(request["method"], request.get("params", {}))
            response = {"id": request_id, "result": result}
        except AccountError as error:
            response = {"id": request_id, "error": str(error)}
        except Exception as error:
            # Never serialize tracebacks, tokens or remote responses.
            frame = traceback.extract_tb(error.__traceback__)[-1]
            response = {"id": request_id, "error": "Не удалось выполнить запрос Яндекс Музыки",
                        "diagnostic": type(error).__name__ + " at " + Path(frame.filename).name + ":" + str(frame.lineno)}
        destination.write(json.dumps(response, ensure_ascii=False) + "\n")
        destination.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    serve(Backend(args.data), sys.stdin, sys.stdout)
