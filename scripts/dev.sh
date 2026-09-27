#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"
python3 scripts/build.py --output dist/media-str-dev.zip
exec python3 service/launcher.py --data .dev --install dist/media-str-dev.zip --host 127.0.0.1 --port 8099
