"""Transport-agnostic recording pipeline for rpi5-recorder Mode C (WiFi AP + web).

Copied out of ble_recorder.py's pipeline logic rather than imported from it —
ble_recorder.py is left untouched by design, see
docs/specs/2026-08-19-wifi-ap-web-control-design.md for why.
No BLE-specific pieces here (manual JSON config, presets, snapshot chunking):
this engine always runs a single env-configured recording, same convention
as autostart.sh. Supports two output modes: H.264/mp4 (default) and raw Bayer
(rpicam-raw, headerless + JSON sidecar). Also owns the recordings listing and
safe delete used by the web file table.
"""
import json
import logging
import os
import pathlib
import re
import signal
import subprocess
import threading
import time

REC_DIR = os.environ.get("REC_DIR", os.path.expanduser("~/recordings"))
WIDTH = int(os.environ.get("WIDTH", 1920))
HEIGHT = int(os.environ.get("HEIGHT", 1080))
FPS = int(os.environ.get("FPS", 30))
BITRATE = int(os.environ.get("BITRATE", 10_000_000))
ENCODER = os.environ.get("ENCODER", "auto")
AUTOFOCUS_MODE = os.environ.get("AUTOFOCUS_MODE", "manual")
LENS_POSITION = os.environ.get("LENS_POSITION", "0")
# Режим сенсора у форматі "W:H" для rpicam --mode. Він задає FoV і стелю
# fps, і не зобов'язаний збігатися з роздільністю запису: режим 2304:1296
# з виводом 1920x1080 дає повний кадр у 1080p. "" = хай libcamera обирає.
SENSOR_MODE = os.environ.get("SENSOR_MODE", "")
SEGMENT_SEC = int(os.environ.get("SEGMENT_SEC", 0))
MAX_FILES = int(os.environ.get("MAX_FILES", 50))
SYNC_INTERVAL_SEC = int(os.environ.get("SYNC_INTERVAL_SEC", 3))
PIPELINE_START_TIMEOUT = float(os.environ.get("PIPELINE_START_TIMEOUT", 3))
# rpicam-raw дропає кадри вище ~10 fps (офіційна документація), і сирий Bayer
# їсть диск на два порядки швидше за H.264 — тому окрема, нижча дефолтна
# частота, не перевикористання FPS=30.
RAW_FPS = int(os.environ.get("RAW_FPS", 10))
# Джерело камери. "csi" (дефолт) — CSI через rpicam, канон проєкту. "usb" —
# UVC-камера через V4L2 напряму в ffmpeg (окреме залізо, де CSI нема).
# Свідомо явний вибір на деплої, не авто, щоб не чіпати CSI-шлях і тести.
CAMERA_SRC = os.environ.get("CAMERA_SRC", "csi")
USB_DEVICE = os.environ.get("USB_DEVICE", "/dev/video0")
# USB UVC-камери майже завжди дають MJPEG (компресований, влазить у USB2 bus);
# YUYV на 1080p не влазить у bandwidth. ffmpeg декодує MJPEG і кодує в H.264.
USB_INPUT_FORMAT = os.environ.get("USB_INPUT_FORMAT", "mjpeg")
# Енкодер для USB-шляху. libx264 не тягне 1080p на слабких Pi (Pi 3 захлинається
# і кладе мережу через спільну USB/Ethernet шину). h264_v4l2m2m — апаратний,
# майже не жере CPU. Дефолт HW; libx264 як явний fallback (USB_ENCODER=libx264).
USB_ENCODER = os.environ.get("USB_ENCODER", "h264_v4l2m2m")
USE_USB = CAMERA_SRC == "usb"
# Нижче цього порогу вільного місця raw-запис зупиняється сам і зберігає вже
# зняте. H.264 натомість іде кільцевою ротацією (rotate_old_files), бо його
# бітрейт передбачуваний — raw же може прибити карту за хвилини.
FREE_MB_MIN = int(os.environ.get("FREE_MB_MIN", 500))
# Обидва розширення, які ми вважаємо записами. Ротація/список/видалення
# працюють по цьому набору.
REC_EXTS = (".mp4", ".raw")

# Стеля HW-енкодера по ширині (перевірено: "asked for 2304x1296, got 1920x1296").
# Вище неї апаратний шлях мовчки ріже кадр, тому кодуємо софтом.
HW_MAX_WIDTH = 1920

# Стеля fps на Pi 4 (bcm2835 ISP + HW H.264), заміряно на IMX708 2026-09-15:
# до 1280×720 чисто тримає 60, більший кадр — 30. Вище не встигає вже ISP, а не
# енкодер (сирий YUV 1280×720@120 без кодування — ~92 fps), і mp4 все одно
# маркується заявленим fps: 720p@120 давав ~67 реальних кадрів/с і прискорене
# відео. На Pi 5 такої стелі нема (1536×864@120 там чистий).
PI4_FPS_MAX = int(os.environ.get("PI4_FPS_MAX", 30))
PI4_FPS_MAX_SMALL = int(os.environ.get("PI4_FPS_MAX_SMALL", 60))
PI4_SMALL_PIXELS = 1280 * 720

