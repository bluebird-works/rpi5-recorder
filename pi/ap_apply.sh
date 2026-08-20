#!/usr/bin/env bash
# Root-вотчер зміни SSID/пароля AP. Запускається systemd path-юнітом, коли
# веб (непривілейований) кладе новий конфіг у spool. Веб не має прав на
# `nmcli con modify` — тому privilege-separation через файл, а не sudo/setuid.
#
# Spool пише процес, якому ми не довіряємо, тому валідуємо ВСЕ заново тут,
# перш ніж чіпати nmcli.
set -euo pipefail

AP_CON_NAME="${AP_CON_NAME:-rpi-web-ap}"
AP_SPOOL_FILE="${AP_SPOOL_FILE:-/var/lib/rpi5-web/spool/ap-config.json}"
# ВАЖЛИВО: current-файл МУСИТЬ бути в root-only теці, куди веб-юзер не має
# write. Інакше pi підмінює його симлінком на root-owned файл, і наші
# `printf >` / (колишній) chown стають root-write/chown primitive = LPE.
AP_CURRENT_FILE="${AP_CURRENT_FILE:-/var/lib/rpi5-web/ap-current.json}"

# Spool пише непривілейований процес — не йдемо за симлінком (pi міг би
# націлити його на root-файл, щоб root прочитав чуже).
if [ -L "${AP_SPOOL_FILE}" ]; then
  echo "ap_apply: spool is a symlink, refusing" >&2
  rm -f "${AP_SPOOL_FILE}"
  exit 1
fi
[ -f "${AP_SPOOL_FILE}" ] || exit 0

# jq відсутній у мінімальному образі — тягнемо поля python'ом (він завжди є).
read_field() {
  python3 -c "import json,sys; print(json.load(open('${AP_SPOOL_FILE}')).get('$1',''))"
}

SSID="$(read_field ssid)"
PSK="$(read_field password)"

# Валідація дублює веб-шар: SSID 1..32, пароль 8..63 друкованих ASCII.
if ! printf '%s' "${SSID}" | grep -qE '^[[:print:]]{1,32}$'; then
  echo "ap_apply: invalid SSID, ignoring spool" >&2
  rm -f "${AP_SPOOL_FILE}"
  exit 1
fi
if ! printf '%s' "${PSK}" | grep -qE '^[[:print:]]{8,63}$'; then
  echo "ap_apply: invalid password, ignoring spool" >&2
  rm -f "${AP_SPOOL_FILE}"
  exit 1
fi

echo "ap_apply: setting SSID=${SSID}"
nmcli con modify "${AP_CON_NAME}" \
  802-11-wireless.ssid "${SSID}" \
  802-11-wireless-security.psk "${PSK}"

# Оновлюємо current-файл (звідки веб читає для показу), потім піднімаємо
# профіль наново — це РОЗІРВЕ активні підключення клієнтів, очікувано.
# Файл лишається root:root (веб-юзер тільки читає) — навмисно НЕ chown-имо
# на pi: тека root-only, а chown по шляху, який pi не контролює, все одно
# зайвий. 644 достатньо для читання вебом.
printf '{"ssid": "%s", "password": "%s"}\n' "${SSID}" "${PSK}" >"${AP_CURRENT_FILE}"
chmod 644 "${AP_CURRENT_FILE}"

# Spool спожито — прибираємо, щоб path-юніт не зациклився.
rm -f "${AP_SPOOL_FILE}"

nmcli con up "${AP_CON_NAME}"
echo "ap_apply: applied, AP restarted"
