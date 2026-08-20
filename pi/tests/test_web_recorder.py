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
    mock_start.assert_called_once_with(raw=False)
    assert resp.get_json() == {"ok": True}


def test_start_raw_passes_flag_to_engine(client):
    with patch.object(
        web_recorder.engine, "start_recording", return_value=True,
    ) as mock_start:
        resp = client.post("/api/start", json={"raw": True})
    mock_start.assert_called_once_with(raw=True)
    assert resp.get_json() == {"ok": True}


def test_start_when_already_recording_returns_not_ok(client):
    with patch.object(web_recorder.engine, "start_recording", return_value=False):
        resp = client.post("/api/start")
    assert resp.get_json() == {"ok": False}


def test_snapshot_ok_calls_engine(client):
    with patch.object(
        web_recorder.engine, "capture_snapshot", return_value="/rec/.snapshot.jpg",
    ) as mock_snap:
        resp = client.post("/api/snapshot")
    mock_snap.assert_called_once()
    assert resp.get_json() == {"ok": True}


def test_snapshot_busy_returns_409(client):
    with patch.object(web_recorder.engine, "capture_snapshot", return_value=None):
        resp = client.post("/api/snapshot")
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False


def test_snapshot_img_404_when_missing(client):
    with patch.object(web_recorder.os.path, "isfile", return_value=False):
        resp = client.get("/api/snapshot.jpg")
    assert resp.status_code == 404


def test_files_lists_recordings(client):
    rows = [{"name": "rec_20260101_000000.mp4", "size": 10,
             "created": 1, "status": "saved"}]
    with patch.object(web_recorder.engine, "list_recordings", return_value=rows):
        resp = client.get("/api/files")
    assert resp.status_code == 200
    assert resp.get_json() == rows


def test_download_rejects_bad_name(client):
    with patch.object(web_recorder.engine, "_safe_rec_path", return_value=None):
        resp = client.get("/api/files/..%2F..%2Fetc%2Fpasswd/download")
    assert resp.status_code == 404


def test_delete_calls_engine(client):
    with patch.object(
        web_recorder.engine, "delete_recording", return_value=True,
    ) as mock_del:
        resp = client.post("/api/files/rec_20260101_000000.mp4/delete")
    mock_del.assert_called_once_with("rec_20260101_000000.mp4")
    assert resp.get_json() == {"ok": True}


def test_ap_post_valid_writes_spool(client, tmp_path):
    spool = tmp_path / "ap-config.json"
    with patch.object(web_recorder, "AP_SPOOL_FILE", str(spool)):
        resp = client.post("/api/ap", json={"ssid": "NewNet", "password": "secret12"})
    assert resp.get_json() == {"ok": True}
    import json as _json
    assert _json.loads(spool.read_text()) == {"ssid": "NewNet", "password": "secret12"}


def test_ap_post_rejects_short_password(client):
    resp = client.post("/api/ap", json={"ssid": "NewNet", "password": "short"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_ap_post_rejects_long_ssid(client):
    resp = client.post("/api/ap", json={"ssid": "x" * 33, "password": "secret12"})
    assert resp.status_code == 400


def test_ap_get_returns_current(client, tmp_path):
    cur = tmp_path / "ap-current.json"
    cur.write_text('{"ssid": "RPiRecorder", "password": "12345678"}')
    with patch.object(web_recorder, "AP_CURRENT_FILE", str(cur)):
        resp = client.get("/api/ap")
    assert resp.get_json() == {"ssid": "RPiRecorder", "password": "12345678"}


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
