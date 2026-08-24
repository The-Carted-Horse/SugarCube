"""Web admin UI — edit config.json from a browser.

Serves the dashboard and a settings page (default port 80; falls back to
8080 when 80 isn't available) where users, ports, API secrets, Tidepool
sources, and display thresholds can be edited. Saving validates the new
config, writes it atomically, and exits the process so systemd restarts
the app with the new settings (Restart=always).

Protected with HTTP Basic auth when config.admin.password is set.
"""

import base64
import html
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import secrets as secrets_mod

from . import config as config_mod
from . import network, predict, synclog, updater
from .server import DualStackServer
from .config import SCREEN_PNG, Config, merged_thresholds
from .store import Store

log = logging.getLogger("sugarcube.webadmin")

PAGE_STYLE = """
:root, [data-theme=dark] { color-scheme: dark;
  --bg:#0d1117; --card:#161b22; --line:#2d333b; --fg:#ebeef1; --dim:#9aa4af;
  --faint:#6e7681; --accent:#58a6ff; --btn:#238636; --danger:#f85149; }
[data-theme=light] { color-scheme: light;
  --bg:#f4f6f8; --card:#ffffff; --line:#c6ccd3; --fg:#1a2027; --dim:#5c6670;
  --faint:#8a939c; --accent:#0969da; --btn:#1a7f37; --danger:#ce2626; }
body { font-family: -apple-system, system-ui, sans-serif; background: var(--bg);
       color: var(--fg); max-width: 760px; margin: 1.5rem auto; padding: 0 1rem; }
h1 { font-size: 1.3rem; } h2 { font-size: 1.05rem; margin-top: 1.8rem; color: var(--dim); }
nav { display:flex; gap:.7rem; align-items:center; margin-bottom:1rem; flex-wrap:wrap; }
nav a, nav button.link { color: var(--dim); background:none; border:1px solid var(--line);
  border-radius:8px; padding:.3rem .7rem; font-size:.85rem; text-decoration:none;
  cursor:pointer; margin:0; }
fieldset { border: 1px solid var(--line); border-radius: 8px; margin: 1rem 0; padding: 1rem; }
legend { padding: 0 .5rem; color: var(--accent); }
label { display: inline-block; width: 11rem; color: var(--dim); }
input, select { background: var(--card); color: var(--fg); border: 1px solid var(--line);
        border-radius: 6px; padding: .35rem .5rem; margin: .2rem 0; width: 16rem; }
input.short { width: 6rem; }
.row { margin: .15rem 0; }
button { background: var(--btn); color: white; border: 0; border-radius: 6px;
         padding: .6rem 1.4rem; font-size: 1rem; cursor: pointer; margin-top: 1rem; }
button.minor { background: none; border: 1px solid var(--line); color: var(--dim);
         padding: .3rem .8rem; font-size: .85rem; margin-top: .5rem; }
button.danger { border-color: var(--danger); color: var(--danger); }
img.screen { width: 100%; border: 1px solid var(--line); border-radius: 8px; margin-top: .5rem; }
.status { color: var(--dim); font-size: .9rem; margin: .2rem 0; }
.status.err { color: var(--danger); }
pre.detail { background: var(--card); border: 1px solid var(--line);
  border-radius: 6px; padding: .5rem; font-size: .75rem; overflow-x: auto;
  white-space: pre-wrap; word-break: break-word; color: var(--dim); }
.note { color: var(--faint); font-size: .85rem; }
table { width:100%; border-collapse:collapse; font-size:.85rem; }
td, th { padding:.35rem .5rem; border-bottom:1px solid var(--line); text-align:left; }
th { color: var(--dim); font-weight:600; }
td.err { color: var(--danger); }
td.time { white-space:nowrap; color: var(--dim); }
"""

THEME_SCRIPT = """<script>
(function(){
  const t = localStorage.theme ||
    (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  document.documentElement.dataset.theme = t;
  window.toggleTheme = function(){
    const n = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    localStorage.theme = n;
    document.documentElement.dataset.theme = n;
  };
})();
</script>"""

NAV_HTML = """<nav><a href="/">Dashboard</a><a href="/settings">Settings</a>
<a href="/log">Sync log</a>
<button class="link" type="button" onclick="toggleTheme()">Theme</button></nav>"""

SETTINGS_SCRIPT = """<script>
setInterval(() => {
  const img = document.getElementById('screen');
  if (img) img.src = '/screen.png?t=' + Date.now();
}, 5000);
function updateSrc(sel) {
  document.querySelectorAll('.srcgrp[data-i="' + sel.dataset.i + '"]').forEach(g => {
    g.style.display = g.dataset.kind.split(' ').includes(sel.value) ? '' : 'none';
  });
}
function initSrc(sel) { sel.addEventListener('change', () => updateSrc(sel)); updateSrc(sel); }
document.querySelectorAll('.srcsel').forEach(initSrc);
function removePerson(i) {
  document.querySelector('[name=u' + i + '_remove]').value = '1';
  document.getElementById('fs' + i).style.display = 'none';
}
function addPerson() {
  let maxI = -1, maxPort = 1336;
  document.querySelectorAll('fieldset.person').forEach(fs => {
    maxI = Math.max(maxI, +fs.dataset.i || 0);
    const p = fs.querySelector('[name$=_port]');
    if (p && +p.value) maxPort = Math.max(maxPort, +p.value);
  });
  const i = maxI + 1;
  const markup = document.getElementById('person-template').innerHTML
    .replaceAll('__I__', i).replaceAll('__PORT__', maxPort + 1);
  document.getElementById('people').insertAdjacentHTML('beforeend', markup);
  initSrc(document.querySelector('#fs' + i + ' .srcsel'));
}
</script>"""

