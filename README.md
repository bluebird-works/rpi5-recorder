# rpi5-recorder

Відео-реєстратор на Raspberry Pi 4 / Pi 5 з **будь-якою офіційною CSI-камерою** (Camera Module v1/v2/v3, HQ, Global Shutter — код сам визначає сенсор при старті). Три незалежні режими:

- **A. BLE web control** — старт/стоп зі смартфона через Web Bluetooth
- **B. Auto-record** — запис починається одразу після подачі живлення
- **C. WiFi AP + web control** — Pi піднімає власну WiFi-точку доступу, старт/стоп через звичайний браузер

Усі три режими пишуть `mp4` в `~/recordings/` з ротацією (макс 50 файлів). Довжина одного файлу задається змінною `SEGMENT_SEC` (сек). Дефолт `0` = **не різати**, писати одним файлом на всю сесію.

Енкодер обирається автоматично під залізо:
- **Pi 4** має апаратний H.264 (`h264_v4l2m2m`) — `rpicam-vid` віддає готовий elementary stream, ffmpeg тільки муксить його в mp4 без перекодування. CPU майже вільний.
- **Pi 5** апаратного енкодера не має — камера віддає сирий YUV, кодує ffmpeg (libx264 ultrafast).

Обидва шляхи дають однаковий результат: 1080p30 без дропів, сегменти рівно по `SEGMENT_SEC`. Фокус за замовчуванням — **фіксована безкінечність** (тільки для Camera Module 3 з моторним фокусом; решта сенсорів фіксовані апаратно).

Веб-сторінка клієнта: **https://bluebird-works.github.io/rpi5-recorder/**

---

## Швидкий старт (TL;DR)

Для тих, хто хоче побачити запис за 5 хвилин, без розуміння деталей.

1. Прошив SD Raspberry Pi Imager'ом (в Advanced options: enable SSH, задав username/password/WiFi).
2. Устромив CSI-шлейф камери у Pi (**сріблястим боком до плати, при вимкненому живленні**), подав живлення.
3. З ноута:
   ```bash
   ssh user@<ip-pi>
   git clone https://github.com/bluebird-works/rpi5-recorder.git
   cd rpi5-recorder
   sudo bash pi/install_ble.sh
   ```
4. На **Android**-телефоні у Chrome відкрив https://bluebird-works.github.io/rpi5-recorder/
5. Тиснеш «Підключити» → обираєш `RPi5-CAM` → «Pair» → `● Запис`.

Все. Файли лежать в `~/recordings/` на Pi. Забрати:
```bash
scp user@<ip-pi>:~/recordings/*.mp4 ./
```

Якщо щось не так — див. розділ **8. Діагностика**.

---

## 1. Що потрібно

| Компонент | Мінімум |
|---|---|
| Raspberry Pi 4 або 5 | 2 GB+, Raspberry Pi OS / Debian 12+ |
| SD-карта | 32 GB+ (краще A2) |
| CSI-камера | Будь-яка офіційна: Camera Module v1 (OV5647), v2 (IMX219), v3 (IMX708), HQ (IMX477), GS (IMX296). Перевірка: `rpicam-hello --list-cameras` — має вивести сенсор у першому рядку |
| Живлення | офіційний БЖ (Pi 4 — 5 V / 3 A) |
| Мережа | тільки для першого налаштування (SSH). Далі BLE — офлайн. |
| Смартфон/ноутбук | Android Chrome/Edge або desktop Chrome/Edge/Opera. **iOS Safari та Firefox — не працюють** (немає Web Bluetooth). |

### Що має стояти на самій Pi

Ставиться автоматично інсталяторами, руками нічого доставляти не треба:

| Пакет | Навіщо | Хто ставить |
|---|---|---|
| `rpicam-apps` | `rpicam-vid` — захоплення з CSI-камери + апаратний H.264 | усі режими |
| `ffmpeg` | муксинг elementary stream у mp4, нарізка на сегменти | усі режими |
| `bluez` | `bluetoothd` (GATT-сервер) + `btmgmt` | режим A |
| `python3-bluezero` | Python-обгортка BlueZ через D-Bus. Немає в apt Debian 13 → ставиться через `pip3 --break-system-packages` | режим A |
| `python3-dbus`, `python3-gi` | залежності bluezero | режим A |
| `network-manager` | `nmcli` — піднімає WiFi точку доступу (`ipv4.method shared`) | режим C |
| `dnsmasq-base` | DHCP/DNS для клієнтів AP, викликається `nmcli` як внутрішній хелпер | режим C |
| `python3-flask` | веб-сервер статусу/старт-стоп | режим C |

