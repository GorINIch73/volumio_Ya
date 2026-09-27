#!/bin/sh
set -eu
printf '%s\n' 'Установка теперь выполняется как штатный плагин Volumio.' 'Соберите ZIP: npm ci --ignore-scripts && python3 scripts/build.py' 'Распакуйте ZIP на Volumio и выполните volumio plugin install из каталога с package.json.' 'Подробности: README.md. Старый установщик: scripts/install-standalone.sh.'
exit 1
