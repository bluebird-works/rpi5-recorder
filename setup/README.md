# setup/ — headless-встановлення Raspberry Pi OS

Ціль: залити Raspberry Pi OS на SD, підключити Pi до Wi-Fi і зайти по SSH **без монітора і клавіатури**.

Хост — Ubuntu 24.04, `rpi-imager 1.8.5+` (є в apt), Pi 4 + CSI Camera Module 3 (IMX708) + SD-карта ≥ 16 GB.

---

## TL;DR

1. `rpi-imager` → OS Customisation (шестерня / `Ctrl+Shift+X`) → заповнити hostname, SSH-ключ, Wi-Fi (SSID + пароль + country=UA), timezone
2. Написати образ на SD, вставити в Pi, ввімкнути живлення
3. Через 60–90 сек: `ssh pi@rpi5-cam.local` — має пустити

Якщо не пустило — див. розділ **Fallback** нижче.

---

## Чому цей шлях (а не старий wpa_supplicant.conf)

Bookworm перевів мережу з `dhcpcd` + `wpa_supplicant` на `NetworkManager`. Старий метод «покласти `wpa_supplicant.conf` в `/boot/`» — **мертвий**: файл читається тільки при першому буті, далі NM його ігнорує. Плюс окремий баг: Wi-Fi soft-blocked через `rfkill` поки не виставлений WLAN country code.

RPi Imager OS Customisation вміє обидва пункти правильно — він пише `firstrun.sh` (або `custom.toml` у нових версіях), який на першому буті створює `nmconnection`-файл, виставляє country code і знімає rfkill.

---

## Крок 1 — RPi Imager UI (основний метод)

```bash
rpi-imager
```

- **Device** → `Raspberry Pi 4`
- **OS** → `Raspberry Pi OS (other)` → `Raspberry Pi OS Lite (64-bit)`
  - Lite, не Desktop: менше пише на SD (важливо для дрон-кейсу з рваним живленням), нема GUI що жере ресурси.
- **Storage** → твоя SD-карта (перевір розмір, щоб не переплутати з іншим диском!)
- Натисни **Next**. З'явиться питання про OS Customisation → **Edit Settings**

### OS Customisation → General