Плюс інсталятор сам:
- додає користувача в групу `video` (доступ до камери);
- режим A: знімає `rfkill` з Bluetooth і піднімає `hci0` (на свіжому образі адаптер часто лежить `DOWN`);
- режим C: знімає `rfkill` з WiFi, за наявності `raspi-config` виставляє regulatory domain, конфігурує nmcli AP-профіль;
- створює systemd-юніт з автозапуском на буті.

Перевірити все разом — `setup/verify.sh`.

---

## 2. Клонування репозиторію на Pi

Реєстратор автономний — після налаштування живе без інтернету. Код на Pi треба залити **один раз** (плюс зрідка `git pull` коли міняється). GitHub є публічним, тож клон анонімно через HTTPS — без токенів і без ключів:

```bash
cd ~
git clone https://github.com/bluebird-works/rpi5-recorder.git
cd rpi5-recorder
```

Оновлення потім:
```bash
cd ~/rpi5-recorder && git pull
```

SSH-ключ до GitHub на Pi **не потрібен** — з реєстратора нічого туди не пушиться. (SSH між твоїм ноутом і Pi — це інша річ, потрібна щоб взагалі залізти на Pi, див. пункт 6.)

---

## 3. Встановлення

**Обери один режим.** Кілька одночасно ставити не варто — будуть битися за камеру.

### 3.1. Режим A — BLE web control

```bash
cd ~/rpi5-recorder
sudo bash pi/install_ble.sh
```

Що робить скрипт:
- ставить `python3-bluezero`, `ffmpeg`, `rpicam-apps`, `bluez`
- копіює `ble_recorder.py` в `~/rpi5-ble/`
- створює systemd-сервіс `rpi5-ble-recorder.service`
- вмикає автозапуск сервісу при бутстапі

Перевірка:
```bash
sudo systemctl status rpi5-ble-recorder
journalctl -u rpi5-ble-recorder -f
```

Має бути `advertising as 'RPi5-CAM'`.

### 3.2. Режим B — Auto-record

```bash
cd ~/rpi5-recorder
sudo bash pi/install_autostart.sh
```

Що робить скрипт:
- ставить `ffmpeg`, `rpicam-apps`
- копіює `autostart.sh` в `~/rpi5-auto/`
- створює systemd-сервіс `rpi5-auto-recorder.service`
- вмикає автозапуск при бутстапі

Перевірка:
```bash
sudo systemctl status rpi5-auto-recorder
ls -la ~/recordings/
```

Файли повинні зʼявлятись одразу після старту сервісу.

### 3.3. Режим C — WiFi AP + web control

> **Обережно з SSH.** Якщо ти зараз на Pi по SSH через WiFi (той самий `wlan0`, що стане AP-інтерфейсом) — сесія обірветься посеред установки, ДО того як в терміналі зʼявиться підсумкове повідомлення з паролем і адресою веб-інтерфейсу. Запускай через Ethernet, серійну консоль, або вкажи інший інтерфейс: `AP_IFACE=wlan1 sudo -E bash pi/install_web.sh` (`-E` обовʼязковий, інакше `sudo` скине змінну). Скрипт друкує це попередження прямим текстом при старті — не пропусти його.

```bash
cd ~/rpi5-recorder
sudo bash pi/install_web.sh
```

Що робить скрипт:
- ставить `network-manager`, `dnsmasq-base`, `python3-flask`, `ffmpeg`, `rpicam-apps`
- знімає `rfkill` з WiFi, за наявності `raspi-config` виставляє regulatory domain (`AP_COUNTRY`, дефолт `UA`)
- конфігурує `nmcli`-профіль точки доступу (`ipv4.method shared`, фіксована адреса `10.42.0.1/24`)
- копіює `recording_engine.py` і `web_recorder.py` в `~/rpi5-web/`
- створює systemd-сервіс `rpi5-web-recorder.service`
- вмикає автозапуск сервісу при бутстапі

