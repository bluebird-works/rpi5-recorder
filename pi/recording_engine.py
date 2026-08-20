"""Transport-agnostic recording pipeline for rpi5-recorder Mode C (WiFi AP + web).

Copied out of ble_recorder.py's pipeline logic rather than imported from it —
ble_recorder.py is left untouched by design, see
docs/superpowers/specs/2026-08-19-wifi-ap-web-control-design.md for why.
No BLE-specific pieces here (manual JSON config, presets, snapshot chunking):
this engine always runs a single env-configured recording, same convention
as autostart.sh.
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
}
lock = threading.Lock()


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
    m = re.search(r"^\s*(\d+)\s*:\s*(\S+)\s*\[(\d+)x(\d+)", text, re.M)
    if not m:
        log.warning("could not parse --list-cameras output:\n%s", text)
        return None
    sensor = m.group(2).lower()
    return {
        "index": int(m.group(1)),
        "sensor": sensor,
        "max_width": int(m.group(3)),
        "max_height": int(m.group(4)),
        "has_autofocus": sensor in AF_CAPABLE_SENSORS,
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


def _persist_state(active):
    if active:
        # Atomic write: temp + rename, захист від torn write при power loss.
        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"active": True}, f)
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
    """Прибирає 0-байтні mp4, що лишились коли ffmpeg відкрив файл, але жоден
    фрагмент не долетів до диска до power-cut (ext4 delayed alloc)."""
    try:
        names = os.listdir(REC_DIR)
    except OSError as e:
        log.warning("prune scan failed: %s", e)
        return
    for name in names:
        if not name.endswith(".mp4"):
            continue
        p = os.path.join(REC_DIR, name)
        try:
            if os.path.getsize(p) == 0:
                os.remove(p)
                log.info("pruned empty %s", name)
        except OSError:
            pass


def rotate_old_files():
    _prune_empty()
    files = sorted(
        (f for f in os.listdir(REC_DIR) if f.endswith(".mp4")),
        reverse=True,
    )
    for old in files[MAX_FILES:]:
        try:
            os.remove(os.path.join(REC_DIR, old))
            log.info("rotated %s", old)
        except OSError as e:
            log.warning("rotate failed for %s: %s", old, e)
