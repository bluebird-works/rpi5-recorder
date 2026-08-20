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
        last_stop_reason=None,
    )
    assert engine.get_status() == {
        "recording": False, "filename": None, "stopping": False,
        "raw": False, "last_stop_reason": None,
    }


def test_get_status_recording():
    engine.state.update(
        recording=True, out_path="/rec/rec_20260101_000000.mp4", stopping=False,
        raw=False, last_stop_reason=None,
    )
    assert engine.get_status() == {
        "recording": True, "filename": "rec_20260101_000000.mp4",
        "stopping": False, "raw": False, "last_stop_reason": None,
    }


def test_get_status_stopping():
    engine.state.update(
        recording=True, out_path="/rec/rec_20260101_000000.mp4", stopping=True,
        raw=False, last_stop_reason=None,
    )
    assert engine.get_status() == {
        "recording": True, "filename": "rec_20260101_000000.mp4",
        "stopping": True, "raw": False, "last_stop_reason": None,
    }


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