Пароль AP, якщо не задано `AP_PASSWORD` явно, генерується випадковий (16 символів). Друкується наостанок і зберігається окремо у `~/rpi5-web/.ap_password` (chmod 600, власник — юзер рекордера) — щоб не загубився у scrollback:

```bash
AP_SSID:     RPiRecorder
AP_PASSWORD: <16 випадкових символів>
web UI:      http://10.42.0.1/
```

Перевірка:
```bash
sudo systemctl status rpi5-web-recorder
journalctl -u rpi5-web-recorder -f
```

---

## 4. Використання (режим A — BLE)

1. **Живимо Pi.** Ждемо ~15 с — Pi піднімається, стартує сервіс, починає advertising `RPi5-CAM`.
2. **На смартфоні** (Android Chrome/Edge) відкриваємо **https://bluebird-works.github.io/rpi5-recorder/**.
3. Тиснемо **«Підключити»** → системний діалог сканера → обираємо `RPi5-CAM` → **«Pair»**.
4. Через 2-3 секунди — UI показує «готово», активуються кнопки `● Запис` / `■ Стоп`.
5. **`● Запис`** — Pi починає писати. Червона лампочка блимає, статус «запис…».
6. **`■ Стоп`** — Pi коректно закриває mp4 (moov-атом на місці, файл програється).

Скинути UUID до дефолтних або задати свої — розгорни `<details>` «Налаштування BLE» на сторінці, вписуй значення, тисни «Зберегти» (лежить в `localStorage` браузера).

**Радіус BLE:** 5-10 м без перешкод. Через стіну ≈ 3-5 м.

**Reload сторінки = розконект.** Chrome не памʼятає пристрій між сесіями (privacy). Треба знову тиснути «Підключити».

---

## 5. Використання (режим B — Auto-record)

Плагін-плей:
- **Живимо Pi** → через ~15 с починається запис.
- **Знімаємо живлення** → systemd надсилає SIGTERM → скрипт валить `rpicam-vid`, ffmpeg добиває mp4 по EOF → shutdown. Останній файл не зіпсований.

Забрати відео:
```bash
scp pi@<pi-ip>:~/recordings/*.mp4 ./
```

---

## 5a. Що коли зникає живлення чи Pi перезавантажилась посеред запису

Рекордер побудований так, щоб такі ситуації **не втрачали більше 1 секунди відео** і продовжували запис після повернення живлення.

**Три сценарії, що покриті:**

1. **Ти закрила вкладку в браузері / телефон вимкнувся.**
   Pi продовжує писати. BLE-розʼєднання **не** зупиняє запис. Відкриваєш сторінку знову, тиснеш «Підключити» — UI сам покаже що йде запис. Тиснеш ■ коли треба.

2. **Pi здохла (kernel panic, SD-карта збоїть).**
   Після рестарту systemd за 3 секунди підіймає сервіс, той бачить `~/recordings/.recording_state` і **автоматично продовжує запис** новим файлом (`rec_YYYYMMDD_HHMMSS.mp4` з поточним часом). Файл, який лишився від крешу, **лишається програвабельним до останнього keyframe** (втрачається до ~1 секунди хвоста) — завдяки fragmented mp4 (`+frag_keyframe+empty_moov+default_base_moof`).

3. **Обірвалось живлення.**
   Те саме, що п.2, плюс атомарний запис state-файла (`tmp + fsync + rename`) переживає torn write. Наступний бут = продовження запису.

**Як воно взагалі знає, що треба продовжити?**
При натисканні `● Запис` рекордер пише файл-маркер `~/recordings/.recording_state` (маленький JSON з номером пресета). При чистому стопі через `■` — цей файл видаляється. Якщо сервіс піднімається і бачить маркер — значить, він упав під час запису, і треба відновити.

**Як зупинити «намертво» щоб більше не відновлювало:**
Тиснеш `■ Стоп` на телефоні — це прибирає state-файл. Або руками:
```bash
rm ~/recordings/.recording_state
sudo systemctl restart rpi5-ble-recorder
```

