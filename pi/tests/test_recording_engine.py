import os
from unittest.mock import MagicMock, patch

import pytest

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
    assert engine._load_state() == {"active": True, "raw": False}
    engine._persist_state(True, raw=True)
    assert engine._load_state() == {"active": True, "raw": True}
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


@pytest.fixture(autouse=True)
def reset_engine_state():
    yield
    engine.state.update(
        recording=False, cam=None, ff=None, stop_event=None,
        rot_thread=None, sync_thread=None, out_path=None, stopping=False,
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
    assert engine._load_state() == {"active": True, "raw": False}
    engine.state["stop_event"].set()


def test_start_recording_already_recording_returns_false():
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
    engine.state.update(
        recording=False, out_path=None, stopping=False, raw=False,
        last_stop_reason=None, started_at=None,
    )
    s = engine.get_status()
    assert s["recording"] is False
    assert s["filename"] is None
    assert s["format"] is None
    assert s["elapsed_sec"] is None


def test_get_status_recording_reports_format_from_file():
    engine.state.update(
        recording=True, out_path="/rec/rec_20260101_000000.mp4", stopping=False,
        raw=False, last_stop_reason=None, started_at=None,
    )
    s = engine.get_status()
    assert s["filename"] == "rec_20260101_000000.mp4"
    assert s["format"] == "mp4"


def test_get_status_raw_file_reports_raw_format():
    # Навіть якщо прапорець raw загубився — формат береться з файлу.
    engine.state.update(
        recording=True, out_path="/rec/rec_20260101_000000.raw", stopping=False,
        raw=False, last_stop_reason=None, started_at=None,
    )
    assert engine.get_status()["format"] == "raw"


def test_get_status_elapsed_counts_up(monkeypatch):
    monkeypatch.setattr(engine.time, "time", lambda: 1000.0)
    engine.state.update(
        recording=True, out_path="/rec/rec_x.mp4", stopping=False, raw=False,
        last_stop_reason=None, started_at=987.0,
    )
    assert engine.get_status()["elapsed_sec"] == 13


def test_get_status_raw_supported_false_on_usb(monkeypatch):
    monkeypatch.setattr(engine, "USE_USB", True)
    engine.state.update(recording=False, out_path=None, started_at=None)
    assert engine.get_status()["raw_supported"] is False


def test_get_status_stopping():
    engine.state.update(
        recording=True, out_path="/rec/rec_20260101_000000.mp4", stopping=True,
        raw=False, last_stop_reason=None, started_at=None,
    )
    assert engine.get_status()["stopping"] is True


def test_capture_snapshot_rejected_while_recording():
    engine.state.update(recording=True)
    assert engine.capture_snapshot() is None
    engine.state.update(recording=False)


def test_capture_snapshot_usb_argv_and_returns_path(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "USE_USB", True)
    monkeypatch.setattr(engine, "USB_DEVICE", "/dev/video0")
    monkeypatch.setattr(engine, "USB_INPUT_FORMAT", "mjpeg")
    monkeypatch.setattr(engine, "SNAPSHOT_PATH", str(tmp_path / ".snapshot.jpg"))
    engine.state.update(recording=False)

    def fake_run(cmd, **kw):
        assert cmd[0] == "ffmpeg"
        assert "-frames:v" in cmd and "1" in cmd
        assert "/dev/video0" in cmd
        with open(engine.SNAPSHOT_PATH, "wb") as f:
            f.write(b"\xff\xd8jpeg")
        return MagicMock()

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    assert engine.capture_snapshot() == engine.SNAPSHOT_PATH


def test_stop_recording_sets_stopping_flag_during_drain(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "STATE_FILE", str(tmp_path / ".recording_state"))
    cam = MagicMock()
    ff = MagicMock()
    stop_event = engine.threading.Event()
    out_path = str(tmp_path / "rec_test.mp4")
    seen_stopping = {}

    def fake_wait(timeout=None):
        # Captured mid-drain, before stop_recording releases the flag —
        # this is the ~25s window get_status() must report accurately.
        seen_stopping["during_wait"] = engine.state["stopping"]

    cam.wait.side_effect = fake_wait
    engine.state.update(
        recording=True, cam=cam, ff=ff, stop_event=stop_event, out_path=out_path,
        stopping=False,
    )
    assert engine.stop_recording() is True
    assert seen_stopping["during_wait"] is True
    assert engine.state["stopping"] is False


def test_stop_recording_returns_false_if_already_stopping(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "STATE_FILE", str(tmp_path / ".recording_state"))
    cam = MagicMock()
    ff = MagicMock()
    stop_event = engine.threading.Event()
    engine.state.update(
        recording=True, cam=cam, ff=ff, stop_event=stop_event,
        out_path=str(tmp_path / "rec_test.mp4"), stopping=True,
    )
    assert engine.stop_recording() is False
    cam.send_signal.assert_not_called()


def test_start_pipeline_hardware_branch_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "PIPELINE_START_TIMEOUT", 0.05)
    monkeypatch.setattr(engine, "_use_hw_encoder", lambda hint: True)
    cam, ff = _mock_popen_alive()
    with patch.object(
        engine.subprocess, "Popen", side_effect=[cam, ff],
    ) as mock_popen:
        cam_out, ff_out, out_path = engine._start_pipeline()
    assert cam_out is cam
    assert ff_out is ff
    cam_argv, ff_argv = (c.args[0] for c in mock_popen.call_args_list)
    assert "rpicam-vid" in cam_argv
    assert "--codec" in cam_argv and "h264" in cam_argv
    assert "--inline" in cam_argv
    assert "ffmpeg" in ff_argv
    assert "-c" in ff_argv and "copy" in ff_argv


