"""Flask web control for rpi5-recorder Mode C (WiFi AP + web).

Exposes recording_engine over HTTP: live status + Start/Stop (incl. raw mode),
a table of recordings (download/delete), and AP SSID/password rename.

Privilege model for the rename: this service runs unprivileged (User=REC_USER,
only CAP_NET_BIND_SERVICE). It cannot call `nmcli con modify` itself. Instead
POST /api/ap writes a spool file that a separate root-owned systemd path unit
(ap_apply.sh) picks up, re-validates, and applies. No sudo, no setuid, no
privileges leak into the web process.
"""
import json
import os
import re
import shutil

from flask import Flask, jsonify, request, send_file

import recording_engine as engine

WEB_PORT = int(os.environ.get("WEB_PORT", 80))
# Поточний стан AP (SSID/пароль), пише root-вотчер, читає веб. Дефолт — поряд
# з кодом у деплой-теці.
AP_CURRENT_FILE = os.environ.get(
    "AP_CURRENT_FILE", os.path.expanduser("~/rpi5-web/ap-current.json"))
# Spool: сюди веб кладе бажаний конфіг, звідси root-вотчер його забирає.
AP_SPOOL_FILE = os.environ.get("AP_SPOOL_FILE", "/var/lib/rpi5-web/ap-config.json")

# WPA2-PSK: 8..63 друкованих ASCII. SSID: 1..32 байти. Валідація дублює те, що
# root-вотчер робить ще раз — spool пише непривілейований процес, довіри нема.
SSID_RE = re.compile(r"^[\x20-\x7e]{1,32}$")
PSK_RE = re.compile(r"^[\x20-\x7e]{8,63}$")