---

## 5b. Використання (режим C — WiFi AP + web control)

1. **Живимо Pi.** Через ~15-20 с піднімається `nmcli`-точка доступу і сервіс `rpi5-web-recorder`.
2. **На будь-якому пристрої** (телефон, ноутбук — не важливо, який ОС/браузер) підключаємось до WiFi-мережі `RPiRecorder` (SSID/пароль — з підсумкового виводу `install_web.sh` або `~/rpi5-web/.ap_password` на Pi).
3. Відкриваємо **http://10.42.0.1/** у браузері.
4. Сторінка показує статус (`Idle` / `Recording: rec_....mp4`), автооновлення раз на 2 с.
5. **«Старт»** — запускає запис на дефолтних `WIDTH/HEIGHT/FPS/BITRATE` з env (без вибору пресету в цій версії).
6. **«Стоп»** — коректно закриває mp4.

Немає підʼєднання до Pi (AP перезапустилась, зникло живлення) — сторінка сама покаже «нема звʼязку з рекордером» і заблокує кнопки замість того, щоб застрягти на застарілому статусі.

Забрати відео — так само, як в інших режимах:
```bash
scp pi@10.42.0.1:~/recordings/*.mp4 ./
```

**Про безпеку.** `web_recorder.py` слухає `0.0.0.0`, не тільки AP-інтерфейс. Якщо на Pi одночасно живий Ethernet або Tailscale поруч з AP, `/api/start`/`/api/stop` доступні без автентифікації і звідти теж. Це прийнятно в межах моделі загрози цього режиму — довірена закрита AP-мережа, а не публічний інтернет — але варто тримати в голові, а не вважати само собою зрозумілим.

Power-loss відновлення (crash-resume, fragmented mp4, ротація) працює так само, як описано в **5a** — логіка спільна для всіх режимів через `recording_engine.py`.

---

## 6. Налаштування SSH на Pi (якщо ще не робили)

### 6.1. Ввімкнути SSH (варіант A: під час прошивки SD)

В Raspberry Pi Imager перед записом натисни **⚙ Advanced options**:
- ✅ Enable SSH → Use public-key authentication only (вставити свій `id_ed25519.pub` з ноута)
- ✅ Set username and password (`pi` + сильний пароль)
- ✅ Configure wireless LAN (SSID + пароль домашнього WiFi)

Після першого бута — з ноута:
```bash
ssh pi@<pi-ip>
```

### 6.2. Ввімкнути SSH (варіант B: на вже прошитій системі)

Створити порожній файл `ssh` в `/boot/firmware/` (або підключитись з клавою+моніком і `sudo raspi-config` → `Interface Options` → `SSH` → Enable).

### 6.3. Знайти IP Pi

Якщо Pi в тій же мережі:
```bash
# з ноута
nmap -sn 192.168.1.0/24 | grep -B2 -i raspberry
# або
arp -a | grep -i raspberry
```

Або тимчасово підключити моніторчик і виконати `hostname -I` на самій Pi.

### 6.4. Ключ замість пароля (обовʼязково)

На **ноуті**:
```bash
ssh-copy-id pi@<pi-ip>
# або якщо ключа немає:
ssh-keygen -t ed25519 -C "$USER@$(hostname)"
ssh-copy-id -i ~/.ssh/id_ed25519.pub pi@<pi-ip>
```