def test_start_pipeline_software_branch_argv_with_segments(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "PIPELINE_START_TIMEOUT", 0.05)
    monkeypatch.setattr(engine, "SEGMENT_SEC", 5)
    monkeypatch.setattr(engine, "_use_hw_encoder", lambda hint: False)
    cam, ff = _mock_popen_alive()
    with patch.object(
        engine.subprocess, "Popen", side_effect=[cam, ff],
    ) as mock_popen:
        cam_out, ff_out, out_path = engine._start_pipeline()
    assert cam_out is cam
    assert ff_out is ff
    cam_argv, ff_argv = (c.args[0] for c in mock_popen.call_args_list)
    assert "rpicam-vid" in cam_argv
    assert "--codec" in cam_argv and "yuv420" in cam_argv
    assert "ffmpeg" in ff_argv
    assert "-f" in ff_argv and "segment" in ff_argv
    assert "-force_key_frames" in ff_argv


# --- Part 4/5: file table, delete, raw ---------------------------------------


def _touch(path, size=10):
    with open(path, "wb") as f:
        f.write(b"x" * size)


def test_list_recordings_reports_status_and_both_exts(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    _touch(tmp_path / "rec_20260101_000000.mp4")
    _touch(tmp_path / "rec_20260101_000100.raw")
    (tmp_path / "rec_20260101_000100.json").write_text("{}")  # sidecar, hidden
    (tmp_path / "notes.txt").write_text("nope")
    engine.state.update(out_path=str(tmp_path / "rec_20260101_000100.raw"))
    rows = engine.list_recordings()
    engine.state.update(out_path=None)
    names = [r["name"] for r in rows]
    assert names == ["rec_20260101_000100.raw", "rec_20260101_000000.mp4"]
    assert "rec_20260101_000100.json" not in names
    assert "notes.txt" not in names
    active = next(r for r in rows if r["name"] == "rec_20260101_000100.raw")
    saved = next(r for r in rows if r["name"] == "rec_20260101_000000.mp4")
    assert active["status"] == "recording"
    assert saved["status"] == "saved"
    assert saved["size"] == 10


def test_safe_rec_path_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    assert engine._safe_rec_path("../../etc/passwd") is None
    assert engine._safe_rec_path("rec_20260101_000000.mp4/../../x") is None
    assert engine._safe_rec_path("evil.sh") is None
    assert engine._safe_rec_path("rec_bad.mp4") is None
    _touch(tmp_path / "rec_20260101_000000.mp4")
    ok = engine._safe_rec_path("rec_20260101_000000.mp4")
    assert ok == os.path.realpath(str(tmp_path / "rec_20260101_000000.mp4"))


def test_delete_recording_removes_file_and_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    engine.state.update(out_path=None)
    _touch(tmp_path / "rec_20260101_000000.raw")
    (tmp_path / "rec_20260101_000000.json").write_text("{}")
    assert engine.delete_recording("rec_20260101_000000.raw") is True
    assert not (tmp_path / "rec_20260101_000000.raw").exists()
    assert not (tmp_path / "rec_20260101_000000.json").exists()


def test_delete_recording_refuses_active(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    _touch(tmp_path / "rec_20260101_000000.mp4")
    engine.state.update(out_path=str(tmp_path / "rec_20260101_000000.mp4"))
    assert engine.delete_recording("rec_20260101_000000.mp4") is False
    engine.state.update(out_path=None)
    assert (tmp_path / "rec_20260101_000000.mp4").exists()


def test_delete_recording_rejects_bad_name(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    assert engine.delete_recording("../../etc/passwd") is False


def test_start_raw_pipeline_argv_and_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "PIPELINE_START_TIMEOUT", 0.05)
    monkeypatch.setattr(engine, "RAW_FPS", 10)
    monkeypatch.setattr(
        engine, "CAMERA",
        {"sensor": "imx708", "has_autofocus": True, "bit_depth": 10,
         "bayer_order": "RGGB", "max_width": 4608, "max_height": 2592},
    )
    cam = MagicMock()
    cam.poll.return_value = None
    with patch.object(engine.subprocess, "Popen", return_value=cam) as mock_popen:
        cam_out, ff_out, out_path = engine._start_raw_pipeline()
    assert cam_out is cam
    assert ff_out is None
    assert out_path.endswith(".raw")
    cam_argv = mock_popen.call_args_list[0].args[0]
    assert "rpicam-raw" in cam_argv
    assert "--framerate" in cam_argv and "10" in cam_argv
    sidecar = engine._sidecar_path(out_path)
    assert os.path.exists(sidecar)
    import json as _json
    meta = _json.loads(open(sidecar).read())
    assert meta["sensor"] == "imx708"
    assert meta["bit_depth"] == 10
    assert meta["fps"] == 10


def test_start_usb_pipeline_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "PIPELINE_START_TIMEOUT", 0.05)
    monkeypatch.setattr(engine, "WIDTH", 1920)
    monkeypatch.setattr(engine, "HEIGHT", 1080)
    monkeypatch.setattr(engine, "FPS", 30)
    monkeypatch.setattr(engine, "USB_DEVICE", "/dev/video0")
    monkeypatch.setattr(engine, "USB_INPUT_FORMAT", "mjpeg")
    monkeypatch.setattr(engine, "USB_ENCODER", "libx264")
    proc = MagicMock()
    proc.poll.return_value = None
    with patch.object(engine.subprocess, "Popen", return_value=proc) as mock_popen:
        cam_out, ff_out, out_path = engine._start_usb_pipeline()
    assert cam_out is proc
    assert ff_out is None
    assert out_path.endswith(".mp4")
    argv = mock_popen.call_args_list[0].args[0]
    assert argv[0] == "ffmpeg"
    assert "-f" in argv and "v4l2" in argv
    assert "-input_format" in argv and "mjpeg" in argv
    assert "/dev/video0" in argv
    assert "libx264" in argv
    assert "1920x1080" in argv


def test_start_usb_pipeline_copy_encoder(tmp_path, monkeypatch):
    # copy-режим (для слабких Pi): нативний потік, без -b:v/-pix_fmt.
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "PIPELINE_START_TIMEOUT", 0.05)
    monkeypatch.setattr(engine, "USB_ENCODER", "copy")
    proc = MagicMock()
    proc.poll.return_value = None
    with patch.object(engine.subprocess, "Popen", return_value=proc) as mock_popen:
        engine._start_usb_pipeline()
    argv = mock_popen.call_args_list[0].args[0]
    assert "-c" in argv and "copy" in argv
    assert "libx264" not in argv
    assert "-b:v" not in argv


