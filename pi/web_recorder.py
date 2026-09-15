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
# Автовідновлення запису після рестарту/power-cut. Дефолт увімкнено (crash-safe
# для CSI/AP-режиму). Але на залізі, де запис душить мережу (USB-камера на Pi 3,
# спільна USB/Ethernet шина), безкінечний resume робить Pi недосяжною — там
# ставимо RESUME_ON_BOOT=0, щоб сервіс піднявся у idle, не входячи в пастку.
RESUME_ON_BOOT = os.environ.get("RESUME_ON_BOOT", "1") == "1"
# Поточний стан AP (SSID/пароль): пише root-вотчер у root-only теку, веб лише
# читає. НЕ в APP_DIR — інакше pi підмінив би файл симлінком (див. ap_apply.sh).
AP_CURRENT_FILE = os.environ.get(
    "AP_CURRENT_FILE", "/var/lib/rpi5-web/ap-current.json")
# Spool: сюди веб (pi-writable підтека) кладе бажаний конфіг, звідси root-вотчер
# його забирає.
AP_SPOOL_FILE = os.environ.get(
    "AP_SPOOL_FILE", "/var/lib/rpi5-web/spool/ap-config.json")

# WPA2-PSK: 8..63 друкованих ASCII. SSID: 1..32 байти. Валідація дублює те, що
# root-вотчер робить ще раз — spool пише непривілейований процес, довіри нема.
SSID_RE = re.compile(r"^[\x20-\x7e]{1,32}$")
PSK_RE = re.compile(r"^[\x20-\x7e]{8,63}$")