INDEX_HTML = """<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RPi Recorder</title>
<style>
  body { font-family: sans-serif; max-width: 680px; margin: 1.5rem auto; padding: 0 1rem; }
  h1 { font-size: 1.25rem; }
  h2 { font-size: 1rem; margin-top: 1.6rem; }
  #status { font-size: 1.1rem; margin: 1rem 0; }
  #status.recording { color: #b00020; font-weight: bold; }
  button { font-size: 1rem; padding: 0.5rem 1rem; margin-right: 0.4rem; }
  label.raw { font-size: 0.95rem; margin-left: 0.3rem; }
  table { border-collapse: collapse; width: 100%; margin-top: 0.5rem; font-size: 0.9rem; }
  th, td { border: 1px solid #ccc; padding: 0.35rem 0.5rem; text-align: left; }
  th { background: #f2f2f2; }
  td.rec { color: #b00020; font-weight: bold; }
  .act a, .act button { font-size: 0.85rem; margin-right: 0.3rem; }
  form.ap input { font-size: 1rem; padding: 0.35rem; margin: 0.2rem 0; width: 14rem; }
  #apmsg, #tablemsg { font-size: 0.9rem; color: #444; }
  dialog { padding: 1.2rem; max-width: 22rem; }
  dialog button { margin-top: 0.8rem; }
</style>
</head>
<body>
<h1>RPi Recorder</h1>

<div id="status">завантаження…</div>
<button id="start">Старт</button>
<button id="stop">Стоп</button>
<label class="raw"><input type="checkbox" id="raw"> raw-data</label>

<h2>Записи</h2>
<div id="tablemsg"></div>
<table id="files">
  <thead><tr>
    <th>Назва</th><th>Розмір</th><th>Дата</th><th>Статус</th><th>Дії</th>
  </tr></thead>
  <tbody></tbody>
</table>

<h2>Точка доступу</h2>
<form class="ap" id="apform">
  <div><input id="ssid" placeholder="SSID" maxlength="32" required></div>
  <div><input id="psk" placeholder="Пароль (8–63)" minlength="8" maxlength="63" required></div>
  <button type="submit">Зберегти й застосувати</button>
</form>
<div id="apmsg"></div>

<dialog id="confirm">
  <div id="confirmtext"></div>
  <button id="confirmyes">Видалити</button>
  <button id="confirmno">Скасувати</button>
</dialog>

<script>
function fmtSize(n) {
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' ГБ';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' МБ';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' КБ';
  return n + ' Б';
}
function fmtDate(sec) {
  const d = new Date(sec * 1000);
  return d.toLocaleString('uk-UA');
}
async function refresh() {
  const el = document.getElementById('status');
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    el.className = s.recording ? 'recording' : '';
    if (s.recording) {
      el.textContent = (s.raw ? 'Raw recording: ' : 'Recording: ') + (s.filename || '…')
        + (s.stopping ? ' (зупиняється…)' : '');
    } else if (s.last_stop_reason === 'low_space') {
      el.textContent = 'Idle — зупинено: мало місця на диску';
    } else {
      el.textContent = 'Idle';
    }
    document.getElementById('start').disabled = s.recording;
    document.getElementById('stop').disabled = !s.recording;
    document.getElementById('raw').disabled = s.recording;
  } catch (e) {
    el.className = '';
    el.textContent = 'нема звʼязку з рекордером';
    document.getElementById('start').disabled = true;
    document.getElementById('stop').disabled = true;
  }
}
async function refreshFiles() {
  const tb = document.querySelector('#files tbody');
  try {
    const r = await fetch('/api/files');
    const rows = await r.json();
    tb.innerHTML = '';
    for (const f of rows) {
      const tr = document.createElement('tr');
      const isRec = f.status === 'recording';
      const actions = isRec
        ? '—'
        : '<a href="/api/files/' + encodeURIComponent(f.name) + '/download">Download</a>'
          + '<button data-name="' + f.name + '" class="del">Delete</button>';
      tr.innerHTML =
        '<td>' + f.name + '</td>' +
        '<td>' + fmtSize(f.size) + '</td>' +
        '<td>' + fmtDate(f.created) + '</td>' +
        '<td class="' + (isRec ? 'rec' : '') + '">' + (isRec ? 'Recording' : 'Saved') + '</td>' +
        '<td class="act">' + actions + '</td>';
      tb.appendChild(tr);
    }
    for (const b of tb.querySelectorAll('button.del')) {
      b.addEventListener('click', () => askDelete(b.dataset.name));
    }
  } catch (e) {
    document.getElementById('tablemsg').textContent = 'не вдалося завантажити список';
  }
}
let pendingDelete = null;
const dlg = document.getElementById('confirm');
function askDelete(name) {
  pendingDelete = name;
  document.getElementById('confirmtext').textContent = 'Видалити ' + name + '?';
  dlg.showModal();
}
document.getElementById('confirmno').addEventListener('click', () => dlg.close());
document.getElementById('confirmyes').addEventListener('click', async () => {
  dlg.close();
  if (!pendingDelete) return;
  await fetch('/api/files/' + encodeURIComponent(pendingDelete) + '/delete', {method: 'POST'});
  pendingDelete = null;
  refreshFiles();
});
async function startRec() {
  const raw = document.getElementById('raw').checked;
  try {
    const r = await fetch('/api/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({raw}),
    });
    const b = await r.json();
    if (b.ok) { refresh(); refreshFiles(); }
    else document.getElementById('status').textContent = 'команда не виконана';
  } catch (e) {
    document.getElementById('status').textContent = 'нема звʼязку з рекордером';
  }
}
async function stopRec() {
  try {
    await fetch('/api/stop', {method: 'POST'});
    refresh(); refreshFiles();
  } catch (e) {
    document.getElementById('status').textContent = 'нема звʼязку з рекордером';
  }
}
document.getElementById('start').addEventListener('click', startRec);
document.getElementById('stop').addEventListener('click', stopRec);

async function loadAp() {
  try {
    const r = await fetch('/api/ap');
    const a = await r.json();
    document.getElementById('ssid').value = a.ssid || '';
    document.getElementById('psk').value = a.password || '';
  } catch (e) {}
}
document.getElementById('apform').addEventListener('submit', async (e) => {
  e.preventDefault();
  const ssid = document.getElementById('ssid').value;
  const psk = document.getElementById('psk').value;
  const msg = document.getElementById('apmsg');
  if (!confirm('Змінити SSID/пароль? Це РОЗІРВЕ поточне підключення до AP — '
    + 'доведеться перепідключитися до нової мережі "' + ssid + '".')) return;
  try {
    const r = await fetch('/api/ap', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ssid, password: psk}),
    });
    const b = await r.json();
    msg.textContent = b.ok
      ? 'Застосовується. Перепідключися до "' + ssid + '" (пароль ' + psk + ').'
      : ('Помилка: ' + (b.error || 'невалідні дані'));
  } catch (e) {
    msg.textContent = 'нема звʼязку з рекордером';
  }
});

refresh();
refreshFiles();
loadAp();
setInterval(refresh, 2000);
setInterval(refreshFiles, 5000);
</script>
</body>
</html>
"""