def test_use_usb_defaults_false(monkeypatch):
    # Дефолт CAMERA_SRC=csi → USB вимкнено, CSI-шлях і тести не зачеплені.
    assert engine.USE_USB is False


def test_space_watchdog_stops_on_low_space(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "FREE_MB_MIN", 500)
    monkeypatch.setattr(engine, "SYNC_INTERVAL_SEC", 0)
    calls = []
    monkeypatch.setattr(engine, "stop_recording", lambda reason: calls.append(reason))

    class FakeVfs:
        f_bavail = 10
        f_frsize = 1024 * 1024  # 10 MB free, below 500

    monkeypatch.setattr(engine.os, "statvfs", lambda p: FakeVfs())
    ev = engine.threading.Event()
    engine._space_watchdog_loop(ev)
    assert calls == ["low_space"]


# --- Sensor modes / resolution / FoV selection -------------------------------

LIST_CAMERAS_IMX708 = """Available cameras
-----------------
0 : imx708 [4608x2592 10-bit RGGB] (/base/soc/i2c0mux/i2c@88000/imx708@1a)
    Modes: 'SRGGB10_CSI2P' : 1536x864 [120.13 fps - (768, 432)/3072x1728 crop]
                             2304x1296 [56.03 fps - (0, 0)/4608x2592 crop]
                             4608x2592 [14.35 fps - (0, 0)/4608x2592 crop]
"""

