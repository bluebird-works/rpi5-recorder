#!/usr/bin/env bash
set -euo pipefail

REC_USER="${REC_USER:-${SUDO_USER:-pi}}"
REC_HOME="$(getent passwd "${REC_USER}" | cut -d: -f6)"
APP_DIR="${REC_HOME}/rpi5-web"
SERVICE_NAME="rpi5-web-recorder.service"

AP_SSID="${AP_SSID:-RPiRecorder}"
AP_PASSWORD="${AP_PASSWORD:-12345678}"
AP_IFACE="${AP_IFACE:-wlan0}"
AP_CON_NAME="rpi-web-ap"

apt-get update
# dnsmasq-base: без нього ipv4.method=shared не роздає DHCP клієнтам AP.
# Це лише Recommends пакета network-manager, не Depends — на мінімальному
# образі з Install-Recommends=false міг би тихо не встановитись, тому тут
# явно.
apt-get install -y ffmpeg rpicam-apps python3-flask network-manager dnsmasq-base

# Trixie підняв AP через NetworkManager нативно — hostapd/dnsmasq тут
# свідомо не піднімаємо (див. спек). Якщо NM не активний — falшивий шлях
# (наприклад голий wpa_supplicant/dhcpcd) не підтримуємо, падаємо чітко.
if ! systemctl is-active --quiet NetworkManager; then
  echo "NetworkManager не активний на цій машині — install_web.sh розрахований" >&2
  echo "на nmcli AP (Debian 13 trixie). hostapd/dnsmasq тут не піднімаємо." >&2
  exit 1
fi

# доступ до CSI-камери йде через групу video
usermod -aG video "${REC_USER}" || true

mkdir -p "${APP_DIR}"
cp "$(dirname "$0")/recording_engine.py" "${APP_DIR}/"
cp "$(dirname "$0")/web_recorder.py" "${APP_DIR}/"
chown -R "${REC_USER}:${REC_USER}" "${APP_DIR}"
mkdir -p "${REC_HOME}/recordings"
chown "${REC_USER}:${REC_USER}" "${REC_HOME}/recordings"

if ! nmcli -t -f NAME con show | grep -qx "${AP_CON_NAME}"; then
  nmcli con add type wifi ifname "${AP_IFACE}" con-name "${AP_CON_NAME}" \
    ssid "${AP_SSID}" \
    802-11-wireless.mode ap 802-11-wireless.band bg \
    802-11-wireless-security.key-mgmt wpa-psk \
    802-11-wireless-security.psk "${AP_PASSWORD}" \
    ipv4.method shared connection.autoconnect yes
else
  nmcli con modify "${AP_CON_NAME}" \
    802-11-wireless.ssid "${AP_SSID}" \
    802-11-wireless-security.psk "${AP_PASSWORD}"
fi

cat >"/etc/systemd/system/${SERVICE_NAME}" <<UNIT
[Unit]
Description=RPi5 web recorder (WiFi AP control)
After=NetworkManager.service
Requires=NetworkManager.service

[Service]
Type=simple
User=${REC_USER}
SupplementaryGroups=video
WorkingDirectory=${APP_DIR}
Environment=HOME=${REC_HOME}
Environment=WIDTH=1920 HEIGHT=1080 FPS=30 BITRATE=10000000
Environment=AUTOFOCUS_MODE=manual LENS_POSITION=0
Environment=SEGMENT_SEC=0
Environment=WEB_PORT=80
# слухати порт 80 без рута — той самий принцип найменших прав, що в BLE-юніті
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
ExecStartPre=+/usr/bin/nmcli con up ${AP_CON_NAME}
ExecStart=/usr/bin/python3 ${APP_DIR}/web_recorder.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

systemctl --no-pager status "${SERVICE_NAME}" || true
echo "web UI: http://10.42.0.1/ (AP ssid=${AP_SSID})"
echo "logs: journalctl -u ${SERVICE_NAME} -f"
