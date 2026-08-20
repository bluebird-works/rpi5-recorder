#!/usr/bin/env bash
# Тест root-side валідації ap_apply.sh: незалежна перевірка SSID/пароля,
# відмова на симлінк, споживання spool. Без nmcli/root — переприсвоюємо
# nmcli на stub і ганяємо в tmpdir. Запуск: bash pi/tests/test_ap_apply.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="${HERE}/ap_apply.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# stub nmcli — просто логуємо аргументи, нічого не робимо
mkdir -p "${TMP}/bin"
cat >"${TMP}/bin/nmcli" <<'STUB'
#!/usr/bin/env bash
echo "nmcli $*" >>"${NMCLI_LOG}"
STUB
chmod +x "${TMP}/bin/nmcli"

pass=0
fail=0
check() {  # check <desc> <expected_rc> <actual_rc>
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1 (expected rc=$2, got $3)"
  fi
}

run() {  # run <spool-json> ; sets RC, SPOOL, CURRENT
  local spool="${TMP}/spool.json"
  printf '%s' "$1" >"${spool}"
  export NMCLI_LOG="${TMP}/nmcli.log"
  : >"${NMCLI_LOG}"
  PATH="${TMP}/bin:${PATH}" \
    AP_SPOOL_FILE="${spool}" \
    AP_CURRENT_FILE="${TMP}/current.json" \
    AP_CON_NAME="test-ap" \
    bash "${SCRIPT}" >/dev/null 2>&1
  RC=$?
}

# 1. Валідний конфіг застосовується, current оновлюється, spool спожито
run '{"ssid": "NewNet", "password": "goodpass1"}'
check "valid config applies" 0 "${RC}"
[ -f "${TMP}/current.json" ] && grep -q "NewNet" "${TMP}/current.json"
check "current file written" 0 $?
[ -f "${TMP}/spool.json" ]
check "spool consumed (removed)" 1 $?
grep -q "802-11-wireless.ssid test-ap" /dev/null 2>&1  # noop keep structure
grep -q "wireless.ssid" "${TMP}/nmcli.log"
check "nmcli con modify called" 0 $?

# 2. Короткий пароль відхиляється
run '{"ssid": "NewNet", "password": "short"}'
check "short password rejected" 1 "${RC}"

# 3. Задовгий SSID (33) відхиляється
run "{\"ssid\": \"$(printf 'x%.0s' {1..33})\", \"password\": \"goodpass1\"}"
check "long ssid rejected" 1 "${RC}"

# 4. Newline у SSID відхиляється (не проходить multiline-обхід)
run '{"ssid": "good\nrest", "password": "goodpass1"}'
check "newline in ssid rejected" 1 "${RC}"

# 5. Невалідний JSON відхиляється, але spool все одно прибирається (trap)
run 'not json at all'
check "bad json rejected" 1 "${RC}"
[ -f "${TMP}/spool.json" ]
check "bad-json spool still consumed" 1 $?

# 6. Симлінк на чужий файл — відмова (O_NOFOLLOW)
echo '{"ssid": "EvilNet", "password": "goodpass1"}' >"${TMP}/secret.json"
ln -sf "${TMP}/secret.json" "${TMP}/spool.json"
export NMCLI_LOG="${TMP}/nmcli.log"; : >"${NMCLI_LOG}"
PATH="${TMP}/bin:${PATH}" \
  AP_SPOOL_FILE="${TMP}/spool.json" \
  AP_CURRENT_FILE="${TMP}/current2.json" \
  AP_CON_NAME="test-ap" \
  bash "${SCRIPT}" >/dev/null 2>&1
check "symlinked spool refused" 1 $?
[ -f "${TMP}/current2.json" ]
check "no current written on symlink" 1 $?

echo "ap_apply.sh: ${pass} passed, ${fail} failed"
[ "${fail}" = 0 ]