INDEX_HTML = """<!doctype html>
<html lang="uk" data-theme="night">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RPi Recorder</title>
<style>
  /* Tactical dark (default) + day, WCAG AAA контраст. Змінні на :root,
     день перевизначає. */
  :root {
    --bg: #0a0f0b; --panel: #131a14; --panel2: #0e140f;
    --text: #e9f2ea; --muted: #9fb3a2; --edge: #2c3a2e;
    --accent: #29c250; --accent-ink: #04140a;
    --danger: #ff453a; --danger-ink: #1a0000;
    --info: #3aa0ff; --focus: #7fd7ff;
  }
  html[data-theme="day"] {
    --bg: #eef1ee; --panel: #ffffff; --panel2: #f4f6f4;
    --text: #0a0f0b; --muted: #47563f; --edge: #c2ccbf;
    --accent: #12862f; --accent-ink: #ffffff;
    --danger: #c1122a; --danger-ink: #ffffff;
    --info: #0a5fb4; --focus: #0a5fb4;
  }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 640px;
         margin: 0 auto; padding: 0.75rem 0.9rem 3rem; color: var(--text);
         background: var(--bg); -webkit-text-size-adjust: 100%; }
  .topbar { display: flex; align-items: center; justify-content: space-between;
            margin-bottom: 0.6rem; }
  h1 { font-size: 1.25rem; margin: 0; letter-spacing: 0.02em; }
  h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em;
       color: var(--muted); margin: 1.6rem 0 0.4rem; }
  .hint { color: var(--muted); font-size: 0.85rem; margin: 0 0 0.6rem;
          line-height: 1.5; }
  svg { display: block; }

  /* Перемикач день/ніч */
  #theme { background: var(--panel); border: 1px solid var(--edge);
           color: var(--text); border-radius: 10px; width: 46px; height: 46px;
           display: grid; place-items: center; cursor: pointer;
           transition: background .2s, border-color .2s; }
  #theme:hover { border-color: var(--accent); }

  /* Статус-панель: іконка + слово + таймер. Найбільший елемент на екрані. */
  .status { display: flex; align-items: center; gap: 0.7rem; padding: 1rem;
            border-radius: 14px; background: var(--panel);
            border: 2px solid var(--edge); margin-bottom: 0.9rem; }
  .status .dot { width: 18px; height: 18px; border-radius: 50%;
                 background: var(--muted); flex: none; }
  .status .word { font-size: 1.15rem; font-weight: 800; letter-spacing: 0.04em;
                  text-transform: uppercase; }
  .status .timer { margin-left: auto; font-variant-numeric: tabular-nums;
                   font-size: 1.8rem; font-weight: 800; letter-spacing: 0.02em;
                   font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; }
  .status .file { font-size: 0.8rem; color: var(--muted); }
  .status.rec { border-color: var(--danger);
                background: color-mix(in srgb, var(--danger) 14%, var(--panel)); }
  .status.rec .dot { background: var(--danger); animation: blink 1s steps(2) infinite; }
  .status.rec .word, .status.rec .timer { color: var(--danger); }
  .status.warn { border-color: var(--danger); }
  .status.warn .word { color: var(--danger); }
  @keyframes blink { 50% { opacity: 0.25; } }
  @media (prefers-reduced-motion: reduce) { .status.rec .dot { animation: none; } }

  /* Великі кнопки під рукавиці (≥56px), контрастні */
  .controls { display: grid; grid-template-columns: 1fr 1fr; gap: 0.6rem; }
  .btn { min-height: 60px; font-size: 1.15rem; font-weight: 800; border: none;
         border-radius: 12px; cursor: pointer; display: flex; align-items: center;
         justify-content: center; gap: 0.5rem; letter-spacing: 0.03em;
         transition: filter .15s, opacity .15s; color: var(--accent-ink); }
  .btn:hover:not(:disabled) { filter: brightness(1.08); }
  .btn:active:not(:disabled) { filter: brightness(0.94); }
  .btn-start { background: var(--accent); }
  .btn-stop { background: var(--danger); color: var(--danger-ink); }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-wide { grid-column: 1 / -1; }
  .btn-ghost { background: var(--panel); color: var(--text);
               border: 1px solid var(--edge); min-height: 52px; font-size: 1rem;
               font-weight: 700; }
  .btn-ghost:hover:not(:disabled) { border-color: var(--info); filter: none; }

  label.raw { display: flex; align-items: center; gap: 0.5rem; margin-top: 0.7rem;
              font-size: 0.95rem; color: var(--muted); cursor: pointer; }
  label.raw input { width: 1.3rem; height: 1.3rem; accent-color: var(--danger); }

  #snapWrap { margin-top: 0.7rem; }
  #snapImg { width: 100%; border-radius: 12px; border: 1px solid var(--edge);
             display: none; }

  table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
  td.bad { color: var(--danger); font-weight: 700; }
  th, td { border-bottom: 1px solid var(--edge); padding: 0.6rem 0.5rem;
           text-align: left; }
  th { color: var(--muted); font-size: 0.72rem; text-transform: uppercase;
       letter-spacing: 0.06em; }
  td.st-rec { color: var(--danger); font-weight: 700; }
  .act { display: flex; gap: 0.4rem; }
  .act a, .act button { font-size: 0.85rem; padding: 0.45rem 0.7rem;
                        border-radius: 8px; border: none; cursor: pointer;
                        font-weight: 700; text-decoration: none; }
  .act a { background: var(--accent); color: var(--accent-ink); }
  .act button.del { background: var(--danger); color: var(--danger-ink); }
  .empty { color: var(--muted); padding: 0.8rem 0; }

  details.set { margin-top: 0.7rem; border: 1px solid var(--edge);
                border-radius: 12px; background: var(--panel2); }
  details.set > summary { cursor: pointer; padding: 0.8rem 0.9rem;
                          font-weight: 700; font-size: 0.95rem;
                          letter-spacing: 0.02em; list-style: none; }
  details.set > summary::-webkit-details-marker { display: none; }
  details.set > summary::after { content: "▾"; float: right; color: var(--muted); }
  details.set[open] > summary::after { content: "▴"; }
  details.set .body { padding: 0 0.9rem 0.9rem; }
  details.set label { display: block; font-size: 0.78rem; color: var(--muted);
                      text-transform: uppercase; letter-spacing: 0.06em;
                      margin: 0.6rem 0 0.25rem; }
  details.set select, details.set input { width: 100%; font-size: 1rem;
                      padding: 0.65rem; background: var(--panel);
                      color: var(--text); border: 1px solid var(--edge);
                      border-radius: 10px; }
  details.set .btn { margin-top: 0.8rem; background: var(--info);
                     color: var(--accent-ink); min-height: 52px; width: 100%; }
  #setmsg { font-size: 0.85rem; color: var(--muted); margin-top: 0.5rem;
            line-height: 1.5; }
  #setmsg.err { color: var(--danger); }

  form.ap input { font-size: 1rem; padding: 0.7rem; margin: 0.25rem 0;
                  width: 100%; background: var(--panel2); color: var(--text);
                  border: 1px solid var(--edge); border-radius: 10px; }
  form.ap .btn { margin-top: 0.5rem; background: var(--info);
                 color: var(--accent-ink); min-height: 52px; }
  #apmsg, #tablemsg { font-size: 0.9rem; color: var(--muted); margin-top: 0.5rem;
                      line-height: 1.5; }

  dialog { padding: 1.3rem; max-width: 22rem; border: 2px solid var(--edge);
           border-radius: 14px; background: var(--panel); color: var(--text); }
  dialog::backdrop { background: rgba(0,0,0,0.6); }
  .dlg-btns { display: flex; gap: 0.6rem; margin-top: 1.1rem; }
  .dlg-btns .btn { flex: 1; min-height: 52px; }

  :focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
</style>
</head>
<body>
<div class="topbar">
  <h1>RPi RECORDER</h1>
  <button id="theme" aria-label="Перемкнути день/ніч" title="День / ніч"></button>
</div>

<div class="status" id="status" role="status" aria-live="polite">
  <span class="dot"></span>
  <div>
    <div class="word" id="statusWord">…</div>
    <div class="file" id="statusFile"></div>
  </div>
  <div class="timer" id="timer"></div>
</div>

<div class="controls">
  <button class="btn btn-start" id="start" aria-label="Почати запис">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="12" r="8"/></svg>
    СТАРТ
  </button>
  <button class="btn btn-stop" id="stop" aria-label="Зупинити запис">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
    СТОП
  </button>
  <button class="btn btn-ghost btn-wide" id="snap" aria-label="Швидке фото для перевірки кадру">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
    Перевірити кадр
  </button>
</div>
<label class="raw" id="rawWrap">
  <input type="checkbox" id="raw"> raw (сира зйомка — їсть багато місця, хвилини на карту)
</label>
<details class="set" id="capset">
  <summary>Режим зйомки</summary>
  <div class="body">
    <p class="hint">Режим сенсора вирішує, скільки кадру видно (FoV) і яка
      стеля fps. Роздільність запису — окремо: можна знімати повний кадр,
      а писати легший 1080p.</p>
    <div id="modeWrap">
      <label for="mode">Режим сенсора (FoV)</label>
      <select id="mode"></select>
    </div>
    <label for="res">Роздільність запису</label>
    <select id="res"></select>
    <label for="fps">FPS</label>
    <input id="fps" type="number" min="1" max="120" step="1">
    <button class="btn" id="setapply">Застосувати</button>
    <div id="setmsg"></div>
  </div>
</details>
<div id="snapWrap"><img id="snapImg" alt="Кадр з камери для перевірки наведення"></div>

<h2>Записи</h2>
<p class="hint">Твої відео. «Download» — скачати, «Delete» — видалити (спитає підтвердження).</p>
<div id="tablemsg"></div>
<table id="files">
  <thead><tr>
    <th>Назва</th><th>Розмір</th><th>Дата</th><th>Статус</th><th>Дії</th>
  </tr></thead>
  <tbody></tbody>
</table>

<h2>Точка доступу (мережа)</h2>
<p class="hint">Зміни імʼя мережі та пароль — якщо поруч кілька рекордерів, щоб не плутати. Увага: після зміни зʼєднання розірветься, треба буде підключитись наново.</p>
<form class="ap" id="apform">
  <div><input id="ssid" placeholder="Імʼя мережі (SSID)" maxlength="32" required></div>
  <div><input id="psk" placeholder="Пароль (8–63 символи)" minlength="8" maxlength="63" required></div>
  <button type="submit" class="btn">Зберегти й застосувати</button>
</form>
<div id="apmsg"></div>

<dialog id="confirm">
  <div id="confirmtext"></div>
  <div class="dlg-btns">
    <button class="btn btn-stop" id="confirmyes">Видалити</button>
    <button class="btn btn-ghost" id="confirmno">Скасувати</button>
  </div>
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
function mmss(sec) {
  sec = Math.max(0, sec | 0);
  const m = (sec / 60) | 0, s = sec % 60;
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}
let timerBase = 0;       // elapsed_sec з останнього /api/status
let timerAt = 0;         // Date.now() коли його отримали
let isRecording = false;
function paintTimer() {
  const el = document.getElementById('timer');
  if (!isRecording) { el.textContent = ''; return; }
  const extra = ((Date.now() - timerAt) / 1000) | 0;
  el.textContent = mmss(timerBase + extra);
}
async function refresh() {
  const box = document.getElementById('status');
  const word = document.getElementById('statusWord');
  const file = document.getElementById('statusFile');
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    isRecording = s.recording;
    box.className = 'status' + (s.recording ? ' rec' : '');
    if (s.recording) {
      word.textContent = s.stopping ? 'ЗУПИНЯЄТЬСЯ'
        : (s.format === 'raw' ? 'ЗАПИС · RAW' : 'ЗАПИС');
      file.textContent = s.filename || '';
      timerBase = s.elapsed_sec || 0; timerAt = Date.now();
    } else {
      file.textContent = '';
      document.getElementById('timer').textContent = '';
      if (s.last_stop_reason === 'low_space') {
        box.className = 'status warn';
        word.textContent = 'СТОП · МАЛО МІСЦЯ';
      } else {
        word.textContent = 'НЕ ПИШЕ';
      }
    }
    // raw доступний тільки на CSI — на USB ховаємо, щоб не плутати.
    document.getElementById('rawWrap').style.display = s.raw_supported ? 'flex' : 'none';
    document.getElementById('start').disabled = s.recording;
    document.getElementById('stop').disabled = !s.recording;
    document.getElementById('raw').disabled = s.recording;
    document.getElementById('snap').disabled = s.recording;
    // Камеру вже відкрито з іншими параметрами — міняти під запис нічого.
    for (const id of ['mode', 'res', 'fps', 'setapply']) {
      document.getElementById(id).disabled = s.recording;
    }
    paintTimer();
  } catch (e) {
    isRecording = false;
    box.className = 'status warn';
    word.textContent = 'НЕМА ЗВʼЯЗКУ';
    file.textContent = ''; document.getElementById('timer').textContent = '';
    document.getElementById('start').disabled = true;
    document.getElementById('stop').disabled = true;
    document.getElementById('snap').disabled = true;
  }
}
function statusCell(f, isRec) {
  if (isRec) return '<td class="rec">● пишеться</td>';
  const c = f.fps_check;
  // Недобір кадрів: файл маркований заявленим fps, тож відео йде прискорено.
  if (c && !c.ok) {
    return '<td class="bad" title="Камера не встигала — відео програється прискорено">⚠ '
      + c.real_fps + ' з ' + c.requested_fps + ' fps</td>';
  }
  return '<td>готово</td>';
}
async function refreshFiles() {
  const tb = document.querySelector('#files tbody');
  try {
    const r = await fetch('/api/files');
    const rows = await r.json();
    tb.innerHTML = '';
    document.getElementById('tablemsg').textContent = '';
    if (rows.length === 0) {
      tb.innerHTML = '<tr><td colspan="5" class="empty">Ще нема записів. Натисни «Старт».</td></tr>';
      return;
    }
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
        statusCell(f, isRec) +
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
    else document.getElementById('statusWord').textContent = 'НЕ ВДАЛОСЬ';
  } catch (e) {
    document.getElementById('statusWord').textContent = 'НЕМА ЗВʼЯЗКУ';
  }
}
async function stopRec() {
  try {
    await fetch('/api/stop', {method: 'POST'});
    refresh(); refreshFiles();
    // Перевірка fps рахується у фоні після стопу — підтягнути її результат.
    setTimeout(refreshFiles, 4000);
  } catch (e) {
    document.getElementById('statusWord').textContent = 'НЕМА ЗВʼЯЗКУ';
  }
}
document.getElementById('start').addEventListener('click', startRec);
document.getElementById('stop').addEventListener('click', stopRec);

// Швидке фото — знімок для перевірки наведення (тільки коли не пише).
const snapImg = document.getElementById('snapImg');
document.getElementById('snap').addEventListener('click', async () => {
  const btn = document.getElementById('snap');
  btn.disabled = true;
  const old = btn.innerHTML; btn.innerHTML = 'Знімаю…';
  try {
    const r = await fetch('/api/snapshot', {method: 'POST'});
    if (r.ok) {
      snapImg.src = '/api/snapshot.jpg?t=' + Date.now();
      snapImg.style.display = 'block';
    }
  } catch (e) {}
  btn.innerHTML = old; btn.disabled = false;
});

// Перемикач день/ніч, вибір памʼятається.
const SUN = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON = '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
const themeBtn = document.getElementById('theme');
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  themeBtn.innerHTML = t === 'day' ? MOON : SUN;
  try { localStorage.setItem('rpi_theme', t); } catch (e) {}
}
themeBtn.addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  applyTheme(cur === 'day' ? 'night' : 'day');
});
let saved = 'night';
try { saved = localStorage.getItem('rpi_theme') || 'night'; } catch (e) {}
// ?theme=day|night перекриває (зручно шерити лінк на конкретну тему).
const qtheme = new URLSearchParams(location.search).get('theme');
if (qtheme === 'day' || qtheme === 'night') saved = qtheme;
applyTheme(saved);
setInterval(paintTimer, 1000);

// --- Режим зйомки: режим сенсора (FoV) + роздільність + fps ---------------
let CAM = null;
function currentMode() {
  if (!CAM || !CAM.modes.length) return null;
  const key = document.getElementById('mode').value;
  return CAM.modes.find(m => m.key === key) || CAM.modes[0];
}
function fillRes(keepW, keepH) {
  const m = currentMode();
  const list = m ? m.resolutions : CAM.resolutions;
  const sel = document.getElementById('res');
  sel.innerHTML = '';
  for (const r of list) {
    const o = document.createElement('option');
    o.value = r.width + 'x' + r.height;
    o.textContent = r.width + '×' + r.height + ' · ≤' + r.max_fps + ' fps'
      + (r.hw ? '  (HW-енкодер)' : '  (SW, гріється)');
    sel.appendChild(o);
  }
  // Зберігаємо поточний вибір при зміні режиму; якщо він у новому режимі
  // недоступний — беремо найбільшу з дозволених.
  const want = keepW + 'x' + keepH;
  const last = list[list.length - 1];
  sel.value = list.some(r => r.width + 'x' + r.height === want)
    ? want : (last ? last.width + 'x' + last.height : '');
  syncFps();
}
function syncFps() {
  // Стеля fps залежить і від режиму, і від роздільності (на Pi 4 — заміряна).
  const m = currentMode();
  const list = m ? m.resolutions : CAM.resolutions;
  const r = list.find(x => x.width + 'x' + x.height === document.getElementById('res').value);
  const fps = document.getElementById('fps');
  fps.max = r ? r.max_fps : (m ? Math.floor(m.max_fps) : 120);
  if (parseInt(fps.value, 10) > fps.max) fps.value = fps.max;
}
document.getElementById('res').addEventListener('change', syncFps);
async function loadCamera() {
  try {
    const r = await fetch('/api/camera');
    CAM = await r.json();
  } catch (e) { return; }
  const modeSel = document.getElementById('mode');
  document.getElementById('modeWrap').style.display =
    CAM.modes.length ? 'block' : 'none';
  modeSel.innerHTML = '';
  for (const m of CAM.modes) {
    const o = document.createElement('option');
    o.value = m.key; o.textContent = m.label;
    modeSel.appendChild(o);
  }
  if (CAM.modes.length) {
    modeSel.value = CAM.modes.some(m => m.key === CAM.settings.mode)
      ? CAM.settings.mode : 'auto';
  }
  fillRes(CAM.settings.width, CAM.settings.height);
  document.getElementById('fps').value = CAM.settings.fps;
}
document.getElementById('mode').addEventListener('change', () => {
  const [w, h] = document.getElementById('res').value.split('x').map(Number);
  fillRes(w, h);
});
document.getElementById('setapply').addEventListener('click', async () => {
  const msg = document.getElementById('setmsg');
  const [width, height] = document.getElementById('res').value.split('x').map(Number);
  const body = {
    mode: CAM && CAM.modes.length ? document.getElementById('mode').value : 'auto',
    width, height,
    fps: parseInt(document.getElementById('fps').value, 10),
  };
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const b = await r.json();
    msg.className = b.ok ? '' : 'err';
    msg.textContent = b.ok
      ? 'Збережено. Діє з наступного запису.'
      : ('Не застосовано: ' + (b.error || 'помилка'));
    if (b.ok) loadCamera();
  } catch (e) {
    msg.className = 'err';
    msg.textContent = 'нема звʼязку з рекордером';
  }
});

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
loadCamera();
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

    @app.route("/api/camera")
    def camera():
        return jsonify(engine.camera_info())

    @app.route("/api/settings", methods=["POST"])
    def settings():
        body = request.get_json(silent=True) or {}
        ok, error = engine.apply_settings(body)
        if not ok:
            # 409, не 400: найчастіша причина — «йде запис», це конфлікт стану.
            return jsonify({"ok": False, "error": error}), 409
        return jsonify({"ok": True, "settings": engine.get_settings()})

    @app.route("/api/snapshot", methods=["POST"])
    def snapshot():
        path = engine.capture_snapshot()
        if path is None:
            return jsonify({"ok": False, "error": "камера зайнята або помилка"}), 409
        return jsonify({"ok": True})

    @app.route("/api/snapshot.jpg")
    def snapshot_img():
        if not os.path.isfile(engine.SNAPSHOT_PATH):
            return jsonify({"error": "no snapshot"}), 404
        # no-store: кадр одноразовий, браузер не має кешувати старий.
        return send_file(engine.SNAPSHOT_PATH, mimetype="image/jpeg",
                         max_age=0, last_modified=None, etag=False)

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
    # (з тим же raw-прапорцем). Вимикається RESUME_ON_BOOT=0 — тоді стан
    # чиститься, а сервіс піднімається в idle (див. чому вище).
    resumed = engine._load_state()
    if resumed and resumed.get("active"):
        if RESUME_ON_BOOT:
            engine.log.info("resuming recording after boot (raw=%s)", resumed.get("raw"))
            if not engine.start_recording(raw=bool(resumed.get("raw"))):
                engine.log.warning("resume failed, clearing state")
                engine._persist_state(False)
        else:
            engine.log.info("RESUME_ON_BOOT=0 — clearing stale state, starting idle")
            engine._persist_state(False)

    create_app().run(host="0.0.0.0", port=WEB_PORT, threaded=True)
