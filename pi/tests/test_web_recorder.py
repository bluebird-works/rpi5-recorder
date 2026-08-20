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
