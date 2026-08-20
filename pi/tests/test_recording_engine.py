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