LIST_CAMERAS_IMX219 = """Available cameras
-----------------
0 : imx219 [3280x2464 10-bit RGGB] (/base/soc/i2c0mux/i2c@7e004000/imx219@10)
    Modes: 'SRGGB10_CSI2P' : 640x480 [206.65 fps - (1000, 752)/1280x960 crop]
                             1640x1232 [41.85 fps - (0, 0)/3280x2464 crop]
                             3280x2464 [21.19 fps - (0, 0)/3280x2464 crop]
"""


def test_parse_modes_imx708_reads_all_three():
    modes = engine._parse_modes(LIST_CAMERAS_IMX708, 4608, 2592)
    assert [(m["width"], m["height"]) for m in modes] == [
        (1536, 864), (2304, 1296), (4608, 2592)
    ]
    assert modes[0]["max_fps"] == 120.13
    assert modes[1]["max_fps"] == 56.03


def test_parse_modes_marks_crop_vs_full_fov():
    modes = engine._parse_modes(LIST_CAMERAS_IMX708, 4608, 2592)
    crop, full, native = modes
    # 3072x1728 з 4608x2592 — вужчий кадр, це і є «кроп FoV».
    assert crop["full_fov"] is False
    assert round(crop["fov_ratio"], 3) == 0.667
    assert full["full_fov"] is True
    assert full["fov_ratio"] == 1.0
    assert native["full_fov"] is True


def test_parse_modes_works_for_non_imx708_sensor():
    modes = engine._parse_modes(LIST_CAMERAS_IMX219, 3280, 2464)
    assert [(m["width"], m["height"]) for m in modes] == [
        (640, 480), (1640, 1232), (3280, 2464)
    ]
    assert modes[0]["full_fov"] is False
    assert modes[1]["full_fov"] is True


def test_parse_modes_empty_when_no_modes_block():
    assert engine._parse_modes("0 : imx708 [4608x2592 10-bit RGGB]", 4608, 2592) == []


def test_detect_camera_includes_modes(monkeypatch):
    monkeypatch.setattr(
        engine.subprocess, "run",
        lambda *a, **kw: MagicMock(stdout=LIST_CAMERAS_IMX708, stderr=""),
    )
    cam = engine._detect_camera()
    assert cam["sensor"] == "imx708"
    assert len(cam["modes"]) == 3
    assert cam["modes"][1]["key"] == "2304:1296"


def test_use_hw_encoder_forced_software_above_1920(monkeypatch):
    # HW-енкодер упирається в 1920 по ширині — вище мусить бути software,
    # інакше rpicam тихо ріже кадр до 1920.
    monkeypatch.setattr(engine, "WIDTH", 2304)
    assert engine._use_hw_encoder("hardware") is False
    monkeypatch.setattr(engine, "WIDTH", 1920)
    assert engine._use_hw_encoder("hardware") is True


