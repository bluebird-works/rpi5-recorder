"""Transport-agnostic recording pipeline for rpi5-recorder Mode C (WiFi AP + web).

Copied out of ble_recorder.py's pipeline logic rather than imported from it —
ble_recorder.py is left untouched by design, see
docs/superpowers/specs/2026-08-19-wifi-ap-web-control-design.md for why.
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
USE_USB = CAMERA_SRC == "usb"
# Нижче цього порогу вільного місця raw-запис зупиняється сам і зберігає вже
# зняте. H.264 натомість іде кільцевою ротацією (rotate_old_files), бо його
# бітрейт передбачуваний — raw же може прибити карту за хвилини.
FREE_MB_MIN = int(os.environ.get("FREE_MB_MIN", 500))
# Обидва розширення, які ми вважаємо записами. Ротація/список/видалення
# працюють по цьому набору.
REC_EXTS = (".mp4", ".raw")

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
    "last_stop_reason": None,
}
lock = threading.Lock()

# Ім'я запису: rec_YYYYMMDD_HHMMSS.mp4 / .raw. Єдиний легальний шаблон —
# ним же валідуємо download/delete проти path traversal.
REC_NAME_RE = re.compile(r"^rec_\d{8}_\d{6}\.(?:mp4|raw)$")


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
    }


CAMERA = _detect_camera()
if CAMERA:
    log.info(
        "camera: %s max=%dx%d autofocus=%s",
        CAMERA["sensor"], CAMERA["max_width"], CAMERA["max_height"],
        CAMERA["has_autofocus"],
    )
else:
    log.warning("no CSI camera detected — рекордер стартує, але запис впаде")


def _use_hw_encoder(encoder_hint, sysfs_root="/sys/class/video4linux"):
    """Pi 4 має апаратний H.264 (bcm2835-codec), Pi 5 — ні, там кодує CPU."""
    if encoder_hint != "auto":
        return encoder_hint == "hardware"
    return any(
        p.read_text().strip() == "bcm2835-codec-encode"
        for p in pathlib.Path(sysfs_root).glob("*/name")
    )


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
    """Raw-запис rec_….raw має сусідній rec_….json з метаданими сенсора —
    без нього headerless Bayer-потік нечитабельний."""
    return os.path.splitext(rec_path)[0] + ".json"


def _remove_sidecar(rec_path):
    if not rec_path.endswith(".raw"):
        return
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
        })
    out.sort(key=lambda r: r["name"], reverse=True)
    return out


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
        "-c:v", "libx264", "-preset", "ultrafast",
        "-b:v", str(BITRATE), "-pix_fmt", "yuv420p",
    ]
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

    log.info("usb pipeline start %dx%d@%d br=%d dev=%s fmt=%s",
             WIDTH, HEIGHT, FPS, BITRATE, USB_DEVICE, USB_INPUT_FORMAT)
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
            last_stop_reason=None,
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
            last_stop_reason=reason,
        )
    _persist_state(False)
    log.info("REC stop reason=%s", reason)
    return True


def get_status():
    with lock:
        return {
            "recording": state["recording"],
            "filename": (
                os.path.basename(state["out_path"]) if state["out_path"] else None
            ),
            "stopping": state["stopping"],
            "raw": state["raw"],
            "last_stop_reason": state["last_stop_reason"],
        }