Потім вимкнути парольний вхід (на Pi):
```bash
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

---

## 7. Конфігурація

Усі режими читають однакові змінні оточення для самого пайплайну запису — задаються при старті, код правити не треба. Режим C додає до них ще чотири свої, тільки для `install_web.sh` (мережевий шар, не пайплайн).

| Змінна | Дефолт | Що робить |
|---|---|---|
| `WIDTH` / `HEIGHT` | `1920` / `1080` | роздільність. **Вище 1920 по ширині HW-енкодер не вміє** |
| `FPS` | `30` | кадрів за секунду |
| `BITRATE` | `10000000` | 10 Mbps |
| `AUTOFOCUS_MODE` | `manual` | `manual`, `auto` або `continuous`. **Ігнорується для камер без моторного фокусу** (v1, v2, HQ, GS). Тільки Camera Module v3 підтримує. |
| `LENS_POSITION` | `0` | тільки для `manual` на v3. `0` = безкінечність, `default` = гіперфокал |
| `ENCODER` | `auto` | `auto` / `hardware` / `software`. `auto` визначає наявність `bcm2835-codec` — Pi 4 → апаратний, Pi 5 → софтверний |
| `SEGMENT_SEC` | `0` | `0` = один файл на сесію; `>0` = чанки по N сек |
| `REC_DIR` | `~/recordings` | куди писати |
| `MAX_FILES` | `50` | скільки файлів тримати |
| `FREE_MB_MIN` | `500` | нижче цього вільного місця — примусова ротація (тільки режим B) |
| `AP_SSID` | `RPiRecorder` | назва WiFi-точки доступу (тільки режим C) |
| `AP_PASSWORD` | випадковий 16-символьний | пароль WPA2 точки доступу. Якщо не задати — генерується на кожному запуску `install_web.sh` (тільки режим C) |
| `AP_IFACE` | `wlan0` | який WiFi-інтерфейс стає точкою доступу (тільки режим C) |
| `WEB_PORT` | `80` | порт, на якому слухає веб-UI (тільки режим C) |

Змінити на живому сервісі — писати чанки по 5 хвилин:
```bash
sudo systemctl edit rpi5-auto-recorder
# додати:
# [Service]
# Environment=SEGMENT_SEC=300
sudo systemctl restart rpi5-auto-recorder
```

Разово, без systemd:
```bash
REC_DIR=/tmp/test SEGMENT_SEC=10 LENS_POSITION=0 bash pi/autostart.sh
```

### Фокус
`AUTOFOCUS_MODE=manual` + `LENS_POSITION=0` — лінза жорстко на безкінечність, автофокус не смикається під час запису. Це дефолт: для реєстратора «різко все, що далі кількох метрів» кращий за автофокус, який перефокусовується на кожну зміну сцени.

Якщо треба різкість на близькій дистанції — `LENS_POSITION` задається як **обернена відстань** (діоптрії): `0.5` ≈ 2 м, `1` ≈ 1 м, `2` ≈ 0.5 м.

### Роздільність вище 1080p
Апаратний енкодер Pi 4 обмежений 1920 по ширині, тому для ширших кадрів треба явно `ENCODER=software`:
```bash
WIDTH=2304 HEIGHT=1296 ENCODER=software bash pi/autostart.sh
```
Заміряно на Pi 4 / `2304x1296`: софтверний libx264 тримає 30 fps, але грів чіп до 76°C проти 62°C на апаратному — без активного охолодження на довгому записі впреться в тротлінг. Повний сенсорний режим `4608x2592` видав 7.9 fps замість 14 — непридатний. На Pi 5 запас більший, але окремо не міряний.

### UUID (сервіс і характеристики BLE)
Дефолтні placeholder-и — у трьох місцях:
- `pi/ble_recorder.py` → `SERVICE_UUID`, `CHAR_UUID`, `STATUS_UUID`
- `index.html` → `DEFAULTS`
- цей README + `CLAUDE.md`

Згенерувати нові — `uuidgen`. Замінити всюди.

---

## 8. Діагностика

**Веб-сторінка каже «Web Bluetooth недоступний»** — браузер не той. Треба Chrome/Edge/Opera, не Firefox/Safari. На iOS — жодна опція не працює.

**Pi не зʼявляється в списку сканера** — перевір з іншого телефону через [nRF Connect](https://play.google.com/store/apps/details?id=no.nordicsemi.android.mcp) чи Pi взагалі рекламується:
```bash
sudo systemctl status rpi5-ble-recorder
sudo hciconfig                    # має бути UP RUNNING
sudo journalctl -u rpi5-ble-recorder -n 50
```

**Запис не стартує (BLE підʼєднаний, кнопка натиснута, але файлів нема)** — типово камера не бачиться:
```bash
rpicam-hello --list-cameras        # має показати сенсор: imx708 / imx219 / ov5647 / imx477 / imx296
groups | grep -o video             # має вивести "video"
```
Якщо `video` немає в групах — `sudo usermod -aG video $USER && sudo reboot`.
Якщо камери нема в списку — перевірити шлейф CSI (контактами до плати, при вимкненому живленні!) і `dmesg | grep -iE 'imx|ov5647'`.

**У логах `no CSI camera detected`** — рекордер запустив `rpicam-vid --list-cameras`, але не зміг зпарсити відповідь. Найчастіше — камера справді не підключена або шлейф болтається. Якщо `rpicam-hello --list-cameras` руками таки показує сенсор, а рекордер його не бачить — відкрий issue на GitHub із виводом обох команд.

**`ERROR: *** no cameras available ***`** — камеру вже тримає інший процес. Кілька сервісів одночасно не ставити: `systemctl status rpi5-ble-recorder rpi5-auto-recorder rpi5-web-recorder`.

**У логах `Timestamps are unset in a packet`** — косметично, ігнорувати. Вилазить один раз на перший пакет raw h264; тривалість і fps у готовому файлі коректні.

**Файли є, але не програються (moov-атом відсутній)** — станеться якщо процес вбити через `SIGKILL` замість `SIGTERM`. Systemd надсилає SIGTERM за замовчуванням, тож проблема тільки при hard-power-off посеред запису. Пом'якшено фрагментованим mp4 (`+frag_keyframe+empty_moov`) — файл лишається програвабельним і без moov.

**Пристрій `RPi5-CAM` не видно у сканері телефона.** Спершу перевір, що реклама реально в ефірі:
```bash
sudo btmgmt advinfo | grep -i "instances list"    # має бути "1 item", а не "0 items"
```
Якщо `0 items` — не піднявся обхід із `pi/ble_advertise.sh`, дивись логи `journalctl -u rpi5-ble-recorder`. У нормі там є рядок `Instance added: 1`.

Чому взагалі обхід: контролер Pi 4 (CYW43455) не підтримує LE Extended Advertising, а BlueZ 5.82 реєструє рекламу тільки через розширений mgmt-шлях і отримує від ядра `Invalid Parameters (0x0d)`. Через D-Bus рекламу підняти неможливо — bluezero отримує `org.bluez.Error.Failed`. Тому інстанс створюється напряму legacy-шляхом через `btmgmt`, а GATT-сервер лишається за `bluetoothd`. Деталі — в шапці `pi/ble_advertise.sh`.

У логах сервісу через це завжди є нешкідливий рядок від bluezero про невдалу реєстрацію реклами — це очікувано, GATT при цьому працює.

**BLE — це радіо на ~10 метрів.** Ні Tailscale, ні SSH тут не допоможуть: щоб підключитись зі смартфона, треба фізично бути поруч із Pi.

**Web Bluetooth debugger від Google** — лінк прямо на сторінці клієнта внизу.

---

## 9. Оновлення коду на Pi

```bash
cd ~/rpi5-recorder
git pull
# режим A:
sudo bash pi/install_ble.sh && sudo systemctl restart rpi5-ble-recorder
# режим B:
sudo bash pi/install_autostart.sh && sudo systemctl restart rpi5-auto-recorder
# режим C:
sudo bash pi/install_web.sh && sudo systemctl restart rpi5-web-recorder
```

---

## 10. Що НЕ підтримується

- USB/UVC-камери — код заточений під CSI через `rpicam-vid`, входу з `/dev/video0` немає.
- Роздільність вище 1920 по ширині на апаратному енкодері.
- Автофокус на камерах без моторного фокусу (v1, v2, HQ, GS). Рекордер їх запускає, але кадрування — на те, куди дивиться сенсор фізично. `AUTOFOCUS_MODE` / `LENS_POSITION` для них ігноруються (авто) — так задумано, інакше `rpicam-vid` падає.
- WebUSB — iOS не підтримує, Pi не в USB-gadget режимі.
- `raspap` / ручний `hostapd`+`dnsmasq` стек — свідомо не робимо навіть у режимі C, там де і так є своя WiFi-точка доступу: `nmcli`/NetworkManager (`ipv4.method shared`) — нативний шлях для Debian 13 trixie, простіший і без окремих конфігів.
- iOS Safari клієнт — Apple не запровадила Web Bluetooth.