def _read_ap_current():
    try:
        with open(AP_CURRENT_FILE) as f:
            data = json.load(f)
        return {"ssid": data.get("ssid"), "password": data.get("password")}
    except (OSError, json.JSONDecodeError):
        return {"ssid": None, "password": None}


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
        body = request.get_json(silent=True) or {}
        raw = bool(body.get("raw", False))
        return jsonify({"ok": engine.start_recording(raw=raw)})

    @app.route("/api/stop", methods=["POST"])
    def stop():
        return jsonify({"ok": engine.stop_recording()})

    @app.route("/api/files")
    def files():
        return jsonify(engine.list_recordings())

    @app.route("/api/files/<path:name>/download")
    def download(name):
        path = engine._safe_rec_path(name)
        if path is None or not os.path.isfile(path):
            return jsonify({"error": "not found"}), 404
        return send_file(path, as_attachment=True, download_name=name)

    @app.route("/api/files/<path:name>/delete", methods=["POST"])
    def delete(name):
        return jsonify({"ok": engine.delete_recording(name)})

    @app.route("/api/ap")
    def ap_get():
        return jsonify(_read_ap_current())

    @app.route("/api/ap", methods=["POST"])
    def ap_set():
        body = request.get_json(silent=True) or {}
        ssid = body.get("ssid", "")
        password = body.get("password", "")
        if not SSID_RE.match(ssid):
            return jsonify({"ok": False, "error": "SSID 1–32 друкованих символів"}), 400
        if not PSK_RE.match(password):
            return jsonify({"ok": False, "error": "пароль 8–63 друкованих символи"}), 400
        try:
            os.makedirs(os.path.dirname(AP_SPOOL_FILE), exist_ok=True)
            tmp = AP_SPOOL_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"ssid": ssid, "password": password}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, AP_SPOOL_FILE)
        except OSError as e:
            engine.log.warning("could not write AP spool: %s", e)
            return jsonify({"ok": False, "error": "не вдалося записати конфіг"}), 500
        return jsonify({"ok": True})

    return app


if __name__ == "__main__":
    for tool in ("ffmpeg", "rpicam-vid", "rpicam-raw"):
        if not shutil.which(tool):
            engine.log.warning("%s not found in PATH", tool)

    # Відновлення після падіння/power-cut: якщо на момент старту сервіса
    # лежить state-файл — вважаємо, що запис ішов, і продовжуємо його
    # (з тим же raw-прапорцем).
    resumed = engine._load_state()
    if resumed and resumed.get("active"):
        engine.log.info("resuming recording after boot (raw=%s)", resumed.get("raw"))
        if not engine.start_recording(raw=bool(resumed.get("raw"))):
            engine.log.warning("resume failed, clearing state")
            engine._persist_state(False)

    create_app().run(host="0.0.0.0", port=WEB_PORT, threaded=True)
