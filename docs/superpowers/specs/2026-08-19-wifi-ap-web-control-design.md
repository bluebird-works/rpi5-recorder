# WiFi AP + Web Control — Mode C

Дата: 2026-08-19
Статус: погоджено з Дашею, готово до writing-plans

## Контекст і мета

Проєкт має два незалежні режими запису: **A. BLE web control** (`pi/ble_recorder.py` + `index.html` на GitHub Pages) і **B. Auto-record** (`pi/autostart.sh`). Обидва задокументовані в `CLAUDE.md` як свідомо ізольовані — BLE обрано саме тому, що телефон не мусить бути в мережі Pi (`feedback_ble_scope.md`, узгоджено з Дмитром 2026-07-30).

Новий таск від R&D просить третій, окремий канал керування: Pi піднімає власну WiFi AP на старті, у браузері з локального IP відкривається веб-інтерфейс зі статусом рекордера, таблицею записів (download/delete), можливістю перейменувати SSID/пароль AP (кілька рекордерів на одній локації), і апгрейд рекордера на raw-data режим.

**Це не заміна BLE-режиму.** Даша підтвердила: старий BLE-рекордер лишається без змін, новий режим — третій, паралельний, обирається на етапі деплою (`install_web.sh`, за зразком `install_ble.sh` / `install_autostart.sh`).

**Декомпозиція.** Повний таск — мінімум п'ять незалежних шматків: (1) WiFi AP, (2) веб-бекенд статусу/старт-стоп, (3) таблиця файлів з download/delete, (4) rename SSID/пароля, (5) raw-data режим запису. Цей спек покриває тільки **(1)+(2)** — AP піднімається на старті, і веб-сторінка зі статусом (Idle/Recording) та кнопками Старт/Стоп. Пункти (3)-(5) — окремі цикли brainstorm → spec → plan, свідомо не в цьому документі.

## Дослідження (WebSearch — Firecrawl MCP не підключено в цій сесії, використано WebSearch)

