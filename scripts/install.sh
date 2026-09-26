#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите: sudo bash scripts/install.sh" >&2
    exit 1
fi

PROJECT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
DATA_DIR=/var/lib/media-str
SERVICE_DIR=/opt/media-str

command -v python3 >/dev/null 2>&1 || { echo "Требуется Python 3.9 или новее" >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 9), "Требуется Python 3.9 или новее"'
command -v systemctl >/dev/null 2>&1 || { echo "Требуется systemd" >&2; exit 1; }
command -v runuser >/dev/null 2>&1 || { echo "Требуется runuser (util-linux)" >&2; exit 1; }

# No apt/npm operations and no change to Volumio's existing music plugins.
if ! id mediastr >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin mediastr
fi
install -d -m 0755 "$SERVICE_DIR"
install -d -m 0700 -o mediastr -g mediastr "$DATA_DIR"
PACKAGE_PATH=$(mktemp /tmp/media-str-install.XXXXXX.zip)
trap 'rm -f "$PACKAGE_PATH"' EXIT HUP INT TERM
python3 "$PROJECT_DIR/scripts/build.py" --output "$PACKAGE_PATH"
chown mediastr:mediastr "$PACKAGE_PATH"
chmod 0600 "$PACKAGE_PATH"

if systemctl is-active --quiet media-str.service; then
    systemctl stop media-str.service
fi
if [ -f "$SERVICE_DIR/maintenance.py" ]; then
    cp -p "$SERVICE_DIR/maintenance.py" "$SERVICE_DIR/maintenance.py.previous"
fi
install -m 0755 "$PROJECT_DIR/service/maintenance.py" "$SERVICE_DIR/maintenance.py"
install -m 0644 "$PROJECT_DIR/service/repository.py" "$SERVICE_DIR/repository.py"

if ! runuser -u mediastr -- python3 "$SERVICE_DIR/maintenance.py" --data "$DATA_DIR" --install "$PACKAGE_PATH" --init-only; then
    echo "Установка приложения не удалась; сохранённые настройки остаются на месте." >&2
    if [ -f "$SERVICE_DIR/maintenance.py.previous" ]; then
        cp -p "$SERVICE_DIR/maintenance.py.previous" "$SERVICE_DIR/maintenance.py"
        systemctl start media-str.service || true
    fi
    exit 1
fi

cat > /etc/systemd/system/media-str.service <<'UNIT'
[Unit]
Description=Media Str maintenance and web interface
After=network.target

[Service]
Type=simple
User=mediastr
Group=mediastr
WorkingDirectory=/var/lib/media-str
ExecStart=/usr/bin/python3 /opt/media-str/maintenance.py --data /var/lib/media-str --host 0.0.0.0 --port 8099
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/media-str

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now media-str.service
sleep 2
if ! systemctl is-active --quiet media-str.service; then
    echo "Сервис не запустился. Диагностика: sudo journalctl -u media-str -n 80" >&2
    exit 1
fi
echo ""
echo "Откройте http://IP-АДРЕС-VOLUMIO:8099"
echo "Логин: admin"
printf 'Пароль: '
cat "$DATA_DIR/initial-password.txt"
echo "Сохраните пароль. Его можно повторно прочитать: sudo cat /var/lib/media-str/initial-password.txt"
