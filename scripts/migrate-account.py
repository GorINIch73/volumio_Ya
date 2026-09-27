#!/usr/bin/env python3
"""Copy a legacy account without changing or stopping the old installation."""
import argparse
import json
import os
from pathlib import Path
import pwd
import re


def migrate(source, target, owner=None):
    value = json.loads(source.read_text())
    if not isinstance(value.get("token"), str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,8192}", value["token"]):
        raise ValueError("Некорректный сохранённый токен")
    if not isinstance(value.get("account"), dict) or not value["account"].get("uid"):
        raise ValueError("В старой установке нет проверенного аккаунта")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation: never replace an account already configured in Volumio.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as output:
            if owner:
                os.fchown(output.fileno(), *owner)
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if owner:
        os.chown(target.parent, *owner)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Запустите: sudo python3 scripts/migrate-account.py")
    user = pwd.getpwnam("volumio")
    try:
        migrate(Path("/var/lib/media-str/app-data/yandex-account.json"),
                Path("/data/configuration/music_service/media_str/yandex-account.json"),
                (user.pw_uid, user.pw_gid))
    except (OSError, ValueError, TypeError, AttributeError):
        raise SystemExit("Аккаунт не перенесён: проверьте наличие старого файла и отсутствие аккаунта в новом плагине.") from None
    print("Аккаунт перенесён. Проверьте его в настройках плагина. Старая установка сохранена.")
