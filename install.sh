#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")"

echo "Checking YaM requirements (Volumio 4 / Python 3.9+)"
python3 -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"'
node -e 'if (Number(process.versions.node.split(".")[0]) < 14) process.exit(1)'
PYTHONDONTWRITEBYTECODE=1 python3 -c 'import sys; sys.path.insert(0, "app"); from app import Music, YandexAccount'
if [ ! -f node_modules/kew/kew.js ]; then
    npm ci --production --ignore-scripts --no-audit --no-fund
fi
node -e 'require("./index.js")'
sudo install -d -m 0700 -o volumio -g volumio /data/configuration/music_service/yam
echo "Enable YaM in Plugins, then open its settings to sign in."
echo "plugininstallend"