LOG_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SugarCube sync log</title>__THEME__<style>__STYLE__</style></head><body>
__NAV__
<h1>Sync log</h1>
<p class="note">Most recent first. Cleared when the app restarts.</p>
<table><thead><tr><th>Time</th><th>Person</th><th>Source</th><th>Event</th></tr></thead>
<tbody id="rows"><tr><td colspan="4">loading&hellip;</td></tr></tbody></table>
<script>
async function refreshLog(){
  try {
    const r = await fetch('/api/log.json', {cache:'no-store'});
    const d = await r.json();
    document.getElementById('rows').innerHTML = d.entries.length
      ? d.entries.map(e =>
          `<tr><td class="time">${new Date(e.ts).toLocaleTimeString()}</td>` +
          `<td>${e.user}</td><td>${e.source}</td>` +
          `<td${e.ok ? '' : ' class="err"'}>${e.message}</td></tr>`).join('')
      : '<tr><td colspan="4">no sync activity yet</td></tr>';
  } catch (err) {}
}
refreshLog();
setInterval(refreshLog, 15000);
</script></body></html>"""


DASHBOARD_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SugarCube</title>
<style>
@font-face { font-family:'Space Grotesk'; font-weight:700;
  src:url('/fonts/SpaceGrotesk-Bold.ttf') format('truetype'); }
@font-face { font-family:'Space Grotesk'; font-weight:500;
  src:url('/fonts/SpaceGrotesk-Medium.ttf') format('truetype'); }
@font-face { font-family:'JetBrains Mono'; font-weight:400;
  src:url('/fonts/JetBrainsMono-Regular.ttf') format('truetype'); }
@font-face { font-family:'JetBrains Mono'; font-weight:500;
  src:url('/fonts/JetBrainsMono-Medium.ttf') format('truetype'); }
:root, [data-theme=dark] { color-scheme: dark;
  --bg:#0a0c0f; --band:#14191e; --line:#262d34; --fg:#e9edf1;
  --dim:#7a848e; --faint:#545d66; --trace:#9da5ae;
  --inrange:#5fde96; --high:#e9b949; --low:#f45c54; --urgent:#ff453a;
}
[data-theme=light] { color-scheme: light;
  --bg:#f6f7f5; --band:#e9ebe6; --line:#d1d4cf; --fg:#181c20;
  --dim:#666e76; --faint:#949ba2; --trace:#7a828a;
  --inrange:#109448; --high:#b07408; --low:#cc2c24; --urgent:#e00000;
}
* { box-sizing:border-box; margin:0; }
html, body { height:100%; }
body { font-family:'JetBrains Mono',ui-monospace,monospace; background:var(--bg);
       color:var(--fg); display:flex; flex-direction:column; overflow:hidden;
       transition:background .2s; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr));
        flex:1 1 auto; min-height:0; }
.card { padding:clamp(.8rem,3vh,1.6rem) clamp(1rem,3.5vw,2.4rem);
        display:flex; flex-direction:column; min-height:0;
        border-left:1px solid var(--line); }
.card:first-child { border-left:0; }
.card.urgent { box-shadow:inset 0 0 0 3px var(--urgent); border-radius:12px; }
.head { display:flex; justify-content:space-between; align-items:baseline; }
.who { font-weight:500; letter-spacing:.35em;
       font-size:clamp(.85rem,2.6vh,1.4rem); text-transform:uppercase; }
.badge { color:var(--dim); letter-spacing:.22em;
         font-size:clamp(.6rem,1.7vh,.85rem); white-space:nowrap; }
.dot { display:inline-block; width:.55em; height:.55em; border-radius:50%;
       margin-right:.55em; vertical-align:6%; }
.bigrow { display:flex; align-items:flex-start; gap:clamp(.8rem,3vw,2rem);
          flex:0 0 auto; margin:.2rem 0 0; }
.big { font-family:'Space Grotesk',system-ui,sans-serif; font-weight:700;
       font-size:clamp(4rem,22vh,11rem); line-height:.95;
       letter-spacing:-.01em; }
.side { display:flex; flex-direction:column; gap:.35em;
        padding-top:clamp(.4rem,2vh,1.2rem);
        font-size:clamp(1rem,3.6vh,2rem); }
.side .arrow { font-weight:500; font-size:1.15em; line-height:1; }
.side .delta { font-family:'Space Grotesk',system-ui,sans-serif;
               font-weight:500; line-height:1; }
.side .unit { color:var(--faint); letter-spacing:.22em;
              font-size:clamp(.55rem,1.5vh,.8rem); }
.fcrow { display:flex; align-items:center; gap:.9em; margin-top:auto;
         padding-top:.4rem; font-size:clamp(.6rem,1.7vh,.85rem); }
.fcrow .lbl { color:var(--dim); letter-spacing:.22em; white-space:nowrap; }
.fcrow .rule { flex:1 1 auto; border-top:1px solid var(--line); }
.fcrow .val { font-family:'Space Grotesk',system-ui,sans-serif; font-weight:700;
              font-size:1.7em; }
.fcrow .eta { color:var(--faint); letter-spacing:.15em; }
.chartbox { flex:0 0 auto; height:clamp(90px,24vh,220px); margin-top:.5rem; }
.chartbox svg { width:100%; height:100%; display:block; overflow:visible; }
.stats { display:grid; grid-template-columns:repeat(4,1fr);
         margin-top:clamp(.5rem,2.4vh,1.4rem); flex:0 0 auto; }
.stats .lbl { font-size:clamp(.55rem,1.6vh,.8rem); color:var(--dim);
              letter-spacing:.22em; }
.stats .val { font-family:'Space Grotesk',system-ui,sans-serif; font-weight:700;
              font-size:clamp(1.4rem,5vh,2.6rem); line-height:1.25; }
.stats .val small { font-size:.5em; color:var(--dim); font-weight:700; }
.stats .sub2 { font-size:clamp(.5rem,1.4vh,.75rem); color:var(--faint);
               letter-spacing:.18em; }
footer { flex:0 0 auto; display:flex; align-items:center; gap:1.2rem;
         border-top:1px solid var(--line);
         padding:.55rem clamp(1rem,2.5vw,2rem);
         font-size:clamp(.6rem,1.7vh,.8rem); letter-spacing:.22em;
         color:var(--dim); }
footer a, footer button { color:var(--dim); background:none; border:0;
  font:inherit; letter-spacing:inherit; text-decoration:none; cursor:pointer;
  padding:0; text-transform:uppercase; }
footer .grow { flex:1 1 auto; }
#updated.err { color:var(--low); }
footer span, footer a, footer button { white-space:nowrap; }
/* Stacked (narrow) layout: viewport can't fit both cards — allow scrolling. */
@media (max-width:719px) {
  html, body { height:auto; min-height:100%; }
  body { overflow-y:auto; }
  .grid { grid-auto-rows:auto; }
  .card { border-left:0; border-top:1px solid var(--line); min-height:88vh; }
  .card:first-child { border-top:0; }
  .big { font-size:clamp(4rem,14vh,7rem); }
  footer { flex-wrap:wrap; gap:.4rem 1.1rem; }
}
</style></head><body>
<div class="grid" id="grid"></div>
<footer>
  <span id="when"></span>
  <span id="updated"></span>
  <span class="grow"></span>
  <a id="upgrade" href="/settings" style="display:none;color:var(--high)"></a>
  <a href="/log">Log</a>
  <a href="/settings">Settings</a>
  <button id="theme"></button>
</footer>
<script>
const ARROWS = {DoubleUp:"\\u2191\\u2191", SingleUp:"\\u2191", FortyFiveUp:"\\u2197",
  Flat:"\\u2192", FortyFiveDown:"\\u2198", SingleDown:"\\u2193", DoubleDown:"\\u2193\\u2193"};
const html = document.documentElement;
function applyTheme(t){ html.dataset.theme = t;
  document.getElementById('theme').innerHTML =
    t === 'dark' ? 'Night &#9788;' : 'Day &#9789;'; }
applyTheme(localStorage.theme ||
  (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));
document.getElementById('theme').onclick = () => {
  const t = html.dataset.theme === 'dark' ? 'light' : 'dark';
  localStorage.theme = t; applyTheme(t);
};
function tick(){
  const d = new Date();
  const date = d.toLocaleDateString('en-GB',
    {weekday:'short', day:'2-digit', month:'short'}).replaceAll(',','');
  const hm = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', hour12:false});
  document.getElementById('when').textContent = (date + ' \\u00b7 ' + hm).toUpperCase();
  render();  // ages keep advancing even when the server is unreachable
}
setInterval(tick, 15000);

const esc = s => String(s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function colorFor(v, th, stale){
  if (v == null || stale) return 'var(--faint)';
  if (v <= th.urgent_low || v >= th.urgent_high) return 'var(--urgent)';
  if (v < th.low) return 'var(--low)';
  if (v > th.high) return 'var(--high)';
  return 'var(--inrange)';
}
function ageC(now, then){
  if (!then) return '--';
  const m = Math.floor((now - then) / 60000);
  if (m < 1) return 'NOW';
  if (m < 60) return m + 'M';
  if (m < 1440) return Math.floor(m/60) + 'H' + String(m%60).padStart(2,'0') + 'M';
  return Math.floor(m/1440) + 'D';
}
function chart(u, th, now, W, H){
  const AXIS = 20, PH = H - AXIS;           // reserve a strip for the axis
  const est = u.forecast && u.forecast.source === 'est';
  const t0 = now - 180*60000, t1 = now + 120*60000;
  const pts = u.history || [];
  const fpts = (u.forecast && u.forecast.series) || [];
  const vals = pts.concat(fpts).map(p => p[1]);
  if (!vals.length) return `<svg viewBox="0 0 ${W} ${H}"></svg>`;
  const lo = Math.min(Math.min(...vals), th.low) - 18;
  const hi = Math.max(Math.max(...vals), th.high) + 24;
  const X = t => (t - t0) / (t1 - t0) * W;
  const Y = v => PH - (v - lo) / (hi - lo) * PH;
  let s = `<svg viewBox="0 0 ${W} ${H}" font-family="JetBrains Mono,monospace">`;
  // target range band + bounds
  s += `<rect x="0" y="${Y(th.high)}" width="${W}" height="${Y(th.low)-Y(th.high)}" fill="var(--band)"/>`;
  s += `<text x="${W-5}" y="${Y(th.high)+13}" font-size="11" fill="var(--faint)" text-anchor="end">${th.high}</text>`;
  s += `<text x="${W-5}" y="${Y(th.low)-4}" font-size="11" fill="var(--faint)" text-anchor="end">${th.low}</text>`;
  // dashed now divider
  s += `<line x1="${X(now)}" x2="${X(now)}" y1="2" y2="${PH-2}" stroke="var(--line)" stroke-dasharray="4 5"/>`;
  // forecast confidence cone, clamped inside the plot area
  if (fpts.length > 1){
    const rate = est ? 0.26 : 0.17;
    const Yc = v => Math.max(0, Math.min(PH, Y(v)));
    const up = fpts.map(p => `${X(p[0]).toFixed(1)},${Yc(p[1] + 4 + (p[0]-now)/60000*rate).toFixed(1)}`);
    const dn = fpts.map(p => `${X(p[0]).toFixed(1)},${Yc(p[1] - 4 - (p[0]-now)/60000*rate).toFixed(1)}`);
    s += `<polygon points="${up.concat(dn.reverse()).join(' ')}" fill="${colorFor(fpts[fpts.length-1][1], th, false)}" opacity="0.12"/>`;
  }
  // history trace, split on >15-min gaps so sensor outages stay visible
  let seg = [];
  const flush = () => {
    if (seg.length > 1)
      s += `<polyline fill="none" stroke="var(--trace)" stroke-width="1.6" points="${
        seg.map(p => X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1)).join(' ')}"/>`;
    seg = [];
  };
  for (const p of pts){
    if (seg.length && p[0] - seg[seg.length-1][0] > 15*60000) flush();
    seg.push(p);
  }
  flush();
  // forecast dots
  const r = Math.max(1.6, PH * 0.022);
  for (const p of fpts)
    s += `<circle cx="${X(p[0]).toFixed(1)}" cy="${Y(p[1]).toFixed(1)}" r="${r.toFixed(1)}" fill="${colorFor(p[1], th, false)}"/>`;
  // now marker: halo + dot at the latest reading
  if (pts.length){
    const last = pts[pts.length-1];
    const c = colorFor(u.sgv, th, false);
    s += `<circle cx="${X(last[0]).toFixed(1)}" cy="${Y(last[1]).toFixed(1)}" r="${(PH*0.14).toFixed(1)}" fill="${c}" opacity="0.18"/>`;
    s += `<circle cx="${X(last[0]).toFixed(1)}" cy="${Y(last[1]).toFixed(1)}" r="${(PH*0.07).toFixed(1)}" fill="${c}"/>`;
  }
  // time axis
  const AX = [[-180,'-3H','start'],[-120,'-2H','middle'],[-60,'-1H','middle'],
              [0,'NOW','middle'],[60,'+1H','middle'],[120,'+2H','end']];
  for (const [m, lab, anchor] of AX)
    s += `<text x="${((m+180)/300*W).toFixed(1)}" y="${H-3}" font-size="11" letter-spacing="2" fill="var(--faint)" text-anchor="${anchor}">${lab}</text>`;
  return s + '</svg>';
}
let lastData = null, receivedAt = 0;
// Ages/staleness advance from the moment the data arrived, so a dead
// server can't freeze the cards looking fresh.
const effNow = () => lastData.now + (Date.now() - receivedAt);
function drawCharts(){
  if (!lastData) return;
  document.querySelectorAll('.chartbox').forEach(box => {
    const u = lastData.users[+box.dataset.i];
    if (u) box.innerHTML = chart(u, u.thresholds || lastData.thresholds, effNow(),
                                 box.clientWidth || 400, box.clientHeight || 130);
  });
}
function render(){
  if (!lastData) return;
  document.getElementById('grid').innerHTML =
    lastData.users.map((u, i) =>
      card(u, u.thresholds || lastData.thresholds, effNow(), i)).join('');
  drawCharts();
}
let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer); resizeTimer = setTimeout(drawCharts, 150);
});
function card(u, th, now, idx){
  const staleMs = th.stale_minutes * 60000;
  const stale = !u.sgv_date || now - u.sgv_date > staleMs;
  const ageMin = u.sgv_date ? (now - u.sgv_date) / 60000 : 1e9;
  const dotCol = ageMin <= 7 ? 'var(--inrange)' : ageMin <= th.stale_minutes ? 'var(--high)' : 'var(--low)';
  const col = colorFor(u.sgv, th, stale);
  const urgent = !stale && u.sgv != null && (u.sgv <= th.urgent_low || u.sgv >= th.urgent_high);
  const tilde = u.forecast && u.forecast.source === 'est' ? '~' : '';
  const f2h = u.forecast && !stale ? u.forecast.horizons[120] : null;
  const eta = new Date(now + 120*60000).toLocaleTimeString([],
    {hour:'2-digit', minute:'2-digit', hour12:false});
  const fc = f2h != null
    ? `<span class="val" style="color:${colorFor(f2h, th, false)}">${tilde}${Math.round(f2h)}</span>
       <span class="eta">${eta}</span>` : '';
  const stat = (lbl, val, unit, sub) =>
    `<div><div class="lbl">${lbl}</div><div class="val">${val}<small>${unit}</small></div>` +
    (sub ? `<div class="sub2">${sub}</div>` : '') + '</div>';
  return `<div class="card${urgent ? ' urgent' : ''}">
    <div class="head"><span class="who">${esc(u.name)}</span>
      <span class="badge"><span class="dot" style="background:${dotCol}"></span>${u.source_label || 'TRIO'} \\u00b7 ${ageC(now, u.sgv_date)}</span></div>
    <div class="bigrow">
      <div class="big" style="color:${col}">${u.sgv != null ? Math.round(u.sgv) : '---'}</div>
      <div class="side">
        <span class="arrow" style="color:${col}">${!stale && ARROWS[u.direction] || ''}</span>
        <span class="delta">${u.delta != null && !stale ? (u.delta >= 0 ? '+' : '') + Math.round(u.delta) : ''}</span>
        <span class="unit">MG/DL</span>
      </div>
    </div>
    <div class="fcrow"><span class="lbl">FORECAST 2H</span><span class="rule"></span>${fc}</div>
    <div class="chartbox" data-i="${idx}"></div>
    <div class="stats">
      ${stat('IOB', u.iob != null ? u.iob.toFixed(1) : '--', u.iob != null ? 'U' : '')}
      ${stat('COB', u.cob != null ? Math.round(u.cob) : '--', u.cob != null ? 'G' : '')}
      ${stat('CARBS', u.last_carbs != null ? Math.round(u.last_carbs) : '--',
             u.last_carbs != null ? 'G' : '',
             u.last_carbs_date ? ageC(now, u.last_carbs_date) + ' AGO' : '')}
      ${stat('BOLUS', u.last_bolus != null ? u.last_bolus.toFixed(2) : '--',
             u.last_bolus != null ? 'U' : '',
             u.last_bolus_date ? ageC(now, u.last_bolus_date) + ' AGO' : '')}
    </div></div>`;
}
async function refresh(){
  const updated = document.getElementById('updated');
  try {
    const r = await fetch('/api/dashboard.json', {cache: 'no-store'});
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    lastData = d;
    receivedAt = Date.now();
    render();
    const up = document.getElementById('upgrade');
    if (d.update && d.update.available) {
      up.textContent = 'Update ' + d.update.latest;
      up.style.display = '';
    } else up.style.display = 'none';
    updated.textContent = '';
    updated.classList.remove('err');
  } catch (e) {
    updated.textContent = 'CONNECTION LOST \\u2014 RETRYING';
    updated.classList.add('err');
  }
}
tick();
refresh();
setInterval(refresh, 30000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
</script></body></html>"""