# Перевірка після стопу: реальних кадрів/с менше цієї частки від заявленого fps —
# запис позначається попередженням. Коротші записи не міряємо: старт і дренаж
# пайплайна там з'їдають помітну частку часу й дають хибні тривоги.
FPS_CHECK_RATIO = float(os.environ.get("FPS_CHECK_RATIO", 0.95))
FPS_CHECK_MIN_SEC = 5

# Кандидати роздільності запису для UI. Фільтруються під обраний режим
# (не більші за нього і того ж співвідношення сторін), плюс завжди додається
# рідна роздільність самого режиму.
COMMON_RESOLUTIONS = [
    (640, 480), (800, 600), (1024, 768), (1280, 720), (1280, 960),
    (1600, 1200), (1920, 1080), (2028, 1520), (2304, 1296), (2560, 1440),
    (3280, 2464), (3840, 2160), (4608, 2592),
]

# Сенсори з моторним автофокусом. Для решти --autofocus-mode/--lens-position
# або впадуть, або тихо не дадуть кадрів (=> 0-байтний mp4).
AF_CAPABLE_SENSORS = {"imx708"}

# Робить mp4 «живучим» до крешу: moov не потрібен, дані самоописні по фрагментах.
FRAG_FLAGS = "+frag_keyframe+empty_moov+default_base_moof"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("recording_engine")

os.makedirs(REC_DIR, exist_ok=True)

# Персистентний стан для відновлення після падіння/power-cut. Пишемо на диск
# при старті запису, стираємо при чистому стопі.
STATE_FILE = os.path.join(REC_DIR, ".recording_state")

state = {
    "recording": False, "cam": None, "ff": None, "stop_event": None,
    "rot_thread": None, "sync_thread": None, "out_path": None,
    "stopping": False, "raw": False, "space_thread": None,
    "last_stop_reason": None, "started_at": None,
}
lock = threading.Lock()

# Знімок для перевірки кадру перед записом. Прихований (не .mp4/.raw), тож у
# список записів не потрапляє.
SNAPSHOT_PATH = os.path.join(REC_DIR, ".snapshot.jpg")

# Ім'я запису: rec_YYYYMMDD_HHMMSS.mp4 / .raw. Єдиний легальний шаблон —
# ним же валідуємо download/delete проти path traversal.
REC_NAME_RE = re.compile(r"^rec_\d{8}_\d{6}\.(?:mp4|raw)$")


def _parse_modes(text, max_width, max_height):
    """Витягує список сенсорних режимів із виводу `rpicam-vid --list-cameras`.

    Рядок режиму: "1536x864 [120.13 fps - (768, 432)/3072x1728 crop]".
    Crop-рект у ньому — це і є FoV: якщо він менший за повний сенсор, кадр
    вужчий. Повертає [] якщо блоку Modes нема (стара rpicam-apps / інший вивід)
    — тоді UI просто не покаже вибір режиму, запис не ламається.
    """
    modes = []
    for m in re.finditer(
        r"(\d+)x(\d+)\s*\[\s*([\d.]+)\s*fps\s*-\s*"
        r"\((\d+),\s*(\d+)\)/(\d+)x(\d+)\s*crop\]",
        text,
    ):
        w, h = int(m.group(1)), int(m.group(2))
        crop_w, crop_h = int(m.group(6)), int(m.group(7))
        ratio = round(crop_w / max_width, 4) if max_width else 1.0
        modes.append({
            "key": "%d:%d" % (w, h),
            "width": w,
            "height": h,
            "max_fps": float(m.group(3)),
            "crop": [int(m.group(4)), int(m.group(5)), crop_w, crop_h],
            # Допуск 1%: деякі сенсори віддають крихту менший рект на повному FoV.
            "full_fov": ratio >= 0.99,
            "fov_ratio": ratio,
        })
    return modes


