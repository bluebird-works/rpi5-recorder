# WiFi AP + Web Control (Mode C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third, parallel recorder control mode: on boot the Pi raises its own WiFi AP (`RPiRecorder` / `12345678`), and a browser at the AP's local IP shows recording status with Start/Stop controls.

**Architecture:** `pi/recording_engine.py` is a new, standalone module holding the transport-agnostic recording pipeline (camera/encoder detection, start/stop, rotation, power-safety sync, crash-resume) — copied out of `pi/ble_recorder.py`, not imported from it. `pi/web_recorder.py` is a small Flask app that imports `recording_engine` and exposes it over HTTP. `pi/install_web.sh` configures a `nmcli`-based WiFi AP and installs `rpi5-web-recorder.service`. This mode is chosen at deploy time (run `install_web.sh` instead of `install_ble.sh`) — never both on one Pi.

**Tech Stack:** Python 3.11+ stdlib (subprocess, threading, json), Flask (via apt `python3-flask`), pytest for local test runs, `nmcli`/NetworkManager for the AP, `rpicam-vid` + `ffmpeg` for the recording pipeline (unchanged from existing modes).

**Spec:** `docs/superpowers/specs/2026-08-19-wifi-ap-web-control-design.md`

## Global Constraints

- `pi/ble_recorder.py` is NOT modified, NOT imported from, and does not import the new code. Zero changes to that file in this plan.
- Network layer uses `nmcli` AP profiles (`ipv4.method shared`), never a manually-configured hostapd/dnsmasq stack (no `/etc/dnsmasq.conf`, no standalone `dnsmasq.service`, no `hostapd.conf`). This does NOT forbid the `dnsmasq-base` *package* — `nmcli`'s own `ipv4.method shared` invokes that binary internally as its DHCP helper (see the `dnsmasq-base` bullet below); installing it is required, not a violation of this constraint.
- Mode selection (BLE vs auto-record vs web) happens only at deploy time via which install script is run — no runtime toggle, no two control services active on one Pi simultaneously.
- Default AP gateway/local IP is `10.42.0.1` (NetworkManager's default for `shared` profiles) — this is the address the web UI is reachable at.
- Env var config style matches `autostart.sh`/`ble_recorder.py`: `REC_DIR`, `WIDTH`, `HEIGHT`, `FPS`, `BITRATE`, `ENCODER`, `AUTOFOCUS_MODE`, `LENS_POSITION`, `SEGMENT_SEC`, `MAX_FILES`, `SYNC_INTERVAL_SEC`, plus new `AP_SSID`, `AP_PASSWORD`, `AP_IFACE`, `WEB_PORT`.
- No preset selector and no file table/download/delete/SSID-rename UI in this PR — those are separate specs (see decomposition in the design doc).
- Flask's built-in dev server (`app.run(..., threaded=True)`) is intentionally used instead of a production WSGI server — single client on a closed AP network, no internet exposure.
- Web service binds port 80 without running as root, via `AmbientCapabilities=CAP_NET_BIND_SERVICE` in the systemd unit (same least-privilege pattern as the existing BLE unit).
- No live Raspberry Pi hardware was available to test against while writing this plan (`recorder` and `recorder2` were offline in the Tailscale tailnet) — flag this explicitly in the PR description.
- `install_web.sh` installs `dnsmasq-base` explicitly, not relying on it being pulled in as a `Recommends` of `network-manager` — `ipv4.method shared` needs it for DHCP, and a minimal/headless image with `Install-Recommends "false"` would otherwise let the AP come up with no error while silently handing out no IPs to clients.

---

### Task 1: `recording_engine.py` — camera/encoder detection and state persistence

**Files:**
- Create: `pi/recording_engine.py`
- Create: `pi/tests/conftest.py`
- Create: `pi/tests/test_recording_engine.py`
- Create: `pi/requirements-dev.txt`

**Interfaces:**
- Produces: `recording_engine.REC_DIR: str`, `recording_engine.MAX_FILES: int`, `recording_engine.STATE_FILE: str`, `recording_engine.CAMERA: dict|None`, `recording_engine.state: dict`, `recording_engine.lock: threading.Lock`, `recording_engine._use_hw_encoder(encoder_hint: str, sysfs_root: str = "/sys/class/video4linux") -> bool`, `recording_engine._persist_state(active: bool) -> None`, `recording_engine._load_state() -> dict|None`, `recording_engine._prune_empty() -> None`, `recording_engine.rotate_old_files() -> None`. These are consumed by Task 2 (same module) and Task 3 (`web_recorder.py`).

- [ ] **Step 1: Write `pi/requirements-dev.txt`**

```
flask
pytest
```

- [ ] **Step 2: Write `pi/tests/conftest.py`**

```python
import os
import sys
import tempfile

# pi/ has no __init__.py — put it on sys.path so `import recording_engine`
# and `import web_recorder` work regardless of cwd pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# recording_engine.py creates REC_DIR at import time (os.makedirs). Point it
# at a throwaway dir before any test module imports it, so test runs don't
# create ~/recordings on the dev machine.
os.environ.setdefault("REC_DIR", tempfile.mkdtemp(prefix="rpi5-recorder-tests-"))
```

- [ ] **Step 3: Write the failing tests — `pi/tests/test_recording_engine.py`**

```python
import os
import time

import recording_engine as engine


def test_use_hw_encoder_explicit_hardware():
    assert engine._use_hw_encoder("hardware") is True


def test_use_hw_encoder_explicit_software():
    assert engine._use_hw_encoder("software") is False


def test_use_hw_encoder_auto_detects_bcm2835(tmp_path):
    node = tmp_path / "video11"
    node.mkdir()
    (node / "name").write_text("bcm2835-codec-encode")
    assert engine._use_hw_encoder("auto", sysfs_root=str(tmp_path)) is True


def test_use_hw_encoder_auto_no_hw_node(tmp_path):
    node = tmp_path / "video0"
    node.mkdir()
    (node / "name").write_text("rp1-cfe-csi2_ch0")
    assert engine._use_hw_encoder("auto", sysfs_root=str(tmp_path)) is False


def test_persist_and_load_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "STATE_FILE", str(tmp_path / ".recording_state"))
    engine._persist_state(True)
    assert engine._load_state() == {"active": True}
    engine._persist_state(False)
    assert engine._load_state() is None


def test_load_state_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "STATE_FILE", str(tmp_path / "nope"))
    assert engine._load_state() is None


def test_prune_empty_removes_only_zero_byte_mp4s(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    empty = tmp_path / "rec_20260101_000000.mp4"
    empty.write_bytes(b"")
    full = tmp_path / "rec_20260101_000100.mp4"
    full.write_bytes(b"x" * 10)
    not_mp4 = tmp_path / "notes.txt"
    not_mp4.write_bytes(b"")
    engine._prune_empty()
    assert not empty.exists()
    assert full.exists()
    assert not_mp4.exists()


def test_rotate_old_files_respects_max_files(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "MAX_FILES", 2)
    names = [
        "rec_20260101_000000.mp4",
        "rec_20260101_000100.mp4",
        "rec_20260101_000200.mp4",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"x" * 10)
    engine.rotate_old_files()
    remaining = sorted(os.listdir(tmp_path))
    assert remaining == ["rec_20260101_000100.mp4", "rec_20260101_000200.mp4"]
```

- [ ] **Step 4: Run tests to verify they fail on import**

Run: `cd pi && python3 -m pip install -r requirements-dev.txt && python3 -m pytest tests/test_recording_engine.py -v`
Expected: FAIL/ERROR — `ModuleNotFoundError: No module named 'recording_engine'` (file doesn't exist yet).

- [ ] **Step 5: Write `pi/recording_engine.py`**

```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd pi && python3 -m pytest tests/test_recording_engine.py -v`
Expected: PASS — 8 tests green.

- [ ] **Step 7: Commit**

```bash
git add pi/recording_engine.py pi/tests/conftest.py pi/tests/test_recording_engine.py pi/requirements-dev.txt
git commit -m "recorder: add recording_engine detection and state helpers"
```

---

### Task 2: `recording_engine.py` — pipeline lifecycle (start/stop/status)

**Files:**
- Modify: `pi/recording_engine.py` (append)
- Modify: `pi/tests/test_recording_engine.py` (append)

**Interfaces:**
- Consumes: everything from Task 1 (`_use_hw_encoder`, `_persist_state`, `rotate_old_files`, `state`, `lock`, `CAMERA`, module-level env config).
- Produces: `recording_engine.start_recording() -> bool`, `recording_engine.stop_recording() -> bool`, `recording_engine.get_status() -> dict` (`{"recording": bool, "filename": str|None}`). Consumed by Task 3 (`web_recorder.py`).

- [ ] **Step 1: Write the failing tests — append to `pi/tests/test_recording_engine.py`**

```python
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_engine_state():
    yield
    engine.state.update(
        recording=False, cam=None, ff=None, stop_event=None,
        rot_thread=None, sync_thread=None, out_path=None,
    )


def _mock_popen_alive():
    cam = MagicMock()
    cam.poll.return_value = None
    cam.stdout = MagicMock()
    ff = MagicMock()
    return cam, ff


def test_start_recording_success(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "STATE_FILE", str(tmp_path / ".recording_state"))
    monkeypatch.setattr(engine, "PIPELINE_START_TIMEOUT", 0.05)
    monkeypatch.setattr(engine, "SYNC_INTERVAL_SEC", 0)
    cam, ff = _mock_popen_alive()
    with patch.object(engine.subprocess, "Popen", side_effect=[cam, ff]):
        assert engine.start_recording() is True
    assert engine.state["recording"] is True
    assert engine.state["out_path"] is not None
    assert engine._load_state() == {"active": True}
    engine.state["stop_event"].set()


def test_start_recording_already_recording_returns_false(monkeypatch):
    engine.state.update(recording=True)
    assert engine.start_recording() is False


def test_start_recording_pipeline_failure_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "STATE_FILE", str(tmp_path / ".recording_state"))
    monkeypatch.setattr(engine, "PIPELINE_START_TIMEOUT", 0.05)
    cam = MagicMock()
    cam.poll.return_value = 1
    cam.returncode = 1
    cam.stdout = MagicMock()
    ff = MagicMock()
    ff.wait.return_value = None
    with patch.object(engine.subprocess, "Popen", side_effect=[cam, ff]):
        assert engine.start_recording() is False
    assert engine.state["recording"] is False


def test_stop_recording_when_not_recording_returns_false():
    assert engine.stop_recording() is False


def test_stop_recording_terminates_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "STATE_FILE", str(tmp_path / ".recording_state"))
    cam = MagicMock()
    ff = MagicMock()
    stop_event = engine.threading.Event()
    out_path = str(tmp_path / "rec_test.mp4")
    engine.state.update(
        recording=True, cam=cam, ff=ff, stop_event=stop_event, out_path=out_path,
    )
    assert engine.stop_recording() is True
    cam.send_signal.assert_called_once_with(engine.signal.SIGTERM)
    assert engine.state["recording"] is False
    assert engine.state["out_path"] is None
    assert engine._load_state() is None


def test_get_status_idle():
    engine.state.update(recording=False, out_path=None)
    assert engine.get_status() == {"recording": False, "filename": None}


def test_get_status_recording():
    engine.state.update(
        recording=True, out_path="/rec/rec_20260101_000000.mp4",
    )
    assert engine.get_status() == {
        "recording": True, "filename": "rec_20260101_000000.mp4",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 -m pytest tests/test_recording_engine.py -v`
Expected: FAIL — `AttributeError: module 'recording_engine' has no attribute 'start_recording'` (and similar for `stop_recording`/`get_status`).

- [ ] **Step 3: Append pipeline lifecycle functions to `pi/recording_engine.py`**

```python
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


def start_recording():
    with lock:
        if state["recording"]:
            return False
        rotate_old_files()
        cam, ff, out_path = _start_pipeline()
        if cam is None:
            log.warning("REC start failed")
            return False
        stop_event = threading.Event()
        rot_t = threading.Thread(target=_rotator_loop, args=(stop_event,), daemon=True)
        rot_t.start()
        sync_t = None
        if SYNC_INTERVAL_SEC > 0:
            sync_t = threading.Thread(target=_sync_loop, args=(stop_event,), daemon=True)
            sync_t.start()
        state.update(
            recording=True, cam=cam, ff=ff, stop_event=stop_event,
            rot_thread=rot_t, sync_thread=sync_t, out_path=out_path,
        )
        _persist_state(True)
        log.info("REC start segments=%ds sync=%ds", SEGMENT_SEC, SYNC_INTERVAL_SEC)
        return True


def stop_recording():
    with lock:
        if not state["recording"]:
            return False
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
        )
    _persist_state(False)
    log.info("REC stop")
    return True


def get_status():
    with lock:
        return {
            "recording": state["recording"],
            "filename": (
                os.path.basename(state["out_path"]) if state["out_path"] else None
            ),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 -m pytest tests/test_recording_engine.py -v`
Expected: PASS — all tests green (8 from Task 1 + 7 new).

- [ ] **Step 5: Commit**

```bash
git add pi/recording_engine.py pi/tests/test_recording_engine.py
git commit -m "recorder: add start/stop/status to recording_engine"
```

---

### Task 3: `web_recorder.py` — Flask app

**Files:**
- Create: `pi/web_recorder.py`
- Create: `pi/tests/test_web_recorder.py`

**Interfaces:**
- Consumes: `recording_engine.get_status()`, `recording_engine.start_recording()`, `recording_engine.stop_recording()`, `recording_engine._load_state()` (all from Task 2).
- Produces: `web_recorder.create_app() -> flask.Flask` (consumed by Task 4's systemd unit indirectly via `python3 web_recorder.py`; consumed directly by this task's tests).

- [ ] **Step 1: Write the failing tests — `pi/tests/test_web_recorder.py`**

```python
from unittest.mock import patch

import pytest

import web_recorder


@pytest.fixture
def client():
    app = web_recorder.create_app()
    app.testing = True
    return app.test_client()


def test_index_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"RPi Recorder" in resp.data


def test_status_reports_idle(client):
    with patch.object(
        web_recorder.engine, "get_status",
        return_value={"recording": False, "filename": None},
    ):
        resp = client.get("/api/status")
    assert resp.status_code == 200
    assert resp.get_json() == {"recording": False, "filename": None}


def test_start_calls_engine_and_returns_ok(client):
    with patch.object(
        web_recorder.engine, "start_recording", return_value=True,
    ) as mock_start:
        resp = client.post("/api/start")
    mock_start.assert_called_once()
    assert resp.get_json() == {"ok": True}


def test_start_when_already_recording_returns_not_ok(client):
    with patch.object(web_recorder.engine, "start_recording", return_value=False):
        resp = client.post("/api/start")
    assert resp.get_json() == {"ok": False}


def test_stop_calls_engine_and_returns_ok(client):
    with patch.object(
        web_recorder.engine, "stop_recording", return_value=True,
    ) as mock_stop:
        resp = client.post("/api/stop")
    mock_stop.assert_called_once()
    assert resp.get_json() == {"ok": True}


def test_get_on_start_route_not_allowed(client):
    resp = client.get("/api/start")
    assert resp.status_code == 405
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 -m pytest tests/test_web_recorder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'web_recorder'`.

- [ ] **Step 3: Write `pi/web_recorder.py`**

```python
"""Flask web control for rpi5-recorder Mode C (WiFi AP + web).

Status + Start/Stop only — file table, SSID rename, and raw-data mode are
separate specs (see docs/superpowers/specs/2026-08-19-wifi-ap-web-control-design.md).
"""
import os
import shutil

from flask import Flask, jsonify

import recording_engine as engine

WEB_PORT = int(os.environ.get("WEB_PORT", 80))

INDEX_HTML = """<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RPi Recorder</title>
<style>
  body { font-family: sans-serif; max-width: 480px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.25rem; }
  #status { font-size: 1.1rem; margin: 1rem 0; }
  #status.recording { color: #b00020; font-weight: bold; }
  button { font-size: 1rem; padding: 0.6rem 1.2rem; margin-right: 0.5rem; }
</style>
</head>
<body>
<h1>RPi Recorder</h1>
<div id="status">завантаження…</div>
<button id="start">Старт</button>
<button id="stop">Стоп</button>
<script>
async function refresh() {
  const r = await fetch('/api/status');
  const s = await r.json();
  const el = document.getElementById('status');
  el.className = s.recording ? 'recording' : '';
  el.textContent = s.recording
    ? 'Recording: ' + (s.filename || '…')
    : 'Idle';
  document.getElementById('start').disabled = s.recording;
  document.getElementById('stop').disabled = !s.recording;
}
async function post(path) {
  await fetch(path, {method: 'POST'});
  refresh();
}
document.getElementById('start').addEventListener('click', () => post('/api/start'));
document.getElementById('stop').addEventListener('click', () => post('/api/stop'));
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


def create_app():
    app = Flask(__name__)

    @app.route("/")
    def index():
        return INDEX_HTML

    @app.route("/api/status")
    def status():
        return jsonify(engine.get_status())

    @app.route("/api/start", methods=["POST"])
    def start():
        return jsonify({"ok": engine.start_recording()})

    @app.route("/api/stop", methods=["POST"])
    def stop():
        return jsonify({"ok": engine.stop_recording()})

    return app


if __name__ == "__main__":
    for tool in ("ffmpeg", "rpicam-vid"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found in PATH")

    # Відновлення після падіння/power-cut: якщо на момент старту сервіса
    # лежить state-файл — вважаємо, що запис ішов, і продовжуємо його.
    resumed = engine._load_state()
    if resumed and resumed.get("active"):
        engine.log.info("resuming recording after boot")
        if not engine.start_recording():
            engine.log.warning("resume failed, clearing state")
            engine._persist_state(False)

    create_app().run(host="0.0.0.0", port=WEB_PORT, threaded=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 -m pytest tests/test_web_recorder.py -v`
Expected: PASS — 6 tests green.

- [ ] **Step 5: Run the full test suite**

Run: `cd pi && python3 -m pytest tests/ -v`
Expected: PASS — all tests from Task 1, 2, and 3 green (21 total).

- [ ] **Step 6: Commit**

```bash
git add pi/web_recorder.py pi/tests/test_web_recorder.py
git commit -m "recorder: add Flask web control app"
```

---

### Task 4: `install_web.sh` — WiFi AP + systemd unit

**Files:**
- Create: `pi/install_web.sh`

**Interfaces:**
- Consumes: `pi/recording_engine.py` and `pi/web_recorder.py` (copies them to the deploy dir, same pattern as `install_ble.sh` copying `ble_recorder.py`).
- Produces: nothing consumed by other tasks — this is the terminal deployment step.

- [ ] **Step 1: Write `pi/install_web.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

REC_USER="${REC_USER:-${SUDO_USER:-pi}}"
REC_HOME="$(getent passwd "${REC_USER}" | cut -d: -f6)"
APP_DIR="${REC_HOME}/rpi5-web"
SERVICE_NAME="rpi5-web-recorder.service"

AP_SSID="${AP_SSID:-RPiRecorder}"
AP_PASSWORD="${AP_PASSWORD:-12345678}"
AP_IFACE="${AP_IFACE:-wlan0}"
AP_CON_NAME="rpi-web-ap"

apt-get update
# dnsmasq-base: без нього ipv4.method=shared не роздає DHCP клієнтам AP.
# Це лише Recommends пакета network-manager, не Depends — на мінімальному
# образі з Install-Recommends=false міг би тихо не встановитись, тому тут
# явно.
apt-get install -y ffmpeg rpicam-apps python3-flask network-manager dnsmasq-base

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
chown -R "${REC_USER}:${REC_USER}" "${APP_DIR}"
mkdir -p "${REC_HOME}/recordings"
chown "${REC_USER}:${REC_USER}" "${REC_HOME}/recordings"

if ! nmcli -t -f NAME con show | grep -qx "${AP_CON_NAME}"; then
  nmcli con add type wifi ifname "${AP_IFACE}" con-name "${AP_CON_NAME}" \
    ssid "${AP_SSID}" \
    802-11-wireless.mode ap 802-11-wireless.band bg \
    802-11-wireless-security.key-mgmt wpa-psk \
    802-11-wireless-security.psk "${AP_PASSWORD}" \
    ipv4.method shared connection.autoconnect yes
else
  nmcli con modify "${AP_CON_NAME}" \
    802-11-wireless.ssid "${AP_SSID}" \
    802-11-wireless-security.psk "${AP_PASSWORD}"
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

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

systemctl --no-pager status "${SERVICE_NAME}" || true
echo "web UI: http://10.42.0.1/ (AP ssid=${AP_SSID})"
echo "logs: journalctl -u ${SERVICE_NAME} -f"
```

- [ ] **Step 2: Make it executable and syntax-check**

Run: `chmod +x pi/install_web.sh && bash -n pi/install_web.sh`
Expected: no output (bash -n only reports syntax errors; none expected).

- [ ] **Step 3: Manual review checklist (no live Pi available — document this in the PR)**

Confirm each of these against the spec (`docs/superpowers/specs/2026-08-19-wifi-ap-web-control-design.md`) and this plan's Global Constraints, by reading the script, not by running it:
- `apt-get install` list matches what `web_recorder.py`/`recording_engine.py` actually import (Flask, ffmpeg, rpicam-apps) plus `dnsmasq-base` for AP DHCP (see Global Constraints) — no other extra packages.
- The `NetworkManager` active-check happens before any `nmcli` call, with a clear failure message and non-zero exit.
- The systemd unit's `AmbientCapabilities`/`CapabilityBoundingSet` lines are present so `WEB_PORT=80` works without `User=root`.
- `ExecStartPre` brings up the AP profile with `+` (root), matching how `install_ble.sh` runs `rfkill`/`hciconfig` as root while the service itself runs as `${REC_USER}`.
- Nothing in this script touches `ble_recorder.py`, `install_ble.sh`, `autostart.sh`, or `install_autostart.sh`.

- [ ] **Step 4: Commit**

```bash
git add pi/install_web.sh
git commit -m "recorder: add install_web.sh for WiFi AP + web control deploy"
```

---

## After all tasks

- [ ] Update `CLAUDE.md`'s "Про проєкт" section: add a one-line **C. WiFi AP web control** entry after modes A and B, matching their existing format (file paths + one-sentence description), as a follow-up commit after Task 4.
- [ ] Open the PR with an explicit note: no live Raspberry Pi hardware was available during implementation (`recorder`/`recorder2` offline in Tailscale tailnet) — `nmcli` AP behavior and Flask-on-port-80 need field verification before this ships to a real device.