class AdminServer(DualStackServer):
    FALLBACK_PORT = 8080

    def __init__(self, config: Config, config_path: str, store: Store):
        try:
            self._bind_dual_stack(config.admin_port, AdminHandler)
        except OSError as exc:
            if config.admin_port == self.FALLBACK_PORT:
                raise
            # Port 80 needs CAP_NET_BIND_SERVICE (the systemd unit grants
            # it) and might be taken by another server. Run by hand — e.g.
            # on a dev machine — fall back to an unprivileged port and let
            # the display show that one.
            log.warning(
                "Cannot bind port %d (%s); using %d instead",
                config.admin_port, exc, self.FALLBACK_PORT,
            )
            config.admin_port = self.FALLBACK_PORT
            self._bind_dual_stack(config.admin_port, AdminHandler)
        self.config = config
        self.config_path = str(config_path)
        self.password = config.admin_password
        self.store = store


class AdminHandler(BaseHTTPRequestHandler):
    server: AdminServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug(fmt % args)

    def _send(self, body: bytes, ctype: str, code: int = 200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        if getattr(self, "_grant_cookie", False):
            self.send_header(
                "Set-Cookie",
                f"sugarcube_key={self.server.password}; Path=/;"
                " Max-Age=604800; SameSite=Lax",
            )
            self._grant_cookie = False
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.server.password:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                if base64.b64decode(header[6:]).decode().split(":", 1)[1] \
                        == self.server.password:
                    return True
            except Exception:
                pass
        # ?key= link (the setup QR encodes it): grant and set a cookie so
        # the rest of the session needs no typed login.
        query = parse_qs(urlparse(self.path).query)
        if query.get("key", [""])[0] == self.server.password:
            self._grant_cookie = True
            return True
        cookies = self.headers.get("Cookie", "")
        return f"sugarcube_key={self.server.password}" in cookies

    def _deny(self):
        self._send(
            b"Authentication required", "text/plain", 401,
            {"WWW-Authenticate": 'Basic realm="SugarCube admin"'},
        )

    # ---- GET ----

    def do_GET(self):
        if not self._authorized():
            self._deny()
            return
        path = self.path.split("?")[0]
        if path == "/":
            self._send(DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/settings":
            self._send(self._render_page().encode(), "text/html; charset=utf-8")
        elif path == "/log":
            page = (LOG_HTML.replace("__THEME__", THEME_SCRIPT)
                    .replace("__STYLE__", PAGE_STYLE).replace("__NAV__", NAV_HTML))
            self._send(page.encode(), "text/html; charset=utf-8")
        elif path == "/api/log.json":
            self._send(
                json.dumps({"entries": synclog.recent()}).encode(),
                "application/json",
            )
        elif path == "/api/dashboard.json":
            self._send(
                json.dumps(self._dashboard_data()).encode(),
                "application/json",
            )
        elif path == "/screen.png":
            try:
                with open(SCREEN_PNG, "rb") as f:
                    self._send(f.read(), "image/png")
            except OSError:
                self._send(b"no screenshot yet", "text/plain", 404)
        elif path.startswith("/fonts/"):
            # The dashboard uses the same typefaces as the physical screen;
            # serving them locally keeps the page fully offline-capable.
            name = os.path.basename(path)
            font_path = os.path.join(os.path.dirname(__file__), "fonts", name)
            if name.endswith(".ttf") and os.path.isfile(font_path):
                with open(font_path, "rb") as f:
                    self._send(f.read(), "font/ttf",
                               extra={"Cache-Control": "max-age=604800"})
            else:
                self._send(b"not found", "text/plain", 404)
        else:
            self._send(b"not found", "text/plain", 404)

    def _dashboard_data(self) -> dict:
        import time
        now_ms = int(time.time() * 1000)
        dc = self.server.config.display
        users = []
        for user in self.server.config.users:
            snap = self.server.store.snapshot(user.name)
            horizons, series, source = predict.predict(snap, now_ms)
            source_type = (user.source or {}).get("type")
            users.append({
                "name": user.name,
                "source_label": {"tidepool": "TWIIST",
                                 "nightscout": "NS"}.get(source_type, "TRIO"),
                "thresholds": {
                    **merged_thresholds(dc, user),
                    "stale_minutes": dc.stale_minutes,
                },
                "sgv": snap.sgv,
                "sgv_date": snap.sgv_date,
                "direction": snap.direction,
                "delta": snap.delta,
                "iob": snap.iob,
                "cob": snap.cob,
                "last_carbs": snap.last_carbs,
                "last_carbs_date": snap.last_carbs_date,
                "last_bolus": snap.last_bolus,
                "last_bolus_date": snap.last_bolus_date,
                "history": snap.history,
                "forecast": {
                    "horizons": horizons,
                    "series": series,
                    "source": source,
                } if horizons else None,
            })
        update_state = self.server.store.get_params(updater.PARAMS_KEY)
        return {
            "now": now_ms,
            "units": dc.units,
            "update": {
                "current": updater.current_version(),
                "latest": update_state.get("latest"),
                "available": bool(update_state.get("available")),
            },
            "thresholds": {
                "low": dc.low, "high": dc.high,
                "urgent_low": dc.urgent_low, "urgent_high": dc.urgent_high,
                "stale_minutes": dc.stale_minutes,
            },
            "users": users,
        }

    def _user_fieldset(self, i, user: dict, status: str, defaults: dict) -> str:
        e = html.escape
        source = user.get("source") or {}
        stype = source.get("type") or "push"
        selected = lambda kind: "selected" if stype == kind else ""
        ns_key = source.get("api_secret") or source.get("token") or ""
        th = user.get("thresholds") or {}
        th_val = lambda k: e(str(th[k])) if th.get(k) else ""
        legend = e(user.get("name") or "New person")
        return f"""
<fieldset class="person" data-i="{i}" id="fs{i}"><legend>{legend}</legend>
  <input type="hidden" name="u{i}_remove" value="">
  <div class="status">{status}</div>
  <div class="row"><label>Name</label><input name="u{i}_name" value="{e(user.get('name', ''))}"></div>
  <div class="row"><label>Port (Nightscout API)</label><input class="short" name="u{i}_port" value="{user.get('port', '')}"></div>
  <div class="row"><label>API secret</label><input name="u{i}_secret" value="{e(user.get('api_secret', ''))}" placeholder="(blank = generate)"></div>
  <div class="row"><label>Low / High</label>
    <input class="short" name="u{i}_th_low" value="{th_val('low')}" placeholder="{defaults['low']:g}">
    <input class="short" name="u{i}_th_high" value="{th_val('high')}" placeholder="{defaults['high']:g}"></div>
  <div class="row"><label>Urgent low / high</label>
    <input class="short" name="u{i}_th_urgent_low" value="{th_val('urgent_low')}" placeholder="{defaults['urgent_low']:g}">
    <input class="short" name="u{i}_th_urgent_high" value="{th_val('urgent_high')}" placeholder="{defaults['urgent_high']:g}"></div>
  <div class="row"><label>Data source</label>
    <select name="u{i}_source" class="srcsel" data-i="{i}">
      <option value="push" {selected('push')}>Push (Trio / Nightscout upload)</option>
      <option value="tidepool" {selected('tidepool')}>Pull from Tidepool (twiist)</option>
      <option value="nightscout" {selected('nightscout')}>Pull from a Nightscout site</option>
    </select></div>
  <div class="srcgrp" data-i="{i}" data-kind="tidepool">
    <div class="row"><label>Tidepool email</label><input name="u{i}_tp_email" value="{e(source.get('email', ''))}"></div>
    <div class="row"><label>Tidepool password</label><input type="password" name="u{i}_tp_password" value="{e(source.get('password', ''))}"></div>
  </div>
  <div class="srcgrp" data-i="{i}" data-kind="nightscout">
    <div class="row"><label>Nightscout URL</label><input name="u{i}_ns_url" value="{e(source.get('url', ''))}" placeholder="https://mysite.example.com"></div>
    <div class="row"><label>API secret or token</label><input type="password" name="u{i}_ns_key" value="{e(ns_key if stype == 'nightscout' else '')}"></div>
  </div>
  <div class="srcgrp" data-i="{i}" data-kind="tidepool nightscout">
    <div class="row"><label>Poll every (seconds)</label><input class="short" name="u{i}_poll" value="{source.get('poll_seconds', 60)}"></div>
  </div>
  <button type="button" class="minor danger" onclick="removePerson('{i}')">Remove</button>
</fieldset>"""

    def _wifi_section(self) -> str:
        if not network.available():
            return ""
        e = html.escape
        # Everything here reads cached state — scanning inline would block
        # the page for the full nmcli timeout while the hotspot is up.
        hotspot = network.hotspot_active()
        status = ("setup hotspot active — choose your home network below"
                  if hotspot else f"connectivity: {network.connectivity()}")

        wifi = network.state()
        last = ""
        if wifi.get("state") == "failed":
            last = (f'<div class="status err">Last attempt: could not join '
                    f'<b>{e(str(wifi.get("ssid", "")))}</b> — '
                    f'{e(str(wifi.get("error", "unknown error")))}</div>')
            if wifi.get("detail"):
                last += (f'<details><summary class="note">technical detail'
                         f'</summary><pre class="detail">'
                         f'{e(str(wifi["detail"]))}</pre></details>')
        elif wifi.get("state") == "joining":
            last = ('<div class="status">Attempting to join '
                    f'<b>{e(str(wifi.get("ssid", "")))}</b>&hellip;</div>')
        elif wifi.get("state") == "ok" and not hotspot:
            last = ('<div class="status">Connected to '
                    f'<b>{e(str(wifi.get("ssid", "")))}</b></div>')
        if wifi.get("reboot_error"):
            last += ('<div class="status err">Could not reboot automatically: '
                     f'{e(str(wifi["reboot_error"]))} — power-cycle the device '
                     'to finish.</div>')
        if wifi.get("hotspot_error"):
            last += ('<div class="status err">Setup hotspot could not start: '
                     f'{e(str(wifi["hotspot_error"]))}</div>')

        networks = network.cached_networks()
        age = network.scan_age_seconds()
        if networks:
            when = ("just now" if age is None or age < 90
                    else f"{int(age // 60)} min ago")
            hint = f"{len(networks)} networks found, scanned {when}"
        else:
            hint = ("no scan results yet — type your network's name below"
                    if hotspot else "no networks found; try Rescan")
        options = "".join(
            f'<option value="{e(n["ssid"])}">'
            f'{e(n["ssid"])} &mdash; {n["signal"]}%'
            f'{"" if n["secured"] else ", open"}</option>'
            for n in networks
        )
        # A datalist-backed text field: pick a scanned network *or* type
        # one in. An empty scan (the norm in AP mode) is no longer a dead
        # end, and hidden networks work too.
        return f"""<h2>Wi-Fi</h2>
<form method="POST" action="/wifi"><fieldset><legend>Network</legend>
  <div class="status">{status}</div>
  {last}
  <div class="row"><label>Network name</label>
    <input name="wifi_ssid" list="ssids" autocapitalize="none"
           autocorrect="off" spellcheck="false" required
           placeholder="pick or type a name"></div>
  <datalist id="ssids">{options}</datalist>
  <div class="row"><label>Password</label>
    <input type="password" name="wifi_password" autocapitalize="none"
           autocorrect="off" spellcheck="false"></div>
  <div class="row"><label>Hidden network</label>
    <input type="checkbox" name="wifi_hidden" value="1" style="width:auto"></div>
  <p class="note">{hint}</p>
  <button type="submit">Join network</button>
</fieldset></form>
<form method="POST" action="/wifi/rescan">
  <button type="submit" class="minor">Rescan for networks</button>
  <span class="note">only works when the setup hotspot is off</span>
</form>"""

    def _updates_section(self) -> str:
        e = html.escape
        st = self.server.store.get_params(updater.PARAMS_KEY)
        current = updater.current_version()
        if st.get("checked_at"):
            import time
            checked = time.strftime("%H:%M", time.localtime(st["checked_at"] / 1000))
            if st.get("error"):
                status = f"last check at {checked} failed: {e(str(st['error']))}"
            elif st.get("available"):
                status = (f'version <b>{e(st.get("latest", "?"))}</b> is '
                          f'available (checked {checked}) — '
                          f'<a href="{e(st.get("url", ""))}">release notes</a>')
            else:
                status = f"up to date (checked {checked})"
        else:
            status = "not checked yet — checks run every 6 hours"
        install = ""
        if st.get("available"):
            install = f"""
  <form method="POST" action="/update/apply" style="display:inline">
    <input type="hidden" name="tag" value="{e(st.get('latest_tag', ''))}">
    <button type="submit">Install {e(st.get('latest', ''))}</button>
  </form>"""
        return f"""<h2>Updates</h2>
<fieldset><legend>Software</legend>
  <div class="status">SugarCube {e(current)} &mdash; {status}</div>
  <form method="POST" action="/update/check" style="display:inline">
    <button type="submit" class="minor">Check now</button>
  </form>{install}
  <p class="note">Updates install from GitHub releases and restart the
  display (about a minute). A release marked <code>[force-update]</code>
  in its notes installs itself at the next check.</p>
</fieldset>"""

    def _render_page(self) -> str:
        raw = json.loads(open(self.server.config_path).read())
        display = raw.get("display", {})
        d = lambda key, default: display.get(key, default)
        defaults = {
            "low": d("low", 70), "high": d("high", 180),
            "urgent_low": d("urgent_low", 55), "urgent_high": d("urgent_high", 250),
        }
        import time
        now_ms = int(time.time() * 1000)
        fieldsets = []
        for i, user in enumerate(raw.get("users", [])):
            snap = self.server.store.snapshot(user["name"])
            if snap.sgv_date:
                mins = int((now_ms - snap.sgv_date) / 60000)
                status = f"last reading {snap.sgv:.0f} mg/dL, {mins}m ago"
            else:
                status = "no data yet"
            fieldsets.append(self._user_fieldset(i, user, status, defaults))
        template = self._user_fieldset(
            "__I__", {"port": "__PORT__"}, "not saved yet", defaults
        )
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SugarCube settings</title>{THEME_SCRIPT}<style>{PAGE_STYLE}</style></head><body>
{NAV_HTML}
<h1>Settings</h1>
<h2>Live display</h2>
<img class="screen" id="screen" src="/screen.png" alt="live display">
{self._wifi_section()}
{self._updates_section()}
<form method="POST" action="/save">
<h2>People</h2>
<div id="people">
{''.join(fieldsets)}
</div>
<button type="button" class="minor" onclick="addPerson()">+ Add person</button>
<template id="person-template">{template}</template>
<h2>Display defaults</h2>
<fieldset><legend>Thresholds (mg/dL) — used unless a person overrides them</legend>
  <div class="row"><label>Low</label><input class="short" name="low" value="{d('low', 70)}"></div>
  <div class="row"><label>High</label><input class="short" name="high" value="{d('high', 180)}"></div>
  <div class="row"><label>Urgent low</label><input class="short" name="urgent_low" value="{d('urgent_low', 55)}"></div>
  <div class="row"><label>Urgent high</label><input class="short" name="urgent_high" value="{d('urgent_high', 250)}"></div>
  <div class="row"><label>Stale after (minutes)</label><input class="short" name="stale_minutes" value="{d('stale_minutes', 12)}"></div>
</fieldset>
<h2>Admin</h2>
<fieldset><legend>Web access</legend>
  <div class="row"><label>New admin password</label>
    <input type="password" name="admin_password" value="" placeholder="(leave blank to keep current)"></div>
  <p class="note">Protects this web interface and the API (username: admin).
  After saving with a new password, your browser will ask you to log in again.</p>
</fieldset>
<button type="submit">Save &amp; Apply</button>
<p class="note">Saving restarts the display (takes ~5 seconds). Blank API secrets
are generated automatically; blank per-person thresholds inherit the defaults.</p>
</form>
{SETTINGS_SCRIPT}</body></html>"""

    @staticmethod
    def _updating_page(version: str) -> bytes:
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='45;url=/settings'>"
            f"<style>{PAGE_STYLE}</style></head><body>"
            f"<h1>Installing {html.escape(version)}&hellip;</h1>"
            "<p>The display restarts on the new version — this page"
            " reloads in about a minute.</p></body></html>"
        ).encode()

    # ---- POST ----

    def do_POST(self):
        if not self._authorized():
            self._deny()
            return
        post_path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        form = {
            k: v[0]
            for k, v in parse_qs(self.rfile.read(length).decode()).items()
        }
        if post_path == "/wifi":
            ssid = form.get("wifi_ssid", "").strip()
            password = form.get("wifi_password", "")
            hidden = bool(form.get("wifi_hidden"))
            if not ssid:
                body = (
                    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                    f"<style>{PAGE_STYLE}</style></head><body>"
                    "<h1>No network name</h1><p>Pick a network from the list or"
                    " type its name, then try again.</p>"
                    '<p><a href="/settings">Back to settings</a></p>'
                    "</body></html>"
                ).encode()
                self._send(body, "text/html; charset=utf-8", 400)
                return
            hotspot_pw = self.server.store.get_params("__network").get(
                "hotspot_password", "")

            def join_then_reboot():
                import time
                # The join tears the setup hotspot down, killing the
                # phone's connection — give the response below a moment
                # to reach it first.
                time.sleep(2)
                ok, _ = network.connect_wifi(ssid, password, hidden)
                if ok:
                    # Reboot so every service starts fresh on the new
                    # network and the screen never shows a stale address.
                    network.reboot()
                elif hotspot_pw:
                    # Bring the setup hotspot straight back for a retry
                    # instead of waiting on the watcher's slow checks.
                    # prescan=False: the join just scanned, and every
                    # extra second here is a second with no way in.
                    network.start_hotspot(hotspot_pw, prescan=False)

            threading.Thread(target=join_then_reboot, daemon=True).start()
            body = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,"
                " initial-scale=1'>"
                f"<style>{PAGE_STYLE}</style></head><body>"
                f"<h1>Joining {html.escape(ssid)}&hellip;</h1>"
                "<p>This takes up to a minute. The setup hotspot drops while"
                " the device tries to connect, so your phone will lose this"
                " page — that is expected.</p>"
                "<p><b>If it worked:</b> the device restarts and its screen"
                " shows the new address. Put your phone back on your home"
                " Wi-Fi and open that address.</p>"
                "<p><b>If it failed:</b> the setup hotspot comes back within"
                " a minute or two. Rejoin it, reopen the settings page, and"
                " the reason for the failure is shown at the top of the Wi-Fi"
                " section. The device's screen shows it too.</p>"
                "</body></html>"
            ).encode()
            self._send(body, "text/html; charset=utf-8")
            return
        if post_path == "/wifi/rescan":
            # Only meaningful with the radio in station mode; in AP mode
            # the scan cache from before the hotspot came up is all there is.
            threading.Thread(
                target=network.refresh_scan, kwargs={"force": True}, daemon=True
            ).start()
            import time
            time.sleep(4)  # usually enough to have fresh results to render
            self._send(b"", "text/html", 303, {"Location": "/settings"})
            return
        if post_path == "/update/check":
            state = updater.check_and_maybe_force(self.server.store)
            if state.get("forcing"):
                self._send(self._updating_page(state.get("latest", "")),
                           "text/html; charset=utf-8")
            else:
                self._send(b"", "text/html", 303, {"Location": "/settings"})
            return
        if post_path == "/update/apply":
            state = self.server.store.get_params(updater.PARAMS_KEY)
            tag = form.get("tag", "")
            # Only the release the last check offered — nothing arbitrary.
            if not state.get("available") or tag != state.get("latest_tag"):
                self._send(b"no such update on offer", "text/plain", 400)
                return
            ok, detail = updater.apply_update(tag)
            if ok:
                self._send(self._updating_page(detail),
                           "text/html; charset=utf-8")
            else:
                body = (f"<h1>Update failed</h1><p>{html.escape(detail)}</p>"
                        '<p><a href="/settings">Back</a></p>').encode()
                self._send(body, "text/html; charset=utf-8", 500)
            return
        if post_path != "/save":
            self._send(b"not found", "text/plain", 404)
            return
        try:
            self._save(form)
        except Exception as exc:
            body = (
                f"<h1>Invalid configuration</h1><p>{html.escape(str(exc))}</p>"
                '<p><a href="/">Back</a></p>'
            ).encode()
            self._send(body, "text/html; charset=utf-8", 400)
            return
        body = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='8;url=/settings'>"
            f"<style>{PAGE_STYLE}</style></head><body>"
            "<h1>Saved</h1><p>Restarting the display&hellip; "
            "this page reloads in a few seconds.</p></body></html>"
        ).encode()
        self._send(body, "text/html; charset=utf-8")
        # Exit shortly after the response flushes; systemd restarts us with
        # the new config (Restart=always).
        log.info("Config saved from web admin; restarting")
        threading.Timer(0.8, lambda: os._exit(0)).start()

    def _save(self, form: dict) -> None:
        raw = json.loads(open(self.server.config_path).read())
        users = []
        i = 0
        while f"u{i}_name" in form:
            idx = i
            i += 1
            if form.get(f"u{idx}_remove"):
                continue
            name = form[f"u{idx}_name"].strip()
            if not name:
                raise ValueError(f"person {idx + 1} needs a name")
            user = {
                "name": name,
                "port": int(form[f"u{idx}_port"]),
                "api_secret": form.get(f"u{idx}_secret", "").strip()
                              or secrets_mod.token_hex(12),
            }
            thresholds = {}
            for key in ("low", "high", "urgent_low", "urgent_high"):
                value = form.get(f"u{idx}_th_{key}", "").strip()
                if value:
                    thresholds[key] = float(value)
            if thresholds:
                user["thresholds"] = thresholds
            stype = form.get(f"u{idx}_source")
            poll = int(form.get(f"u{idx}_poll", 60) or 60)
            if stype == "tidepool":
                user["source"] = {
                    "type": "tidepool",
                    "email": form.get(f"u{idx}_tp_email", "").strip(),
                    "password": form.get(f"u{idx}_tp_password", ""),
                    "poll_seconds": poll,
                }
            elif stype == "nightscout":
                url = form.get(f"u{idx}_ns_url", "").strip()
                if url and not url.startswith(("http://", "https://")):
                    url = "https://" + url
                # The poller auto-detects whether the key is a classic API
                # secret or an access token, so one field covers both.
                user["source"] = {
                    "type": "nightscout",
                    "url": url,
                    "api_secret": form.get(f"u{idx}_ns_key", "").strip(),
                    "poll_seconds": poll,
                }
            users.append(user)
        if not users:
            raise ValueError("at least one person is required")
        raw["users"] = users
        display = raw.setdefault("display", {})
        for key in ("low", "high", "urgent_low", "urgent_high", "stale_minutes"):
            if form.get(key):
                display[key] = float(form[key])

        new_admin_password = form.get("admin_password", "").strip()
        if new_admin_password:
            if len(new_admin_password) < 6:
                raise ValueError("admin password must be at least 6 characters")
            raw.setdefault("admin", {})["password"] = new_admin_password

        tmp = self.server.config_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(raw, f, indent=2)
            f.write("\n")
        config_mod.load(tmp)  # validate before replacing the live file
        os.replace(tmp, self.server.config_path)


def start_admin(config: Config, config_path, store: Store) -> AdminServer | None:
    if not config.admin_port:
        return None
    server = AdminServer(config, config_path, store)
    thread = threading.Thread(target=server.serve_forever, name="webadmin", daemon=True)
    thread.start()
    log.info("Web admin listening on port %d", config.admin_port)
    return server
