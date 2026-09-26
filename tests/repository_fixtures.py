import io
import json
from pathlib import Path
import zipfile

SHA = "a" * 40


def revision():
    return {"sha": SHA, "commit": {"message": "Test update\nDetails"}}


def archive():
    buffer = io.BytesIO()
    source = Path(__file__).resolve().parents[1] / "app"
    with zipfile.ZipFile(buffer, "w") as output:
        for file in source.rglob("*"):
            if file.is_file() and "__pycache__" not in file.parts:
                output.writestr("volumio_Ya-" + SHA + "/app/" + file.relative_to(source).as_posix(), file.read_bytes())
    return buffer.getvalue()


def fake_fetch(host, path, limit):
    if host == "api.github.com":
        return json.dumps([revision()]).encode()
    if host == "codeload.github.com" and path.endswith(SHA):
        return archive()
    raise AssertionError("Unexpected download: " + host + path)
