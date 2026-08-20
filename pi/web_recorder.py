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
  const el = document.getElementById('status');
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    el.className = s.recording ? 'recording' : '';
    el.textContent = s.recording
      ? 'Recording: ' + (s.filename || '…')
      : 'Idle';
    document.getElementById('start').disabled = s.recording;
    document.getElementById('stop').disabled = !s.recording;
  } catch (e) {
    el.className = '';
    el.textContent = 'нема звʼязку з рекордером';
    document.getElementById('start').disabled = true;
    document.getElementById('stop').disabled = true;
  }
}
async function post(path) {
  try {
    const r = await fetch(path, {method: 'POST'});
    const body = await r.json();
    if (body.ok) {
      // Успіх — одразу підтягуємо новий статус.
      refresh();
    } else {
      // Провал: лишаємо повідомлення на екрані, найближчий periodic
      // refresh() (setInterval нижче) підмінить його реальним статусом
      // за ~2с — не перетираємо його миттєво власним викликом.
      document.getElementById('status').textContent = 'команда не виконана';
    }
  } catch (e) {
    document.getElementById('status').textContent = 'нема звʼязку з рекордером';
  }
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