def _detect_camera():
    """Читає перший рядок з `rpicam-vid --list-cameras`.

    Формат: '0 : imx708 [4608x2592 10-bit RGGB] (/base/...)'
    Повертає dict або None, якщо нічого не знайшли.
    """
    try:
        r = subprocess.run(
            ["rpicam-vid", "--list-cameras"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.warning("camera list failed: %s", e)
        return None
    text = (r.stdout or "") + (r.stderr or "")
    # Формат: '0 : imx708 [4608x2592 10-bit RGGB] (/base/...)'.
    # Bit-depth і Bayer-порядок потрібні лише raw-режиму (sidecar), тому
    # опційні: якщо не розпарсились — camera все одно валідна для H.264.
    m = re.search(r"^\s*(\d+)\s*:\s*(\S+)\s*\[(\d+)x(\d+)", text, re.M)
    if not m:
        log.warning("could not parse --list-cameras output:\n%s", text)
        return None
    sensor = m.group(2).lower()
    fmt = re.search(
        r"\[%dx%d\s+(\d+)-bit\s+([RGB]{4})\]" % (int(m.group(3)), int(m.group(4))),
        text,
    )
    return {
        "index": int(m.group(1)),
        "sensor": sensor,
        "max_width": int(m.group(3)),
        "max_height": int(m.group(4)),
        "has_autofocus": sensor in AF_CAPABLE_SENSORS,
        "bit_depth": int(fmt.group(1)) if fmt else None,
        "bayer_order": fmt.group(2) if fmt else None,
        "modes": _parse_modes(text, int(m.group(3)), int(m.group(4))),
    }


CAMERA = _detect_camera()
if CAMERA:
    log.info(
        "camera: %s max=%dx%d autofocus=%s modes=%d",
        CAMERA["sensor"], CAMERA["max_width"], CAMERA["max_height"],
        CAMERA["has_autofocus"], len(CAMERA["modes"]),
    )
else:
    log.warning("no CSI camera detected — рекордер стартує, але запис впаде")


def _has_hw_encoder_node(sysfs_root="/sys/class/video4linux"):
    """Pi 4 має апаратний H.264 (bcm2835-codec), Pi 5 — ні, там кодує CPU."""
    return any(
        p.read_text().strip() == "bcm2835-codec-encode"
        for p in pathlib.Path(sysfs_root).glob("*/name")
    )


def _use_hw_encoder(encoder_hint, sysfs_root="/sys/class/video4linux"):
    # Вище HW_MAX_WIDTH апаратний енкодер не тягне — навіть якщо його явно
    # попросили, тихий кроп кадру гірший за софтверний шлях.
    if WIDTH > HW_MAX_WIDTH:
        return False
    if encoder_hint != "auto":
        return encoder_hint == "hardware"
    return _has_hw_encoder_node(sysfs_root)


# bcm2835-codec є тільки на Pi 4 — ним і розпізнаємо платформу зі стелею fps.
FPS_CAPPED = _has_hw_encoder_node()


def _fps_cap(width, height, mode_max_fps):
    """Найбільший fps, який залізо реально тримає для цієї роздільності."""
    cap = int(mode_max_fps)
    # USB-камера йде повз ISP — заміри Pi 4 до неї не застосовні.
    if FPS_CAPPED and not USE_USB:
        limit = PI4_FPS_MAX_SMALL if width * height <= PI4_SMALL_PIXELS else PI4_FPS_MAX
        cap = min(cap, limit)
    return cap


# --- Налаштування зйомки (режим сенсора / роздільність / fps) -----------------
# Живуть у module-globals (WIDTH/HEIGHT/FPS/SENSOR_MODE), бо весь модуль так
# написаний, а на диску дублюються тут — щоб вибір із веб-панелі пережив
# ребут і power-cut, як і .recording_state.
SETTINGS_FILE = os.path.join(REC_DIR, ".settings.json")


def _save_settings(data):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        log.warning("could not save settings: %s", e)


def _load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not load settings: %s", e)
        return None


def get_settings():
    return {
        "mode": SENSOR_MODE or "auto",
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
    }


def _aspect_ok(w, h, mw, mh):
    """±2% — щоб 1920x1080 пройшло під 2304x1296, а 4:3 під 16:9 не пройшло."""
    if not (h and mh):
        return False
    return abs(w / h - mw / mh) / (mw / mh) <= 0.02


def _resolutions_for(mode, strict_aspect=True):
    """Список роздільностей запису, допустимих для режиму сенсора.

    mode=None — обмежень з боку режиму нема (auto або USB): тоді просто всі
    кандидати, без фільтра по aspect.
    """
    max_w = mode["width"] if mode else (
        CAMERA["max_width"] if (CAMERA and not USE_USB) else 4096)
    max_h = mode["height"] if mode else (
        CAMERA["max_height"] if (CAMERA and not USE_USB) else 4096)
    pairs = [
        (w, h) for w, h in COMMON_RESOLUTIONS
        if w <= max_w and h <= max_h
        and (not (mode and strict_aspect)
             or _aspect_ok(w, h, mode["width"], mode["height"]))
    ]
    if mode and (mode["width"], mode["height"]) not in pairs:
        pairs.append((mode["width"], mode["height"]))
    pairs.sort()
    mode_max_fps = mode["max_fps"] if mode else _max_auto_fps()
    return [{"width": w, "height": h, "hw": w <= HW_MAX_WIDTH,
             "max_fps": _fps_cap(w, h, mode_max_fps)} for w, h in pairs]


def _mode_label(mode):
    fov = ("повний FoV" if mode["full_fov"]
           else "кроп FoV %.2f×" % mode["fov_ratio"])
    return "%d×%d · %s · ≤%d fps" % (
        mode["width"], mode["height"], fov, int(mode["max_fps"]))


def _max_auto_fps():
    modes = (CAMERA or {}).get("modes") or []
    return max((m["max_fps"] for m in modes), default=120.0)


def camera_info():
    """Все, що потрібно веб-панелі для селекторів: режими (FoV), допустимі
    роздільності до кожного з них і поточний вибір."""
    modes = [] if USE_USB else list((CAMERA or {}).get("modes") or [])
    out_modes = []
    if modes:
        out_modes.append({
            "key": "auto",
            "label": "автовибір · ≤%d fps" % int(_max_auto_fps()),
            "width": CAMERA["max_width"],
            "height": CAMERA["max_height"],
            "max_fps": _max_auto_fps(),
            "full_fov": True,
            "fov_ratio": 1.0,
            "resolutions": _resolutions_for(None),
        })
        for m in modes:
            out_modes.append(dict(m, label=_mode_label(m),
                                  resolutions=_resolutions_for(m)))
    return {
        "src": "usb" if USE_USB else "csi",
        "sensor": None if USE_USB else (CAMERA or {}).get("sensor"),
        "hw_max_width": HW_MAX_WIDTH,
        "modes": out_modes,
        # Для auto/USB — коли режим не обраний або його поняття не існує.
        "resolutions": _resolutions_for(None),
        "settings": get_settings(),
    }


def apply_settings(payload):
    """Змінює режим/роздільність/fps для НАСТУПНОГО запису.

    Повертає (ok, error). Активний запис не чіпаємо — камера вже відкрита з
    іншими параметрами, перезапуск на льоту рвав би файл.
    """
    with lock:
        if state["recording"]:
            return False, "не можна міняти під час запису"

    cur = get_settings()
    try:
        width = int(payload.get("width", cur["width"]))
        height = int(payload.get("height", cur["height"]))
        fps = int(payload.get("fps", cur["fps"]))
    except (TypeError, ValueError):
        return False, "роздільність і fps мають бути числами"

    mode_key = str(payload.get("mode") or "auto")
    mode = None
    if USE_USB:
        # У UVC-камери поняття сенсорного режиму (і FoV) немає.
        mode_key = "auto"
    elif mode_key != "auto":
        mode = next(
            (m for m in ((CAMERA or {}).get("modes") or []) if m["key"] == mode_key),
            None,
        )
        if mode is None:
            return False, "невідомий режим сенсора %s" % mode_key

    if mode:
        max_w, max_h, max_fps = mode["width"], mode["height"], mode["max_fps"]
    elif CAMERA and not USE_USB:
        max_w, max_h = CAMERA["max_width"], CAMERA["max_height"]
        max_fps = _max_auto_fps()
    else:
        max_w, max_h, max_fps = 4096, 4096, 120.0

    if width % 2 or height % 2:
        return False, "роздільність має бути парною"
    if not (128 <= width <= max_w and 96 <= height <= max_h):
        return False, "роздільність %dx%d не влазить у режим (макс %dx%d)" % (
            width, height, max_w, max_h)
    ceiling = _fps_cap(width, height, max_fps)
    if not (1 <= fps <= ceiling):
        return False, "fps %d поза межами для %dx%d (1–%d)" % (
            fps, width, height, ceiling)
    if mode and not _aspect_ok(width, height, mode["width"], mode["height"]):
        return False, "співвідношення сторін не збігається з режимом %s" % mode_key

    globals().update(
        SENSOR_MODE="" if mode_key == "auto" else mode_key,
        WIDTH=width, HEIGHT=height, FPS=fps,
    )
    _save_settings({"mode": mode_key, "width": width, "height": height, "fps": fps})
    log.info("settings: mode=%s %dx%d@%d", mode_key, width, height, fps)
    return True, None


def _restore_saved_settings():
    """Збережений вибір перекриває env на старті сервісу. Без валідації проти
    камери: якщо залізо змінили, rpicam впаде помітно, а тихо підмінити вибір
    користувача гірше."""
    saved = _load_settings()
    if not saved:
        return
    try:
        globals().update(
            SENSOR_MODE="" if saved.get("mode", "auto") == "auto" else str(saved["mode"]),
            WIDTH=int(saved["width"]), HEIGHT=int(saved["height"]),
            FPS=int(saved["fps"]),
        )
    except (KeyError, TypeError, ValueError) as e:
        log.warning("ignoring bad saved settings: %s", e)
        return
    log.info("restored settings: mode=%s %dx%d@%d",
             saved.get("mode"), WIDTH, HEIGHT, FPS)


_restore_saved_settings()


def _persist_state(active, raw=False):
    if active:
        # Atomic write: temp + rename, захист від torn write при power loss.
        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"active": True, "raw": raw}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            log.warning("could not persist state: %s", e)
    else:
        try:
            os.remove(STATE_FILE)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not clear state: %s", e)


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not load state: %s", e)
        return None


def _prune_empty():
    """Прибирає 0-байтні записи, що лишились коли процес відкрив файл, але жоден
    байт не долетів до диска до power-cut (ext4 delayed alloc)."""
    try:
        names = os.listdir(REC_DIR)
    except OSError as e:
        log.warning("prune scan failed: %s", e)
        return
    for name in names:
        if not name.endswith(REC_EXTS):
            continue
        p = os.path.join(REC_DIR, name)
        try:
            if os.path.getsize(p) == 0:
                os.remove(p)
                # Сайдкар осиротілого raw теж прибрати.
                _remove_sidecar(p)
                log.info("pruned empty %s", name)
        except OSError:
            pass


def rotate_old_files():
    _prune_empty()
    files = sorted(
        (f for f in os.listdir(REC_DIR) if f.endswith(REC_EXTS)),
        reverse=True,
    )
    for old in files[MAX_FILES:]:
        p = os.path.join(REC_DIR, old)
        try:
            os.remove(p)
            _remove_sidecar(p)
            log.info("rotated %s", old)
        except OSError as e:
            log.warning("rotate failed for %s: %s", old, e)


def _sidecar_path(rec_path):
    """Сусідній rec_….json. У raw — метадані сенсора (без них headerless
    Bayer-потік нечитабельний), у mp4 — результат перевірки реального fps."""
    return os.path.splitext(rec_path)[0] + ".json"


def _remove_sidecar(rec_path):
    try:
        os.remove(_sidecar_path(rec_path))
    except OSError:
        pass


def _safe_rec_path(name):
    """Валідує ім'я запису й повертає абсолютний шлях усередині REC_DIR, або
    None. Три бар'єри проти path traversal: шаблон імені, realpath-containment,
    і що це справді файл."""
    if not REC_NAME_RE.match(name or ""):
        return None
    candidate = os.path.realpath(os.path.join(REC_DIR, name))
    root = os.path.realpath(REC_DIR)
    if os.path.dirname(candidate) != root:
        return None
    return candidate


def list_recordings():
    """[{name, size, created, status}] найновіші перші. status='recording' для
    активного файлу, інакше 'saved'. Сайдкари (.json) в список не потрапляють."""
    with lock:
        active = os.path.basename(state["out_path"]) if state["out_path"] else None
    out = []
    try:
        names = os.listdir(REC_DIR)
    except OSError as e:
        log.warning("list scan failed: %s", e)
        return out
    for name in names:
        if not name.endswith(REC_EXTS):
            continue
        p = os.path.join(REC_DIR, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({
            "name": name,
            "size": st.st_size,
            "created": int(st.st_mtime),
            "status": "recording" if name == active else "saved",
            "fps_check": _load_fps_check(p) if name.endswith(".mp4") else None,
        })
    out.sort(key=lambda r: r["name"], reverse=True)
    return out


def _load_fps_check(rec_path):
    try:
        with open(_sidecar_path(rec_path)) as f:
            data = json.load(f)
        return {k: data[k] for k in ("requested_fps", "real_fps", "ok")}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _count_video_frames(path):
    """Кількість відеопакетів у файлі (для H.264 у mp4 = кадрів) або None."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
             "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=120,
        )
        return int(r.stdout.strip().splitlines()[0].strip(","))
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError,
            ValueError, IndexError) as e:
        log.warning("frame count failed for %s: %s", os.path.basename(path), e)
        return None


def _check_recording(out_path, fps, wall_sec):
    """Реальний fps запису проти заявленого. Файл маркується заявленим fps
    незалежно від того, скільки кадрів камера встигла віддати, тож недобір
    видно тільки так: кадри / реальний час запису."""
    frames = _count_video_frames(out_path)
    if frames is None or wall_sec <= 0:
        return
    real = frames / wall_sec
    result = {
        "requested_fps": fps,
        "frames": frames,
        "wall_sec": round(wall_sec, 1),
        "real_fps": round(real, 1),
        "ok": real >= fps * FPS_CHECK_RATIO,
    }
    try:
        with open(_sidecar_path(out_path), "w") as f:
            json.dump(result, f)
    except OSError as e:
        log.warning("could not write fps check: %s", e)
    if not result["ok"]:
        log.warning("%s: real %.1f fps of requested %d — frames dropped",
                    os.path.basename(out_path), real, fps)


def delete_recording(name):
    """Видаляє запис (і його raw-сайдкар). Відмовляє для активного файлу.
    Повертає True при успіху, False інакше."""
    path = _safe_rec_path(name)
    if path is None:
        return False
    with lock:
        active = os.path.basename(state["out_path"]) if state["out_path"] else None
    if name == active:
        log.warning("refusing to delete active recording %s", name)
        return False
    try:
        os.remove(path)
    except OSError as e:
        log.warning("delete failed for %s: %s", name, e)
        return False
    _remove_sidecar(path)
    log.info("deleted %s", name)
    return True


def _start_pipeline():
    """rpicam-vid → ffmpeg → mp4. Повертає (cam, ff, out_path) або (None, None, None)."""
    # Сегмент ріжеться тільки по keyframe, тому GOP = довжині сегмента.
    intra = FPS * SEGMENT_SEC if SEGMENT_SEC > 0 else FPS
    cam_cmd = [
        "rpicam-vid", "-t", "0", "-n",
        "--width", str(WIDTH), "--height", str(HEIGHT),
        "--framerate", str(FPS),
    ]
    # Режим сенсора задає FoV незалежно від роздільності виводу.
    if SENSOR_MODE:
        cam_cmd += ["--mode", SENSOR_MODE]
    if CAMERA is None or CAMERA["has_autofocus"]:
        cam_cmd += ["--autofocus-mode", AUTOFOCUS_MODE]
        if AUTOFOCUS_MODE == "manual":
            cam_cmd += ["--lens-position", LENS_POSITION]

    # -flush_packets 1: не тримати пакети в user-space буфері ffmpeg, інакше
    # sync-loop нижче не встигає flushit'и фрагменти в kernel до power-cut.
    ff_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-flush_packets", "1",
    ]
    hardware = _use_hw_encoder(ENCODER)
    if hardware:
        cam_cmd += [
            "--bitrate", str(BITRATE), "--codec", "h264",
            "--inline", "--intra", str(intra),
        ]
        ff_cmd += [
            "-fflags", "+genpts", "-r", str(FPS), "-f", "h264", "-i", "-",
            "-c", "copy",
        ]
    else:
        # Софтверний шлях: камера віддає сирий YUV, кодує ffmpeg.
        cam_cmd += ["--codec", "yuv420"]
        ff_cmd += [
            "-f", "rawvideo", "-pix_fmt", "yuv420p",
            "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-b:v", str(BITRATE), "-pix_fmt", "yuv420p",
        ]
        if SEGMENT_SEC > 0:
            ff_cmd += [
                "-flags", "+cgop", "-g", str(intra), "-keyint_min", str(intra),
                "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SEC})",
            ]
    cam_cmd += ["-o", "-"]

    out_path = None
    if SEGMENT_SEC > 0:
        ff_cmd += [
            "-f", "segment", "-segment_time", str(SEGMENT_SEC),
            "-segment_format", "mp4",
            "-segment_format_options", f"movflags={FRAG_FLAGS}",
            "-reset_timestamps", "1", "-strftime", "1",
            os.path.join(REC_DIR, "rec_%Y%m%d_%H%M%S.mp4"),
        ]
    else:
        out_path = os.path.join(REC_DIR, time.strftime("rec_%Y%m%d_%H%M%S.mp4"))
        ff_cmd += ["-movflags", FRAG_FLAGS, "-f", "mp4", out_path]

    cam = subprocess.Popen(cam_cmd, stdout=subprocess.PIPE)
    ff = subprocess.Popen(ff_cmd, stdin=cam.stdout)
    cam.stdout.close()

    # Чекаємо подію (процес помер / перші байти в mp4), не фіксовану паузу —
    # велика роздільність ініціалізується довше, ніж 0.5с.
    deadline = time.monotonic() + PIPELINE_START_TIMEOUT
    while time.monotonic() < deadline:
        if cam.poll() is not None:
            break
        if out_path and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            break
        time.sleep(0.1)
    if cam.poll() is not None:
        log.error("rpicam-vid exited at start with code %s", cam.returncode)
        try:
            ff.wait(timeout=3)
        except subprocess.TimeoutExpired:
            ff.terminate()
            try:
                ff.wait(timeout=2)
            except subprocess.TimeoutExpired:
                ff.kill()
        if out_path and os.path.exists(out_path):
            try:
                if os.path.getsize(out_path) == 0:
                    os.remove(out_path)
                    log.info("removed empty %s", os.path.basename(out_path))
            except OSError:
                pass
        return None, None, None

    log.info(
        "pipeline start %dx%d@%d br=%d encoder=%s",
        WIDTH, HEIGHT, FPS, BITRATE, "hardware" if hardware else "software",
    )
    return cam, ff, out_path


def _start_usb_pipeline():
    """USB UVC-камера через V4L2 напряму в ffmpeg (один процес, без rpicam).
    Повертає (proc, None, out_path) — proc це ffmpeg, займає слот 'cam' у стані
    (той самий SIGTERM-стоп що й для CSI). Підтримує сегменти як software-CSI."""
    intra = FPS * SEGMENT_SEC if SEGMENT_SEC > 0 else FPS
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-flush_packets", "1",
        "-f", "v4l2", "-input_format", USB_INPUT_FORMAT,
        "-framerate", str(FPS), "-video_size", f"{WIDTH}x{HEIGHT}",
        "-i", USB_DEVICE,
    ]
    if USB_ENCODER == "copy":
        # Нативний потік камери (MJPEG) без транскоду — нуль CPU, нуль
        # залежності від HW-енкодера. Рятунок для слабких Pi (Pi 3), де
        # h264_v4l2m2m зависає, а libx264 не тягне 1080p. Файл більший.
        cmd += ["-c", "copy"]
    elif USB_ENCODER == "libx264":
        cmd += ["-c:v", "libx264", "-preset", "ultrafast",
                "-b:v", str(BITRATE), "-pix_fmt", "yuv420p"]
    else:
        # h264_v4l2m2m (апаратний): CPU майже вільний, де він працює.
        cmd += ["-c:v", USB_ENCODER, "-b:v", str(BITRATE), "-pix_fmt", "yuv420p"]
    out_path = None
    if SEGMENT_SEC > 0:
        cmd += [
            "-flags", "+cgop", "-g", str(intra), "-keyint_min", str(intra),
            "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SEC})",
            "-f", "segment", "-segment_time", str(SEGMENT_SEC),
            "-segment_format", "mp4",
            "-segment_format_options", f"movflags={FRAG_FLAGS}",
            "-reset_timestamps", "1", "-strftime", "1",
            os.path.join(REC_DIR, "rec_%Y%m%d_%H%M%S.mp4"),
        ]
    else:
        out_path = os.path.join(REC_DIR, time.strftime("rec_%Y%m%d_%H%M%S.mp4"))
        cmd += ["-movflags", FRAG_FLAGS, "-f", "mp4", out_path]

    proc = subprocess.Popen(cmd)
    deadline = time.monotonic() + PIPELINE_START_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if out_path and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            break
        time.sleep(0.1)
    if proc.poll() is not None:
        log.error("ffmpeg(usb) exited at start with code %s", proc.returncode)
        if out_path and os.path.exists(out_path):
            try:
                if os.path.getsize(out_path) == 0:
                    os.remove(out_path)
            except OSError:
                pass
        return None, None, None

    log.info("usb pipeline start %dx%d@%d br=%d dev=%s fmt=%s enc=%s",
             WIDTH, HEIGHT, FPS, BITRATE, USB_DEVICE, USB_INPUT_FORMAT, USB_ENCODER)
    return proc, None, out_path


def _start_raw_pipeline():
    """rpicam-raw → сирий Bayer прямо у файл, БЕЗ ffmpeg (headerless потік,
    муксити нема що). Повертає (cam, None, out_path) або (None, None, None).

    Поряд пише rec_….json — сайдкар з метаданими сенсора, без якого потік
    неможливо інтерпретувати (rpicam-raw не додає жодного заголовка)."""
    out_path = os.path.join(REC_DIR, time.strftime("rec_%Y%m%d_%H%M%S.raw"))
    cam_cmd = [
        "rpicam-raw", "-t", "0", "-n",
        "--width", str(WIDTH), "--height", str(HEIGHT),
        "--framerate", str(RAW_FPS),
    ]
    if SENSOR_MODE:
        cam_cmd += ["--mode", SENSOR_MODE]
    if CAMERA is None or CAMERA["has_autofocus"]:
        cam_cmd += ["--autofocus-mode", AUTOFOCUS_MODE]
        if AUTOFOCUS_MODE == "manual":
            cam_cmd += ["--lens-position", LENS_POSITION]
    cam_cmd += ["-o", out_path]

    # Сайдкар пишемо ДО старту камери — краще осиротілий json без raw, ніж
    # raw без метаданих.
    _write_raw_sidecar(out_path)

    cam = subprocess.Popen(cam_cmd, stderr=subprocess.DEVNULL)

    deadline = time.monotonic() + PIPELINE_START_TIMEOUT
    while time.monotonic() < deadline:
        if cam.poll() is not None:
            break
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            break
        time.sleep(0.1)
    if cam.poll() is not None:
        log.error("rpicam-raw exited at start with code %s", cam.returncode)
        for p in (out_path, _sidecar_path(out_path)):
            try:
                if os.path.exists(p) and (p.endswith(".json") or os.path.getsize(p) == 0):
                    os.remove(p)
            except OSError:
                pass
        return None, None, None

    log.info("raw pipeline start %dx%d@%d sensor=%s",
             WIDTH, HEIGHT, RAW_FPS, CAMERA["sensor"] if CAMERA else "unknown")
    return cam, None, out_path


def _write_raw_sidecar(out_path):
    meta = {
        "width": WIDTH,
        "height": HEIGHT,
        "fps": RAW_FPS,
        "sensor": CAMERA["sensor"] if CAMERA else None,
        "sensor_mode": SENSOR_MODE or "auto",
        "bit_depth": CAMERA.get("bit_depth") if CAMERA else None,
        "bayer_order": CAMERA.get("bayer_order") if CAMERA else None,
        "format": "raw Bayer, headerless, one frame after another",
        "note": (
            "Заявлений формат сенсора з --list-cameras. Реальний рядок може "
            "бути CSI2P-packed — перевірити на залізі перед парсингом."
        ),
    }
    try:
        with open(_sidecar_path(out_path), "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning("could not write raw sidecar: %s", e)


def _space_watchdog_loop(stop_event):
    """Тільки для raw: нижче FREE_MB_MIN зупиняємо запис і зберігаємо зняте.
    Крутиться в окремому треді, stop_recording сам бере lock."""
    while not stop_event.wait(SYNC_INTERVAL_SEC if SYNC_INTERVAL_SEC > 0 else 3):
        try:
            st = os.statvfs(REC_DIR)
            free_mb = st.f_bavail * st.f_frsize / (1024 * 1024)
        except OSError as e:
            log.warning("statvfs failed: %s", e)
            continue
        if free_mb < FREE_MB_MIN:
            log.warning("low space (%.0f MB < %d MB), stopping raw recording",
                        free_mb, FREE_MB_MIN)
            stop_recording(reason="low_space")
            return


def _rotator_loop(stop_event):
    while not stop_event.wait(30):
        rotate_old_files()


def _sync_loop(stop_event):
    # os.sync() глобальний, але цінніше — обмежити втрату при power-cut до
    # SYNC_INTERVAL_SEC секунд відео.
    while not stop_event.wait(SYNC_INTERVAL_SEC):
        try:
            os.sync()
        except OSError as e:
            log.warning("sync failed: %s", e)


def start_recording(raw=False):
    with lock:
        if state["recording"]:
            return False
        rotate_old_files()
        # USB ігнорує raw (rpicam-raw до UVC не застосовний) — завжди H.264,
        # тож і watchdog вільного місця (нижче, if raw) не потрібен.
        if USE_USB:
            raw = False
            cam, ff, out_path = _start_usb_pipeline()
        elif raw:
            cam, ff, out_path = _start_raw_pipeline()
        else:
            cam, ff, out_path = _start_pipeline()
        if cam is None:
            log.warning("REC start failed (raw=%s)", raw)
            return False
        stop_event = threading.Event()
        rot_t = threading.Thread(target=_rotator_loop, args=(stop_event,), daemon=True)
        rot_t.start()
        sync_t = None
        if SYNC_INTERVAL_SEC > 0:
            sync_t = threading.Thread(target=_sync_loop, args=(stop_event,), daemon=True)
            sync_t.start()
        # Watchdog вільного місця — лише для raw (H.264 сам ротується кільцем).
        space_t = None
        if raw:
            space_t = threading.Thread(
                target=_space_watchdog_loop, args=(stop_event,), daemon=True)
            space_t.start()
        state.update(
            recording=True, cam=cam, ff=ff, stop_event=stop_event,
            rot_thread=rot_t, sync_thread=sync_t, out_path=out_path,
            stopping=False, raw=raw, space_thread=space_t,
            last_stop_reason=None, started_at=time.time(),
        )
        _persist_state(True, raw)
        log.info("REC start raw=%s segments=%ds sync=%ds", raw, SEGMENT_SEC, SYNC_INTERVAL_SEC)
        return True


def stop_recording(reason="manual"):
    with lock:
        if not state["recording"]:
            return False
        if state["stopping"]:
            return False
        state["stopping"] = True
        cam = state["cam"]
        ff = state["ff"]
        stop_event = state["stop_event"]
        out_path = state["out_path"]
        started_at = state["started_at"]
        raw = state["raw"]
    stopped_at = time.time()
    stop_event.set()
    # Валимо тільки камеру: ffmpeg бачить EOF, дописує moov і виходить сам.
    if cam:
        cam.send_signal(signal.SIGTERM)
        try:
            cam.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cam.kill()
            cam.wait(timeout=5)
    if ff:
        try:
            ff.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg hung after EOF, killing")
            ff.kill()
            ff.wait(timeout=5)
    try:
        os.sync()
    except OSError as e:
        log.warning("final sync failed: %s", e)
    with lock:
        state.update(
            recording=False, cam=None, ff=None, stop_event=None,
            rot_thread=None, sync_thread=None, out_path=None,
            stopping=False, raw=False, space_thread=None,
            last_stop_reason=reason, started_at=None,
        )
    _persist_state(False)
    log.info("REC stop reason=%s", reason)
    # Сегменти (out_path=None) і raw не перевіряємо: час запису на окремий
    # сегмент невідомий, а raw пише фіксований RAW_FPS без енкодера.
    if (out_path and not raw and started_at is not None
            and stopped_at - started_at >= FPS_CHECK_MIN_SEC):
        # Окремий тред: ffprobe на великому файлі — секунди, стоп не має чекати.
        threading.Thread(
            target=_check_recording,
            args=(out_path, FPS, stopped_at - started_at),
            daemon=True,
        ).start()
    return True


def get_status():
    with lock:
        out = state["out_path"]
        filename = os.path.basename(out) if out else None
        # Формат — з реального файлу, не з прапорця запиту (USB ігнорує raw).
        fmt = None
        if filename:
            fmt = "raw" if filename.endswith(".raw") else "mp4"
        elapsed = None
        if state["recording"] and state["started_at"] is not None:
            elapsed = int(time.time() - state["started_at"])
        return {
            "recording": state["recording"],
            "filename": filename,
            "format": fmt,
            "stopping": state["stopping"],
            "raw": state["raw"],
            # USB завжди H.264 — raw там недоступний, UI ховає чекбокс.
            "raw_supported": not USE_USB,
            "elapsed_sec": elapsed,
            "last_stop_reason": state["last_stop_reason"],
        }


def capture_snapshot():
    """Один кадр для перевірки наведення ДО запису. Повертає шлях до JPEG або
    None. Відмовляє під час запису — камера зайнята одним процесом."""
    with lock:
        if state["recording"]:
            log.warning("snapshot rejected: recording in progress")
            return None
    if USE_USB:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "v4l2", "-input_format", USB_INPUT_FORMAT,
            "-video_size", f"{WIDTH}x{HEIGHT}", "-i", USB_DEVICE,
            "-frames:v", "1", SNAPSHOT_PATH,
        ]
    else:
        cmd = [
            "rpicam-jpeg", "-n", "-t", "500",
            "--width", str(WIDTH), "--height", str(HEIGHT),
        ]
        # Той самий режим, що піде в запис — інакше знімок показує інший кадр.
        if SENSOR_MODE:
            cmd += ["--mode", SENSOR_MODE]
        if CAMERA is None or CAMERA["has_autofocus"]:
            cmd += ["--autofocus-mode", AUTOFOCUS_MODE]
            if AUTOFOCUS_MODE == "manual":
                cmd += ["--lens-position", LENS_POSITION]
        cmd += ["-o", SNAPSHOT_PATH]
    try:
        subprocess.run(cmd, check=True, timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError) as e:
        log.warning("snapshot failed: %s", e)
        return None
    if os.path.exists(SNAPSHOT_PATH) and os.path.getsize(SNAPSHOT_PATH) > 0:
        return SNAPSHOT_PATH
    return None
