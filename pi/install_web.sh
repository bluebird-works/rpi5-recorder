#!/usr/bin/env bash
set -euo pipefail

REC_USER="${REC_USER:-${SUDO_USER:-pi}}"
REC_HOME="$(getent passwd "${REC_USER}" | cut -d: -f6)"
APP_DIR="${REC_HOME}/rpi5-web"
SERVICE_NAME="rpi5-web-recorder.service"

AP_SSID="${AP_SSID:-RPiRecorder}"
# Дефолт свідомо простий — так вимагає таск. Пристрій польовий: оператор
# набирає пароль на телефоні, іноді в рукавицях, іноді поспіхом. Випадковий
# 16-символьний рядок тут коштує більше, ніж дає: пароль однаково видно
# всім, хто має доступ до пристрою, а мережа не має виходу в інтернет.
# Хто працює на локації з кількома рекордерами — міняє SSID/пароль через
# веб-інтерфейс або AP_PASSWORD=... на деплої.
AP_PASSWORD="${AP_PASSWORD:-12345678}"
AP_IFACE="${AP_IFACE:-wlan0}"
AP_CON_NAME="rpi-web-ap"
# Spool-тека для privilege-separated rename AP: веб (непривілейований) кладе
# бажаний конфіг сюди, root-вотчер (ap_apply.sh) забирає й застосовує.
SPOOL_DIR="/var/lib/rpi5-web"
APPLY_SERVICE="rpi5-ap-apply.service"
APPLY_PATH="rpi5-ap-apply.path"

cat <<WARNING >&2
################################################################################
# УВАГА: цей скрипт підніме власну WiFi точку доступу (AP) на ${AP_IFACE}.
# Якщо ти зараз підключена до Pi по SSH саме через ${AP_IFACE} (WiFi-клієнт),
# ця сесія обірветься ДО того, як зʼявиться підсумкове повідомлення з адресою
# веб-інтерфейсу — інтерфейс перемкнеться з клієнта на AP просто посеред
# виконання скрипта.
#
# Рекомендація: запускай через Ethernet, серійну консоль, або вкажи інший
# інтерфейс — AP_IFACE=wlan1 sudo -E bash pi/install_web.sh
################################################################################
WARNING

apt-get update
# dnsmasq-base: без нього ipv4.method=shared не роздає DHCP клієнтам AP.
# Це лише Recommends пакета network-manager, не Depends — на мінімальному
# образі з Install-Recommends=false міг би тихо не встановитись, тому тут
# явно.
apt-get install -y ffmpeg rpicam-apps python3-flask network-manager dnsmasq-base

# на свіжому образі wifi-адаптер часто лежить soft-blocked через rfkill,
# так само як bluetooth в install_ble.sh
rfkill unblock wifi || true

# nmcli AP-профіль (ipv4.method shared) без regulatory domain може не
# обрати легальний 2.4GHz канал — `nmcli con up` тоді падає без діагностики
# в межах досяжності (AP так і не з'явився, SSH вже нема), а systemd-юніт
# з Restart=always просто крутиться по колу. raspi-config nonint
# do_wifi_country виставляє regdomain явно; якщо утиліти нема (мінімальний
# образ) — просто пропускаємо, а не падаємо.
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_wifi_country "${AP_COUNTRY:-UA}" || true
fi

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
install -m 755 "$(dirname "$0")/ap_apply.sh" "${APP_DIR}/ap_apply.sh"
chown -R "${REC_USER}:${REC_USER}" "${APP_DIR}"
mkdir -p "${REC_HOME}/recordings"
chown "${REC_USER}:${REC_USER}" "${REC_HOME}/recordings"

# Spool-тека: root володіє, веб-юзер має право писати (кладе бажаний конфіг).
mkdir -p "${SPOOL_DIR}"
chown "root:${REC_USER}" "${SPOOL_DIR}"
chmod 770 "${SPOOL_DIR}"

# Поточний стан AP (для показу у веб-UI) — сідуємо тим, що ставимо зараз.
printf '{"ssid": "%s", "password": "%s"}\n' "${AP_SSID}" "${AP_PASSWORD}" \
  >"${APP_DIR}/ap-current.json"
chmod 600 "${APP_DIR}/ap-current.json"
chown "${REC_USER}:${REC_USER}" "${APP_DIR}/ap-current.json"

# Пароль AP переживає термінальний scrollback — оператору треба його
# знайти пізніше, а не тільки в момент інсталяції.
printf '%s\n' "${AP_PASSWORD}" >"${APP_DIR}/.ap_password"
chmod 600 "${APP_DIR}/.ap_password"
chown "${REC_USER}:${REC_USER}" "${APP_DIR}/.ap_password"

if ! nmcli -t -f NAME con show | grep -qx "${AP_CON_NAME}"; then
  nmcli con add type wifi ifname "${AP_IFACE}" con-name "${AP_CON_NAME}" \
    ssid "${AP_SSID}" \
    802-11-wireless.mode ap 802-11-wireless.band bg \
    802-11-wireless-security.key-mgmt wpa-psk \
    802-11-wireless-security.psk "${AP_PASSWORD}" \
    ipv4.method shared ipv4.addresses 10.42.0.1/24 \
    connection.autoconnect yes
else
  # Той самий повний набір властивостей, що й у add-гілці вище — щоб
  # повторний запуск інсталятора (наприклад з новим AP_PASSWORD) полагодив
  # і будь-який дрейф mode/band/key-mgmt/ipv4, не тільки ssid/psk.
  nmcli con modify "${AP_CON_NAME}" \
    802-11-wireless.ssid "${AP_SSID}" \
    802-11-wireless.mode ap 802-11-wireless.band bg \
    802-11-wireless-security.key-mgmt wpa-psk \
    802-11-wireless-security.psk "${AP_PASSWORD}" \
    ipv4.method shared ipv4.addresses 10.42.0.1/24
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
Environment=RAW_FPS=10 FREE_MB_MIN=500
Environment=AP_CURRENT_FILE=${APP_DIR}/ap-current.json
Environment=AP_SPOOL_FILE=${SPOOL_DIR}/ap-config.json
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

# Root-вотчер rename AP: path-юніт стежить за spool-файлом, service його
# застосовує від root. Веб-сервіс сам прав на nmcli con modify не має.
cat >"/etc/systemd/system/${APPLY_SERVICE}" <<UNIT
[Unit]
Description=Apply RPi5 AP SSID/password change from web spool
After=NetworkManager.service

[Service]
Type=oneshot
Environment=AP_CON_NAME=${AP_CON_NAME}
Environment=AP_SPOOL_FILE=${SPOOL_DIR}/ap-config.json
Environment=AP_CURRENT_FILE=${APP_DIR}/ap-current.json
Environment=CURRENT_OWNER=${REC_USER}
ExecStart=${APP_DIR}/ap_apply.sh
UNIT

cat >"/etc/systemd/system/${APPLY_PATH}" <<UNIT
[Unit]
Description=Watch for RPi5 AP config change from web

[Path]
PathExists=${SPOOL_DIR}/ap-config.json
Unit=${APPLY_SERVICE}

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "${APPLY_PATH}"
systemctl enable --now "${SERVICE_NAME}"

systemctl --no-pager status "${SERVICE_NAME}" || true
echo "=============================================================="
echo "AP ssid:     ${AP_SSID}"
echo "AP password: ${AP_PASSWORD}"
echo "web UI:      http://10.42.0.1/"
echo "password also saved to: ${APP_DIR}/.ap_password (chmod 600)"
echo "=============================================================="
echo "logs: journalctl -u ${SERVICE_NAME} -f"