- nmcli-based AP — рекомендований шлях для Debian 13 (trixie) з NetworkManager, hostapd+dnsmasq — legacy, вимагає вимкнення NM: [raspiCamSrv Trixie hotspot guide](https://signag.github.io/raspi-cam-srv/latest/bp_Hotspot_Trixie/), [RaspberryTips access-point guide](https://raspberrytips.com/access-point-setup-raspberry-pi/), [Raspberry Pi Forums Trixie AP thread](https://forums.raspberrypi.com/viewtopic.php?t=395800).
- Це узгоджується з існуючою забороною в `CLAUDE.md` на raspap/hostapd — той запис писався для BLE-контексту ("не hotspot"), а нативний AP-шлях на Trixie і так не використовує hostapd.
- **Не перевірено наживо**: обидва тестові Pi (`recorder` 100.85.195.10, `recorder2` 100.109.56.87) на момент написання offline в тайлнеті Дмитра (`tailscale status` — recorder 4d ago, recorder2 4h ago). NetworkManager на реальному залізі не підтверджено — install-скрипт мусить це перевіряти явно, не припускати.
- Flask (`Context7 /pallets/flask`): `send_file()` зі шляхом (не `BytesIO`) підтримує range-requests і conditional/etag з коробки — актуально для майбутнього download відео (поза цим спеком, але вплинуло на вибір фреймворку зараз).

## Компоненти

### 1. `pi/recording_engine.py` (новий файл)

Копія (не імпорт, не рефакторинг) transport-agnostic частини `ble_recorder.py`: presets, `_detect_camera`, `_use_hw_encoder`, `_resolve_preset`, `_start_pipeline`, `start_recording`/`stop_recording`, `_rotator_loop`/`_sync_loop`, `_persist_state`/`_load_state`, `_prune_empty`.

**`ble_recorder.py` не змінюється взагалі** — ні рефакторингу, ні імпортів в жоден бік. Причина: код уже робочий і протестований на Pi 4, живого доступу до заліза для регресійного тесту зараз нема (обидва Pi offline) — ризик ламати перевірений флоу заради спільного модуля не виправданий, поки немає другого споживача, якому це реально потрібно без ризику.

Дублювання логіки між `ble_recorder.py` і `recording_engine.py` — усвідомлений компроміс. Майбутні зміни pipeline (наприклад raw-режим, пункт 5 декомпозиції) йдуть тільки в `recording_engine.py`; `ble_recorder.py` і далі живе окремим життям, як і зараз з `autostart.sh`.

### 2. `pi/web_recorder.py` (новий файл)

Flask-застосунок, імпортує тільки `recording_engine`. Один процес — стан (`state` dict + `threading.Lock()` з `recording_engine`) живе в пам'яті одного процесу, міжпроцесна координація не потрібна, бо це єдиний control-канал на деплой (BLE і Web ніколи не працюють одночасно на одному Pi — взаємовиключність на рівні install-скрипта, не рантайму).

Роути:
- `GET /` — HTML-сторінка: поточний статус (Idle/Recording), кнопки Старт/Стоп.
- `GET /api/status` — JSON `{"recording": bool, "filename": str|null}`, для polling з JS (без вибору пресету в цьому PR — старт іде на дефолтних `WIDTH/HEIGHT/FPS/BITRATE` з env, як зараз `autostart.sh`).
- `POST /api/start`, `POST /api/stop`.

`app.run(host="0.0.0.0", port=WEB_PORT, threaded=True)` — Flask dev-сервер офіційно "not for production", але тут єдиний клієнт у закритій AP-мережі без виходу в інтернет; окремий WSGI-сервер (gunicorn/waitress) — оверкіл на 2 GB Pi заради непублічного сервісу. Якщо в майбутньому зросте навантаження (кілька клієнтів, стрім файлів) — переглянути.

### 3. Мережевий шар

`nmcli` AP-профіль, не hostapd/dnsmasq:

```
nmcli con add type wifi ifname "${AP_IFACE}" con-name rpi-web-ap \
  ssid "${AP_SSID}" \
  802-11-wireless.mode ap 802-11-wireless.band bg \
  802-11-wireless-security.key-mgmt wpa-psk \
  802-11-wireless-security.psk "${AP_PASSWORD}" \
  ipv4.method shared connection.autoconnect yes
```

`ipv4.method shared` — NetworkManager сам піднімає DHCP; дефолтний гейтвей для shared-профілів — `10.42.0.1`. Це буде задокументований "локальний IP" (не розпливчасто "дивись роутер").

Env-дефолти (той самий стиль, що `WIDTH`/`HEIGHT` в `autostart.sh`): `AP_SSID=RPiRecorder`, `AP_PASSWORD=12345678`, `AP_IFACE=wlan0`, `WEB_PORT=80`.

### 4. `pi/install_web.sh` (новий файл, за зразком `install_ble.sh`)

- `apt-get install -y ffmpeg rpicam-apps python3-flask network-manager`
- Явна перевірка `systemctl is-active NetworkManager` перед конфігурацією AP — якщо неактивний, падає з чіткою помилкою (не намагається сама піднімати hostapd як fallback; на живому Pi це не перевірено, тому без сліпих припущень).
- Конфігурує nmcli-профіль (див. вище).
- Ставить systemd-юніт `rpi5-web-recorder.service`: `User=${REC_USER}`, `SupplementaryGroups=video` (як в BLE-юніті), `AmbientCapabilities=CAP_NET_BIND_SERVICE` + `CapabilityBoundingSet=CAP_NET_BIND_SERVICE` — щоб слухати порт 80 без рута, той самий принцип найменших прав що вже є в проєкті. `ExecStartPre=+/usr/bin/nmcli con up rpi-web-ap` (root тільки тут, як `rfkill`/`hciconfig` в BLE-юніті).

## Data flow

```
Power on
  → NetworkManager піднімає rpi-web-ap (autoconnect) → 10.42.0.1
  → rpi5-web-recorder.service стартує web_recorder.py
  → телефон конектиться на RPiRecorder / 12345678
  → браузер → http://10.42.0.1/ → GET /api/status (polling)
  → клік Старт → POST /api/start → recording_engine.start_recording()
                                   → rpicam-vid | ffmpeg (як зараз)
```

## Явно поза цим спеком

- Таблиця файлів (назва/розмір/дата/статус, download/delete + модалка підтвердження) — окремий spec.
- Rename SSID/пароля через UI — окремий spec (зараз тільки env-дефолти на деплої).
- Raw-data режим запису — окремий spec, торкнеться `recording_engine.py`.
- Live-тест на реальному Pi — обидва тестові пристрої offline на момент написання; перевірити при першій нагоді доступу до `recorder`/`recorder2`.

## Тестування (цей PR)

Живого Pi для регресії зараз нема. План перевірки:
- Статичний: `python3 -c "import ast; ast.parse(open('pi/recording_engine.py').read())"` і те саме для `web_recorder.py` — синтаксична валідність.
- Ручний review nmcli-команд проти документації Debian trixie (вище), без вигаданих прапорців.
- Явно позначити в PR, що live-тест на CSI-камері й реальній AP не проводився — заліза немає.
