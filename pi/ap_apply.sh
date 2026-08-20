#!/usr/bin/env bash
# Root-вотчер зміни SSID/пароля AP. Запускається systemd path-юнітом, коли
# веб (непривілейований) кладе новий конфіг у spool. Веб не має прав на
# `nmcli con modify` — тому privilege-separation через файл, а не sudo/setuid.
#
# ЦЕЙ СКРИПТ ВИКОНУЄТЬСЯ ВІД ROOT. Він МУСИТЬ лежати в root-owned теці, куди
# веб-юзер (pi) не має write — інакше pi перезапише його й отримає root.
# Інсталятор кладе його в /usr/local/sbin, не в pi-owned APP_DIR.
#
# Spool пише процес, якому ми не довіряємо, тому валідуємо ВСЕ заново тут,
# перш ніж чіпати nmcli.
set -euo pipefail

AP_CON_NAME="${AP_CON_NAME:-rpi-web-ap}"
AP_SPOOL_FILE="${AP_SPOOL_FILE:-/var/lib/rpi5-web/spool/ap-config.json}"
# current-файл МУСИТЬ бути в root-only теці (див. install_web.sh) — pi лише
# читає, писати не може, тож не підмінить симлінком.
AP_CURRENT_FILE="${AP_CURRENT_FILE:-/var/lib/rpi5-web/ap-current.json}"

# Spool споживаємо на будь-якому виході — інакше path-юніт (PathExists) не
# побачить нового false→true переходу і rename тихо «залипне» назавжди.
trap 'rm -f "${AP_SPOOL_FILE}"' EXIT

[ -e "${AP_SPOOL_FILE}" ] || exit 0

# Читаємо, парсимо й ВАЛІДУЄМО в одному python-виклику з O_NOFOLLOW — щоб
# check і use були над тим самим file object (без TOCTOU-підміни симлінком
# між перевіркою й читанням) і щоб pi не націлив spool на чужий root-файл.
# Друкує рівно два рядки: SSID, потім пароль. Валідні значення не містять
# newline (лише \x20-\x7e), тому поділ по рядках безпечний.
CREDS="$(python3 - "${AP_SPOOL_FILE}" <<'PY'
import json, os, re, sys
path = sys.argv[1]
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
except OSError:
    sys.exit(1)  # symlink або зник — відмова
with os.fdopen(fd, "rb") as f:
    data = f.read()
try:
    cfg = json.loads(data)
except ValueError:
    sys.exit(1)
ssid = cfg.get("ssid", "")
psk = cfg.get("password", "")
if not (isinstance(ssid, str) and re.fullmatch(r"[\x20-\x7e]{1,32}", ssid)):
    sys.exit(1)
if not (isinstance(psk, str) and re.fullmatch(r"[\x20-\x7e]{8,63}", psk)):
    sys.exit(1)
# ssid/psk — байти в межах ASCII, довжина в символах == довжина в байтах.
sys.stdout.write(ssid + "\n" + psk + "\n")
PY
)" || { echo "ap_apply: invalid or unreadable spool, ignoring" >&2; exit 1; }

SSID="$(printf '%s' "${CREDS}" | sed -n '1p')"
PSK="$(printf '%s' "${CREDS}" | sed -n '2p')"

echo "ap_apply: setting SSID=${SSID}"
nmcli con modify "${AP_CON_NAME}" \
  802-11-wireless.ssid "${SSID}" \
  802-11-wireless-security.psk "${PSK}"

# Оновлюємо current-файл (звідки веб читає для показу). root:root, 644 —
# pi читає, але не може підмінити (тека root-only). Навмисно НЕ chown-имо.
printf '{"ssid": "%s", "password": "%s"}\n' "${SSID}" "${PSK}" >"${AP_CURRENT_FILE}"
chmod 644 "${AP_CURRENT_FILE}"

# Піднімаємо профіль наново — це РОЗІРВЕ активні підключення клієнтів,
# очікувано (SSID/пароль змінились, треба перепідключитись).
nmcli con up "${AP_CON_NAME}"
echo "ap_apply: applied, AP restarted"