def _fake_camera():
    return {
        "index": 0, "sensor": "imx708", "max_width": 4608, "max_height": 2592,
        "has_autofocus": True, "bit_depth": 10, "bayer_order": "RGGB",
        "modes": engine._parse_modes(LIST_CAMERAS_IMX708, 4608, 2592),
    }


def test_resolutions_for_mode_filters_by_size_and_aspect(monkeypatch):
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    res = engine._resolutions_for(engine.CAMERA["modes"][1])  # 2304x1296, 16:9
    pairs = [(r["width"], r["height"]) for r in res]
    assert (1280, 720) in pairs
    assert (1920, 1080) in pairs
    assert (2304, 1296) in pairs           # native режиму завжди в списку
    assert all(w <= 2304 and h <= 1296 for w, h in pairs)
    assert (640, 480) not in pairs         # 4:3 не той aspect
    hw = {(r["width"], r["height"]): r["hw"] for r in res}
    assert hw[(1920, 1080)] is True
    assert hw[(2304, 1296)] is False       # вище стелі HW-енкодера


def test_camera_info_lists_modes_and_current_settings(monkeypatch):
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    monkeypatch.setattr(engine, "USE_USB", False)
    info = engine.camera_info()
    assert info["src"] == "csi"
    assert info["sensor"] == "imx708"
    assert info["modes"][0]["key"] == "auto"     # автовибір першим
    assert [m["key"] for m in info["modes"][1:]] == [
        "1536:864", "2304:1296", "4608:2592"
    ]
    assert info["settings"]["width"] == engine.WIDTH


def test_camera_info_usb_has_no_modes(monkeypatch):
    monkeypatch.setattr(engine, "USE_USB", True)
    info = engine.camera_info()
    assert info["src"] == "usb"
    assert info["modes"] == []
    assert len(info["resolutions"]) > 0


def test_apply_settings_updates_globals(monkeypatch):
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    monkeypatch.setattr(engine, "SETTINGS_FILE", "/dev/null")
    engine.state.update(recording=False)
    ok, err = engine.apply_settings(
        {"mode": "2304:1296", "width": 1920, "height": 1080, "fps": 30})
    assert (ok, err) == (True, None)
    assert engine.SENSOR_MODE == "2304:1296"
    assert (engine.WIDTH, engine.HEIGHT, engine.FPS) == (1920, 1080, 30)


def test_apply_settings_rejected_while_recording(monkeypatch):
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    engine.state.update(recording=True)
    ok, err = engine.apply_settings({"mode": "auto", "width": 1280, "height": 720})
    engine.state.update(recording=False)
    assert ok is False
    assert "запис" in err


def test_apply_settings_rejects_unknown_mode(monkeypatch):
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    engine.state.update(recording=False)
    ok, err = engine.apply_settings({"mode": "9999:9999", "width": 1280, "height": 720})
    assert ok is False


def test_apply_settings_rejects_resolution_above_mode(monkeypatch):
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    engine.state.update(recording=False)
    ok, err = engine.apply_settings(
        {"mode": "1536:864", "width": 1920, "height": 1080, "fps": 30})
    assert ok is False


def test_apply_settings_rejects_fps_above_mode_ceiling(monkeypatch):
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    engine.state.update(recording=False)
    ok, err = engine.apply_settings(
        {"mode": "2304:1296", "width": 1920, "height": 1080, "fps": 90})
    assert ok is False
    assert "fps" in err.lower()


def test_apply_settings_rejects_mismatched_aspect(monkeypatch):
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    engine.state.update(recording=False)
    ok, err = engine.apply_settings(
        {"mode": "2304:1296", "width": 640, "height": 480, "fps": 30})
    assert ok is False


