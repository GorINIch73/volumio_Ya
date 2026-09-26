#!/usr/bin/env python3
"""Optional local helper; never used by the maintenance service."""
import os
from pathlib import Path
import sys


def main():
    try:
        from yandex_music import Client
    except ImportError:
        sys.exit("Установите yandex-music в отдельное окружение по инструкции README.md")
    client = Client()
    if not hasattr(client, "device_auth"):
        sys.exit("В этой версии yandex-music нет device_auth. Обновите библиотеку; альтернативы указаны в README.md")

    def on_code(code):
        print("Откройте в браузере:", code.verification_url)
        print("Код для подтверждения:", code.user_code)
        print("Подтвердите вход в свой аккаунт Яндекса.", flush=True)

    # This helper intentionally performs no music/playlist modifications.
    token = client.device_auth(on_code=on_code)
    folder = Path.home() / ".config" / "media-str"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / "yandex-access-token.txt"
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        sys.exit("Файл токена уже существует: %s. Сохраните или переименуйте его перед повторным получением." % path)
    with os.fdopen(descriptor, "w") as file:
        file.write(token.access_token + "\n")
    print("Токен сохранён в:", path)
    print("Откройте этот файл локально и скопируйте токен в настройки музыкального плагина.")
    print("Срок действия (секунд):", token.expires_in)
    print("В текущем выпуске автоматического обновления токена нет.")


if __name__ == "__main__":
    main()