| Поле | Значення |
|---|---|
| Set hostname | `rpi5-cam` |
| Set username and password | username=`pi`, пароль — довільний, тільки як fallback (SSH піде по ключу) |
| Configure wireless LAN | ✅ ввімкнути |
| SSID | твоя мережа |
| Password | пароль мережі |
| Wireless LAN country | `UA` (**обов'язково**, інакше rfkill soft-block) |
| Set locale settings | Timezone=`Europe/Kyiv`, Keyboard=`us` |

### OS Customisation → Services

- ✅ **Enable SSH** → «Allow public-key authentication only»
- Встав вміст `~/.ssh/id_ed25519.pub` (якщо ключа нема — згенеруй `ssh-keygen -t ed25519`)

### OS Customisation → Options

- ✅ Eject media when finished — щоб не смикати вручну

**Save → Yes** (застосувати customisation) → **Yes** (стерти SD) → чекай прошивку + verify.

---

## Крок 2 — перший бут

1. Встав SD в Pi 4, приєднай CSI-камеру шлейфом (контактами до плати, фіксатор роз'єму притиснути), подай живлення (офіційний 5 V / 3 A USB-C).
2. Зелений LED моргає ~30–60 сек (перший бут довший, бо firstrun-скрипт розширює rootfs і налаштовує NM).
3. Ще ~30 сек — Pi ребутиться і піднімає Wi-Fi.
4. З твого Ubuntu:

```bash
ping -c 3 rpi5-cam.local
```

Якщо відповідає — все ОК, роби `ssh pi@rpi5-cam.local`.

Якщо `.local` не резолвиться (Avahi/mDNS не всюди працює):

```bash
# знайти Pi в LAN за MAC-префіксом
nmap -sn 192.168.1.0/24 | grep -B 2 -i "raspberry\|b8:27\|dc:a6\|d8:3a\|2c:cf"
# або
arp -an | grep -i "b8:27\|dc:a6\|d8:3a\|2c:cf"
```

Потім `ssh pi@<IP>`.

---

## Крок 3 — перевірка після SSH (обов'язково)

Скопіюй і виконай `verify.sh` з цієї папки, або руками:

```bash
# Wi-Fi не заблокований?
rfkill list                    # Soft blocked: no, Hard blocked: no
# Країна виставлена?
iw reg get                     # country UA (а не 00)
# NM бачить wlan0 як connected?
nmcli device status            # wlan0 → connected
# Інет живий?
ping -c 3 -I wlan0 1.1.1.1
# ffmpeg є?
ffmpeg -version | head -1
# Камера видима? (має показати imx708)
rpicam-hello --list-cameras
# Апаратний H.264-енкодер на місці?
ffmpeg -hide_banner -encoders | grep h264_v4l2m2m
# Для BLE-режиму: адаптер піднятий?
hciconfig hci0                 # має бути UP RUNNING, а не DOWN
```

Якщо все зелене — переходиш до `pi/install_ble.sh` або `pi/install_autostart.sh`.

---

## Fallback — якщо OS Customisation не спрацював

Симптом: Pi завантажився, зелений LED моргає, але через 2 хв ping не проходить і SSH mert.

**Причина №1:** ти вибрала не той образ (32-bit / Legacy / не для Pi 4). Перепрошити на `Raspberry Pi OS Lite (64-bit)`.

**Причина №2:** Imager не записав customisation через якийсь глюк (буває). Тоді — ручний `custom.toml`:

1. Вийми SD з Pi, встав у комп.
2. Boot-розділ (FAT32, ~500 MB, монтується автоматично) називається `bootfs`.
3. Скопіюй `setup/custom.toml.example` → `<bootfs>/custom.toml`, відредагуй TODO-поля.
4. Встав SD назад у Pi, ввімкни живлення. `custom.toml` спрацює на першому буті і самовидалиться.

**Причина №3:** роутер має MAC-фільтрацію → додай MAC Pi у whitelist, або тимчасово вимкни фільтрацію.

**Причина №4:** SD-карта дохла. `Imager` міг пройти verify але карта потім розсипається під навантаженням. Спробуй іншу карту / інший кардрідер.

---

## Troubleshoot: Wi-Fi ліг **після** робочого сетапу

Найчастіший сценарій у твоєму дрон/реєстратор-юз-кейсі: різке вимикання живлення → корупція NM-state → Wi-Fi більше не піднімається.

Швидкий фікс з ethernet-кабелем (якщо є) або з монітора-один-раз:

```bash
sudo systemctl stop NetworkManager
sudo rm -f /etc/NetworkManager/system-connections/*.nmconnection
sudo systemctl start NetworkManager
sudo nmcli device wifi connect "TvijSSID" password "xxx"
```

Довгостроково — overlayfs / read-only rootfs. Це наступним ходом.

---

## Джерела

- ffmpeg-formats §4.71.1 — segment muxer вимоги (не по темі WiFi, для рекордера)
- [Bookworm networking change → NetworkManager](https://industrialmonitordirect.com/blogs/knowledgebase/reconfiguring-wifi-on-headless-raspberry-pi-os-lite-after-router-reset) — `wpa_supplicant.conf` deprecated
- [rpi forum thread 379629](https://forums.raspberrypi.com/viewtopic.php?t=379629) — rfkill + country code баг у листопадовому образі 2024
- [Kernovax gist](https://gist.github.com/Kernovax/cf39d9b00ecec0c70f8692bbc3b9a30b) — корупція NM після втрати живлення
- [raspberrypi/trixie-feedback #25](https://github.com/raspberrypi/trixie-feedback/issues/25) — Wi-Fi гине через час на Bookworm/Trixie