def test_settings_round_trip_through_file(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    monkeypatch.setattr(engine, "SETTINGS_FILE", str(tmp_path / ".settings.json"))
    engine.state.update(recording=False)
    assert engine.apply_settings(
        {"mode": "1536:864", "width": 1536, "height": 864, "fps": 120})[0] is True
    assert engine._load_settings() == {
        "mode": "1536:864", "width": 1536, "height": 864, "fps": 120}


def test_start_pipeline_passes_sensor_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "PIPELINE_START_TIMEOUT", 0.05)
    monkeypatch.setattr(engine, "SENSOR_MODE", "2304:1296")
    monkeypatch.setattr(engine, "_use_hw_encoder", lambda hint: False)
    cam, ff = _mock_popen_alive()
    with patch.object(engine.subprocess, "Popen", side_effect=[cam, ff]) as mock_popen:
        engine._start_pipeline()
    cam_argv = mock_popen.call_args_list[0].args[0]
    assert "--mode" in cam_argv
    assert cam_argv[cam_argv.index("--mode") + 1] == "2304:1296"


def test_start_pipeline_omits_mode_when_auto(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "PIPELINE_START_TIMEOUT", 0.05)
    monkeypatch.setattr(engine, "SENSOR_MODE", "")
    monkeypatch.setattr(engine, "_use_hw_encoder", lambda hint: False)
    cam, ff = _mock_popen_alive()
    with patch.object(engine.subprocess, "Popen", side_effect=[cam, ff]) as mock_popen:
        engine._start_pipeline()
    assert "--mode" not in mock_popen.call_args_list[0].args[0]


def test_snapshot_uses_same_sensor_mode(tmp_path, monkeypatch):
    # Інакше «перевірити кадр» показував би не той FoV, що піде в запис.
    monkeypatch.setattr(engine, "USE_USB", False)
    monkeypatch.setattr(engine, "SENSOR_MODE", "1536:864")
    monkeypatch.setattr(engine, "SNAPSHOT_PATH", str(tmp_path / ".snapshot.jpg"))
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    engine.state.update(recording=False)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        with open(engine.SNAPSHOT_PATH, "wb") as f:
            f.write(b"\xff\xd8jpeg")
        return MagicMock()

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    engine.capture_snapshot()
    assert "--mode" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--mode") + 1] == "1536:864"


def test_start_raw_pipeline_passes_sensor_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "PIPELINE_START_TIMEOUT", 0.05)
    monkeypatch.setattr(engine, "SENSOR_MODE", "4608:2592")
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    cam = MagicMock()
    cam.poll.return_value = None
    with patch.object(engine.subprocess, "Popen", return_value=cam) as mock_popen:
        engine._start_raw_pipeline()
    argv = mock_popen.call_args_list[0].args[0]
    assert "--mode" in argv and "4608:2592" in argv


# --- Стеля fps на Pi 4 і перевірка реального fps після запису -----------------


def test_fps_cap_pi4_by_resolution(monkeypatch):
    monkeypatch.setattr(engine, "FPS_CAPPED", True)
    monkeypatch.setattr(engine, "USE_USB", False)
    assert engine._fps_cap(1280, 720, 120.13) == 60
    assert engine._fps_cap(1536, 864, 120.13) == 30
    assert engine._fps_cap(1920, 1080, 56.03) == 30
    assert engine._fps_cap(1280, 720, 14.35) == 14   # режим нижчий за стелю


def test_fps_cap_absent_on_pi5_and_usb(monkeypatch):
    monkeypatch.setattr(engine, "USE_USB", False)
    monkeypatch.setattr(engine, "FPS_CAPPED", False)
    assert engine._fps_cap(1536, 864, 120.13) == 120
    monkeypatch.setattr(engine, "FPS_CAPPED", True)
    monkeypatch.setattr(engine, "USE_USB", True)
    assert engine._fps_cap(1536, 864, 120.13) == 120


def test_resolutions_for_reports_max_fps(monkeypatch):
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    monkeypatch.setattr(engine, "USE_USB", False)
    monkeypatch.setattr(engine, "FPS_CAPPED", True)
    res = engine._resolutions_for(engine.CAMERA["modes"][0])  # 1536x864 @120
    fps = {(r["width"], r["height"]): r["max_fps"] for r in res}
    assert fps[(1280, 720)] == 60
    assert fps[(1536, 864)] == 30


def test_apply_settings_rejects_fps_above_pi4_cap(monkeypatch):
    monkeypatch.setattr(engine, "CAMERA", _fake_camera())
    monkeypatch.setattr(engine, "USE_USB", False)
    monkeypatch.setattr(engine, "FPS_CAPPED", True)
    monkeypatch.setattr(engine, "SETTINGS_FILE", "/dev/null")
    engine.state.update(recording=False)
    ok, err = engine.apply_settings(
        {"mode": "1536:864", "width": 1280, "height": 720, "fps": 120})
    assert ok is False
    assert "fps" in err.lower()
    ok, err = engine.apply_settings(
        {"mode": "1536:864", "width": 1280, "height": 720, "fps": 60})
    assert (ok, err) == (True, None)


def test_check_recording_flags_dropped_frames(tmp_path, monkeypatch):
    rec = tmp_path / "rec_20260915_162047.mp4"
    _touch(rec)
    monkeypatch.setattr(engine, "_count_video_frames", lambda p: 1400)
    engine._check_recording(str(rec), 120, 20.0)
    assert engine._load_fps_check(str(rec)) == {
        "requested_fps": 120, "real_fps": 70.0, "ok": False}


def test_check_recording_ok_when_fps_held(tmp_path, monkeypatch):
    rec = tmp_path / "rec_20260915_161825.mp4"
    _touch(rec)
    monkeypatch.setattr(engine, "_count_video_frames", lambda p: 600)
    engine._check_recording(str(rec), 30, 20.0)
    assert engine._load_fps_check(str(rec))["ok"] is True


def test_count_video_frames_parses_ffprobe(monkeypatch):
    done = MagicMock(stdout="1257\n")
    with patch.object(engine.subprocess, "run", return_value=done) as run:
        assert engine._count_video_frames("/x/rec.mp4") == 1257
    assert run.call_args[0][0][0] == "ffprobe"


def test_stop_recording_schedules_fps_check(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "STATE_FILE", str(tmp_path / ".recording_state"))
    monkeypatch.setattr(engine, "FPS", 60)
    out_path = str(tmp_path / "rec_test.mp4")
    engine.state.update(
        recording=True, cam=MagicMock(), ff=MagicMock(),
        stop_event=engine.threading.Event(), out_path=out_path,
        started_at=engine.time.time() - 20, raw=False,
    )
    with patch.object(engine.threading, "Thread") as thread:
        assert engine.stop_recording() is True
    thread.assert_called_once()
    kwargs = thread.call_args.kwargs
    assert kwargs["target"] is engine._check_recording
    path, fps, wall = kwargs["args"]
    assert (path, fps) == (out_path, 60)
    assert wall >= 20


def test_stop_recording_skips_check_for_short_or_segmented(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "STATE_FILE", str(tmp_path / ".recording_state"))
    for out_path, age in ((str(tmp_path / "rec_a.mp4"), 2), (None, 60)):
        engine.state.update(
            recording=True, cam=MagicMock(), ff=MagicMock(),
            stop_event=engine.threading.Event(), out_path=out_path,
            started_at=engine.time.time() - age, raw=False,
        )
        with patch.object(engine.threading, "Thread") as thread:
            assert engine.stop_recording() is True
        thread.assert_not_called()


def test_list_recordings_includes_fps_check(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    _touch(tmp_path / "rec_20260915_162047.mp4")
    (tmp_path / "rec_20260915_162047.json").write_text(
        '{"requested_fps": 120, "frames": 1400, "wall_sec": 20.0,'
        ' "real_fps": 70.0, "ok": false}')
    _touch(tmp_path / "rec_20260915_161825.mp4")
    rows = {r["name"]: r for r in engine.list_recordings()}
    assert rows["rec_20260915_162047.mp4"]["fps_check"] == {
        "requested_fps": 120, "real_fps": 70.0, "ok": False}
    assert rows["rec_20260915_161825.mp4"]["fps_check"] is None


def test_delete_mp4_removes_fps_check_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REC_DIR", str(tmp_path))
    _touch(tmp_path / "rec_20260915_162047.mp4")
    (tmp_path / "rec_20260915_162047.json").write_text("{}")
    engine.state.update(out_path=None)
    assert engine.delete_recording("rec_20260915_162047.mp4") is True
    assert not (tmp_path / "rec_20260915_162047.json").exists()
