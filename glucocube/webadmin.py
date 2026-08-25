"""Web admin UI — edit config.json from a browser.

Serves the dashboard and a settings page (default port 80; falls back to
8080 when 80 isn't available) where users, ports, API secrets, Tidepool
sources, and display thresholds can be edited. Saving validates the new
config, writes it atomically, and exits the process so systemd restarts
the app with the new settings (Restart=always).

Protected with HTTP Basic auth when config.admin.password is set.
"""

import base64
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import secrets as secrets_mod

from . import config as config_mod
from . import captive, network, onboarding, predict, synclog, ui, updater
from . import verify
from .server import DualStackServer
from .config import SCREEN_PNG, Config, merged_thresholds
from .store import Store

log = logging.getLogger("glucocube.webadmin")

SCREEN_SCRIPT = """<script>
// Keep every live screenshot on the page current (the hub thumbnail and
// the full preview are the same image).
setInterval(() => {
  document.querySelectorAll('img.live').forEach(img => {
    img.src = '/screen.png?t=' + Date.now();
  });
}, 5000);
</script>"""

PERSON_SCRIPT = """<script>
// "Test connection": check the credentials before saving, rather than
// finding out from the sync log hours later that a letter was wrong.
document.addEventListener('click', async (event) => {
  const button = event.target.closest('button.test');
  if (!button) return;
  event.preventDefault();
  const form = button.closest('form');
  const out = document.getElementById('testresult');
  const value = (name) => {
    const el = form.querySelector('[name="' + name + '"]');
    return el ? el.value : '';
  };
  const picked = form.querySelector('[name=source]:checked');
  out.hidden = false;
  out.className = 'banner info';
  out.textContent = 'Testing…';
  button.disabled = true;
  try {
    const response = await fetch('/api/source/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        index: form.dataset.index,
        type: picked ? picked.value : 'push',
        email: value('tp_email'), password: value('tp_password'),
        url: value('ns_url'), api_secret: value('ns_key'),
      }),
    });
    const result = await response.json();
    out.className = 'banner ' + (result.ok ? 'ok' : 'err');
    out.textContent = result.message;
  } catch (err) {
    out.className = 'banner err';
    out.textContent = 'Could not run the test.';
  }
  button.disabled = false;
});
</script>"""

WIFI_SCRIPT = """<script>
// A rescan runs in the background; the page asks whether it has finished
// instead of the server holding the request open while it waits.
if (location.search.indexOf('scanning=1') >= 0) {
  const tick = async () => {
    try {
      const r = await fetch('/api/wifi.json', {cache: 'no-store'});
      if (!(await r.json()).scanning) {
        location.replace('/settings/network');
        return;
      }
    } catch (err) {}
    setTimeout(tick, 1500);
  };
  setTimeout(tick, 1500);
}
</script>"""

CLOCK_SCRIPT = """<script>
// Say what time it is in the zone being chosen: "Europe/Chisinau" means
// nothing to most people, "it is 6:42 pm there" does.
(function(){
  var list = document.getElementById('timezone');
  var out = document.getElementById('tzpreview');
  var detected = document.getElementById('tzdetected');
  function preview(){
    if (!list || !out) return;
    if (!list.value) {
      out.textContent = 'The device keeps the time it has now.';
      return;
    }
    try {
      out.textContent = 'It is ' + new Date().toLocaleString(undefined,
        {timeZone: list.value, weekday: 'long', hour: 'numeric',
         minute: '2-digit'}) + ' there.';
    } catch (err) { out.textContent = ''; }
  }
  // The phone knows where it is; offer that as a one-tap answer.
  if (detected && list) {
    var zone = '';
    try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; }
    catch (err) {}
    if (zone && zone !== list.value) {
      for (var i = 0; i < list.options.length; i++) {
        if (list.options[i].value === zone) {
          detected.hidden = false;
          detected.querySelector('b').textContent = zone.replace(/_/g, ' ');
          detected.querySelector('button').onclick = function(){
            list.value = zone; detected.hidden = true; preview();
          };
          break;
        }
      }
    }
  }
  document.addEventListener('change', preview);
  preview();
  setInterval(preview, 30000);
})();
</script>"""


LOG_BODY = """<h1>Sync log</h1>
<p class="note">Most recent first. Cleared when the app restarts.</p>
<div class="tablewrap"><table>
<thead><tr><th>Time</th><th>Person</th><th>Source</th><th>Event</th></tr></thead>
<tbody id="rows"><tr><td colspan="4">loading&hellip;</td></tr></tbody>
</table></div>"""

LOG_SCRIPT = """<script>
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
</script>"""


DASHBOARD_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GlucoCube</title>
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
/* The same cube the physical screen draws, quiet in the middle. */
footer .brand { display:inline-flex; align-items:center; gap:.6em;
                color:var(--faint); }
footer .brand svg { flex:0 0 auto; }
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
  /* No room to be decorative once the footer is wrapping. */
  footer .brand, footer .grow { display:none; }
}
</style></head><body>
<div class="grid" id="grid"></div>
<footer>
  <span id="when"></span>
  <span id="updated"></span>
  <span class="grow"></span>
  <span class="brand"><svg viewBox="0 0 16 16" width="14" height="14"
    aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.2"
    stroke-linejoin="round" d="M8 1 14.1 4.5 14.1 11.5 8 15 1.9 11.5 1.9 4.5Z
    M8 8 14.1 4.5 M8 8 8 15 M8 8 1.9 4.5"/></svg>GlucoCube</span>
  <span class="grow"></span>
  <a id="upgrade" href="/settings/updates" style="display:none;color:var(--high)"></a>
  <a href="/log">Log</a>
  <a href="/settings">Settings</a>
  <a href="/fonts/OFL-JetBrainsMono.txt">Fonts</a>
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
  // The viewer's own locale, not a hard-coded en-GB: this page is read
  // from a phone that may be anywhere, and the device's own screen is the
  // thing the configured time zone is for.
  const date = d.toLocaleDateString(undefined,
    {weekday:'short', day:'2-digit', month:'short'}).replaceAll(',','');
  const hm = d.toLocaleTimeString(undefined, {hour:'2-digit', minute:'2-digit'});
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
  const eta = new Date(now + 120*60000).toLocaleTimeString(undefined,
    {hour:'2-digit', minute:'2-digit'});
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


def _g(value, default="") -> str:
    """A threshold as people write it: 70, not 70.0. Blank stays blank."""
    if value in (None, ""):
        value = default
    if value in (None, ""):
        return ""
    try:
        return format(float(value), "g")
    except (TypeError, ValueError):
        return str(value)


def _check_ranges(values: dict) -> None:
    """Reject a set of thresholds the display could not colour sensibly.

    Cheap to get wrong on a phone (a stray digit in "high"), and the
    result is a panel that is red all day, so it is worth saying no.
    """
    if values["low"] >= values["high"]:
        raise ValueError("The low has to be under the high.")
    if values["urgent_low"] > values["low"]:
        raise ValueError("The urgent low has to be at or under the low.")
    if values["urgent_high"] < values["high"]:
        raise ValueError("The urgent high has to be at or over the high.")
    if values["urgent_low"] <= 0:
        raise ValueError("Readings are above zero, so the urgent low has "
                         "to be too.")


def _number(form: dict, key: str, label: str):
    """One number out of a form, or None when it was left blank.

    Raises with the field's own name in the message: "could not convert
    string to float: 'ninety'" is not something to show anybody.
    """
    value = (form.get(key) or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{label} needs to be a number.") from None


def timezone_options() -> list[tuple[str, str]]:
    """(value, label) pairs for a time zone <select>, blank option first."""
    return [("", "Use whatever the device is set to")] + [
        (zone, zone.replace("_", " ")) for zone in config_mod.available_timezones()
    ]


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
        cookie = getattr(self, "_cookie_value", None)
        if cookie is None and getattr(self, "_grant_cookie", False):
            cookie = self.server.password
        if cookie:
            # After a password change this has to carry the NEW one, or the
            # browser is locked out the instant the process restarts — on a
            # page the user has already navigated away from.
            self.send_header(
                "Set-Cookie",
                f"glucocube_key={cookie}; Path=/;"
                " Max-Age=604800; SameSite=Lax",
            )
            self._grant_cookie = False
            self._cookie_value = None
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
        return f"glucocube_key={self.server.password}" in cookies

    def _deny(self):
        self._send(
            b"Authentication required", "text/plain", 401,
            {"WWW-Authenticate": 'Basic realm="GlucoCube admin"'},
        )

    # ---- GET ----

    def do_GET(self):
        path = self.path.split("?")[0]
        # Before the auth check on purpose: a 401 makes a phone's captive
        # browser ask for a username instead of showing the setup page.
        if captive.maybe_handle(self, path):
            return
        if not self._authorized() and not onboarding.open_without_login(path):
            self._deny()
            return
        if onboarding.handles(path):
            onboarding.do_get(self, path)
            return
        if path == "/":
            self._send(DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/settings":
            self._send(ui.page("GlucoCube settings", self._page_hub(),
                               nav=True, script=SCREEN_SCRIPT).encode(),
                       "text/html; charset=utf-8")
        elif path.startswith("/settings/"):
            self._settings_get(path)
        elif path == "/api/health.json":
            # Deliberately tiny: the page shown during a restart polls it
            # until the new process answers.
            self._send(json.dumps({"ok": True,
                                   "version": updater.current_version()}
                                  ).encode(), "application/json")
        elif path == "/log":
            page = ui.page("GlucoCube sync log", LOG_BODY, nav=True,
                           script=LOG_SCRIPT)
            self._send(page.encode(), "text/html; charset=utf-8")
        elif path == "/api/wifi.json":
            self._send(json.dumps({
                "scanning": network.scan_in_progress(),
                "networks": network.cached_networks(),
                "age": network.scan_age_seconds(),
            }).encode(), "application/json")
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
            # The OFL text is served alongside them because the license
            # asks that each copy of the fonts carry it — and this handler
            # hands a copy to every browser that loads the dashboard.
            name = os.path.basename(path)
            font_path = os.path.join(os.path.dirname(__file__), "fonts", name)
            is_font = name.endswith(".ttf")
            is_notice = name.startswith("OFL-") and name.endswith(".txt")
            if (is_font or is_notice) and os.path.isfile(font_path):
                with open(font_path, "rb") as f:
                    self._send(
                        f.read(),
                        "font/ttf" if is_font else "text/plain; charset=utf-8",
                        extra={"Cache-Control": "max-age=604800"},
                    )
            else:
                self._send(b"not found", "text/plain", 404)
        else:
            self._send(ui.page("Not found", "<h1>Not found</h1>"
                               '<p><a href="/settings">Back to settings</a></p>'
                               ).encode(), "text/html; charset=utf-8", 404)

    def do_HEAD(self):
        """Some Windows connectivity probes use HEAD rather than GET."""
        path = self.path.split("?")[0]
        if captive.maybe_handle(self, path, body=False):
            return
        if not self._authorized():
            self._deny()
            return
        known = path in ("/", "/setup", "/log") or path.startswith("/settings")
        self._send(b"", "text/html; charset=utf-8", 200 if known else 404)

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
    SOURCE_CARDS = (
        ("push", "Trio, or another uploader",
         "The pump app sends readings to this device"),
        ("tidepool", "twiist",
         "Pulled from the wearer's Tidepool account"),
        ("nightscout", "A Nightscout site",
         "Pulled from an existing cloud Nightscout"),
    )
    SOURCE_NAMES = {"push": "Trio", "tidepool": "twiist",
                    "nightscout": "Nightscout"}
    ACCESS_CARDS = (
        ("on", "Ask for a password",
         "Log in as admin — needed if anyone else can reach this network"),
        ("off", "No password",
         "Anyone on this network opens the dashboard and settings"),
    )
    CHANNEL_CARDS = (
        ("stable", "Standard",
         "Full releases only — what almost everyone wants"),
        ("beta", "Beta",
         "Pre-releases as well: new things first, rough edges included"),
    )
    # Short confirmations. Every save ends in a redirect, so what happened
    # travels in the URL as ?msg= rather than in a variable that the
    # restart would eat.
    FLASHES = {
        "saved": ("ok", "Saved."),
        "removed": ("ok", "Removed."),
        "checked": ("info", "Checked just now."),
        "channel": ("ok", "Channel changed — this device already runs the "
                          "newest release on it."),
        "nothing": ("warn", "Nothing has been published on that channel yet, "
                            "so nothing was installed."),
        "switched": ("ok", "Switched."),
    }

    # ---- settings: shared pieces ----

    def _raw_config(self) -> dict:
        return json.loads(open(self.server.config_path).read())

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _flash(self) -> str:
        found = self.FLASHES.get(self._query().get("msg", ""))
        return ui.banner(found[0], found[1]) if found else ""

    def _lan_ip(self) -> str:
        """One lookup per page render, not one per person."""
        if not getattr(self, "_lan_ip_cache", ""):
            self._lan_ip_cache = network.get_lan_ip()
        return self._lan_ip_cache

    def _theme(self) -> str:
        return self.server.store.get_params("__display").get("theme", "dark")

    @staticmethod
    def _secret_placeholder(is_current: bool, stored) -> str:
        if is_current and stored:
            return "(unchanged — type to replace)"
        return ""

    def _person_state(self, user: dict, now_ms: int) -> dict:
        """How this person is doing, in the words the lists use.

        One place, because the hub, the people list and the person page
        all have to agree about whether something needs attention.
        """
        source = user.get("source") or {}
        label = self.SOURCE_NAMES.get(source.get("type") or "push", "")
        if source.get("type") and not self._source_ready(source):
            return {"text": f"{label} — credentials missing",
                    "short": "needs setup", "pill": "needs setup",
                    "kind": "err"}
        snap = self.server.store.snapshot(user.get("name", ""))
        if not snap.sgv_date or snap.sgv is None:
            return {"text": f"{label} — nothing has arrived yet",
                    "short": "no data", "pill": "", "kind": "warn"}
        minutes = max(0, int((now_ms - snap.sgv_date) / 60000))
        when = ("just now" if minutes < 1
                else f"{minutes}m ago" if minutes < 120
                else f"{minutes // 60}h ago")
        stale = minutes > self.server.config.display.stale_minutes
        return {"text": f"{label} — {snap.sgv:.0f} mg/dL, {when}",
                "short": when, "pill": "stale" if stale else "",
                "kind": "warn" if stale else "ok"}

    # ---- settings: the hub ----

    def _page_hub(self) -> str:
        raw = self._raw_config()
        config = self.server.config
        display = raw.get("display", {})
        users = raw.get("users") or []
        update = self.server.store.get_params(updater.PARAMS_KEY)
        now_ms = int(time.time() * 1000)
        wifi_up = network.available()
        hotspot = network.hotspot_active_cached() if wifi_up else False
        wifi = network.state() if wifi_up else {}

        notices = [self._flash()]
        if update.get("available") and update.get("rejoin"):
            notices.append(ui.banner(
                "warn", "This device is on a pre-release, but set to the "
                'standard channel. <a href="/settings/updates">Put it back on '
                "a full release</a>."))
        elif update.get("available"):
            notices.append(ui.banner(
                "info", f"<b>{ui.esc(update.get('latest', ''))}</b> is ready to "
                'install. <a href="/settings/updates">See what changed</a>.'))
        if hotspot:
            notices.append(ui.banner(
                "info", "The setup hotspot is on, so this device has no "
                'internet yet. <a href="/settings/network">Join a network</a>.'))
        elif wifi.get("state") == "failed":
            notices.append(ui.banner(
                "err", f"Could not join <b>{ui.esc(wifi.get('ssid', ''))}</b>. "
                '<a href="/settings/network">Try again</a>.'))
        if not (config.admin_password or config.admin_password_off):
            notices.append(ui.banner(
                "warn", "Anyone on this network can change these settings. "
                '<a href="/settings/access">Set a password</a>.'))

        states = [(user, self._person_state(user, now_ms)) for user in users]
        parts = [f"{user.get('name') or 'unnamed'} — {state['short']}"
                 for user, state in states[:2]]
        if len(states) > 2:
            parts.append(f"and {len(states) - 2} more")
        # A pill only for what needs doing; "no data" on a device that was
        # set up ten seconds ago is not a problem, and the line already
        # says so.
        needs_setup = [s for _, s in states if s["kind"] == "err"]

        if config.admin_password:
            access_sub = "Password set — the username is admin"
        elif config.admin_password_off:
            access_sub = "No password, on purpose — open on this network"
        else:
            access_sub = "No password set"

        theme = self._theme()
        zone = display.get("timezone") or ""
        channel = config_mod.normalize_channel(config.update_channel)
        channel_label = config_mod.CHANNEL_LABELS[channel]
        if update.get("rejoin"):
            updates_sub = f"On a pre-release · {channel_label} channel"
        elif update.get("available"):
            updates_sub = (f"{update.get('latest', '')} ready to install · "
                           f"{channel_label} channel")
        elif update.get("checked_at"):
            updates_sub = (f"{updater.current_version()} · up to date · "
                           f"{channel_label} channel")
        else:
            updates_sub = (f"{updater.current_version()} · "
                           f"{channel_label} channel")

        items = [
            ui.menu_item(
                "/settings/screen", "The screen",
                "Night colours" if theme == "dark" else "Day colours",
                lead='<span class="lead">' + ui.icon("screen")
                     + '<img class="thumb live" src="/screen.png" alt=""'
                       ' onerror="this.hidden=true"'
                       ' onload="this.previousElementSibling.hidden=true">'
                       "</span>"),
            ui.menu_item(
                "/settings/people", "People",
                ", ".join(parts) if parts else "nobody yet",
                lead=ui.icon("people"),
                badge="needs setup" if needs_setup else "", badge_kind="err"),
            ui.menu_item(
                "/settings/ranges", "Ranges",
                f"{_g(display.get('low'), 70)}–{_g(display.get('high'), 180)}"
                f" · urgent under {_g(display.get('urgent_low'), 55)},"
                f" over {_g(display.get('urgent_high'), 250)}",
                lead=ui.icon("ranges")),
        ]
        if wifi_up:
            if hotspot:
                wifi_sub = "Setup hotspot is on"
            elif wifi.get("state") == "ok" and wifi.get("ssid"):
                wifi_sub = f"Connected to {wifi['ssid']}"
            elif wifi.get("state") == "failed":
                wifi_sub = f"Could not join {wifi.get('ssid', '')}"
            else:
                wifi_sub = f"{len(network.cached_networks())} networks nearby"
            items.append(ui.menu_item("/settings/network", "Wi-Fi", wifi_sub,
                                      lead=ui.icon("wifi")))
        items += [
            ui.menu_item(
                "/settings/clock", "Clock",
                f"{zone.replace('_', ' ')} · {time.strftime('%H:%M')}" if zone
                else f"No zone set — the clock reads {time.strftime('%H:%M')}",
                lead=ui.icon("clock")),
            ui.menu_item("/settings/updates", "Updates", updates_sub,
                         lead=ui.icon("update"),
                         badge="new" if update.get("available") else "",
                         badge_kind="warn"),
            ui.menu_item(
                "/settings/access", "Access", access_sub, lead=ui.icon("lock"),
                # Only a device that has no password *and* never said it
                # meant to gets the badge: a warning nobody can act on is
                # one people learn to look past.
                badge="" if config.admin_password or config.admin_password_off
                else "open",
                badge_kind="warn"),
        ]
        more = ui.menu([
            ui.menu_item("/setup?again=1", "Run guided setup again",
                         "The same questions, one screen at a time",
                         lead=ui.icon("wizard")),
            ui.menu_item("/log", "Sync log", "What has arrived, and when",
                         lead=ui.icon("log")),
        ])
        address = config_mod.admin_url(self._lan_ip(), config.admin_port)
        return f"""<h1>Settings</h1>
<p class="lede">GlucoCube {ui.esc(updater.current_version())} at
{ui.esc(address)}</p>
{''.join(notices)}
{ui.menu(items)}
<h2>More</h2>
{more}"""

    # ---- settings: one page per thing ----

    def _page_screen(self) -> str:
        theme = self._theme()
        other, label = (("light", "Switch to Day") if theme == "dark"
                        else ("dark", "Switch to Night"))
        return f"""<h1>The screen</h1>
<p class="lede">Exactly what the device is showing, refreshed every few
seconds.</p>
<img class="screen live" id="screen" src="/screen.png"
     alt="what the display shows" onerror="this.hidden=true">
<form method="POST" action="/display/theme">
  <input type="hidden" name="theme" value="{other}">
  <input type="hidden" name="back" value="/settings/screen">
  <div class="actions">
    <button type="submit" class="secondary">{label}</button>
    <span class="note">Currently {"night" if theme == "dark" else "day"}.
      The sun or moon on the device does the same thing, and the QR beside
      it opens these settings on a phone — no password to type.</span>
  </div>
</form>"""

    def _page_people(self) -> str:
        users = self._raw_config().get("users") or []
        now_ms = int(time.time() * 1000)
        rows = []
        for index, user in enumerate(users):
            state = self._person_state(user, now_ms)
            rows.append(ui.menu_item(
                f"/settings/person?i={index}",
                user.get("name") or f"Person {index + 1}", state["text"],
                lead=ui.icon("people"), badge=state["pill"],
                badge_kind=state["kind"]))
        rows.append(ui.menu_item(
            "/settings/person?i=new", "Add a person",
            "Another panel on the screen", lead=ui.icon("people")))
        return f"""<h1>People</h1>
<p class="lede">One panel on the screen each.</p>
{self._flash()}
{ui.menu(rows)}"""

    def _next_port(self, users: list[dict]) -> int:
        taken = {user.get("port") for user in users}
        taken.add(self.server.config.admin_port)
        port = config_mod.FIRST_USER_PORT
        while port in taken:
            port += 1
        return port

    def _page_person(self, index, *, form=None, banner: str = "") -> str:
        raw = self._raw_config()
        users = raw.get("users") or []
        adding = index == "new"
        user = {} if adding else dict(users[index])
        source = user.get("source") or {}

        def pick(key, saved):
            return (form or {}).get(key, saved)

        stype = pick("source", source.get("type") or "push")
        port = pick("port", user.get("port") or self._next_port(users))
        secret = (pick("api_secret", user.get("api_secret"))
                  or config_mod.readable_secret(16))
        th = user.get("thresholds") or {}
        control = "src"

        # The port and the push secret belong to the push source and to
        # nothing else — someone on twiist was being asked for a "Port
        # (Nightscout API)" they will never use. They stay in the form
        # (hidden inputs still submit, so the port round-trips) but they
        # are only *shown* for the source that needs them.
        push = (
            ui.row("URL for the uploader",
                   ui.copy_input("push_url", f"http://{self._lan_ip()}:{port}",
                                 input_id="push_url"), inline=False)
            + ui.row("API secret",
                     ui.copy_input("api_secret", secret, input_id="api_secret"),
                     inline=False,
                     hint="Enter both in Trio under Settings &rarr; Services"
                          " &rarr; Nightscout.")
            + "<details><summary>Port</summary>"
            + ui.row("Port", ui.text_input("port", port, kind="number",
                                           input_id="port"),
                     for_id="port",
                     hint="The uploader connects to this port on this device."
                          " Only change it if something else is using it.")
            + "</details>"
        )
        tidepool = (
            ui.row("Tidepool email",
                   ui.text_input("tp_email",
                                 pick("tp_email", source.get("email", "")
                                      if stype == "tidepool" else ""),
                                 kind="email", input_id="tp_email",
                                 extra='autocapitalize="none" autocorrect="off"'
                                       ' spellcheck="false"'),
                   inline=False, for_id="tp_email")
            # Stored credentials are never rendered back into the page:
            # blank means "keep what is saved".
            + ui.row("Tidepool password",
                     ui.password_input("tp_password", "",
                                       placeholder=self._secret_placeholder(
                                           stype == "tidepool",
                                           source.get("password")),
                                       input_id="tp_password"),
                     inline=False, for_id="tp_password")
        )
        ns_key = source.get("api_secret") or source.get("token") or ""
        nightscout = (
            ui.row("Nightscout address",
                   ui.text_input("ns_url",
                                 pick("ns_url", source.get("url", "")
                                      if stype == "nightscout" else ""),
                                 kind="url", placeholder="mysite.example.com",
                                 input_id="ns_url",
                                 extra='autocapitalize="none" autocorrect="off"'
                                       ' spellcheck="false"'),
                   inline=False, for_id="ns_url")
            + ui.row("API secret or token",
                     ui.password_input("ns_key", "",
                                       placeholder=self._secret_placeholder(
                                           stype == "nightscout", ns_key),
                                       input_id="ns_key"),
                     inline=False, for_id="ns_key",
                     hint="Either works — GlucoCube works out which.")
        )
        pull_extra = (
            ui.row("Check every",
                   ui.text_input("poll", pick("poll",
                                              source.get("poll_seconds", 60)),
                                 kind="number", input_id="poll"),
                   for_id="poll", hint="seconds")
            + '<button type="button" class="test secondary" data-needs-js'
              " hidden>Test connection</button>"
            + '<div class="banner" id="testresult" hidden></div>'
        )
        # Open when this person has overrides, and when a failed save is
        # being re-rendered with some — collapsing over what someone just
        # typed is how you lose it.
        typed = any((form or {}).get(f"th_{key}") for key in
                    ("low", "high", "urgent_low", "urgent_high"))
        ranges = "".join([
            '<details class="ranges"' + (" open" if th or typed else "") + ">",
            "<summary>Ranges just for this person</summary>",
            ui.row("Low / high", '<div class="pair">'
                   + ui.text_input("th_low", pick("th_low", _g(th.get("low"))),
                                   kind="number", placeholder="default")
                   + ui.text_input("th_high", pick("th_high",
                                                   _g(th.get("high"))),
                                   kind="number", placeholder="default")
                   + "</div>", inline=False),
            ui.row("Urgent low / high", '<div class="pair">'
                   + ui.text_input("th_urgent_low",
                                   pick("th_urgent_low",
                                        _g(th.get("urgent_low"))),
                                   kind="number", placeholder="default")
                   + ui.text_input("th_urgent_high",
                                   pick("th_urgent_high",
                                        _g(th.get("urgent_high"))),
                                   kind="number", placeholder="default")
                   + "</div>", inline=False,
                   hint="Blank uses the ranges everyone shares."),
            "</details>",
        ])
        remove = ""
        if not adding and len(users) > 1:
            remove = f"""
<form method="POST" action="/settings/person/remove?i={index}"
      onsubmit="return confirm('Remove this person from the display?')">
  <div class="actions">
    <button type="submit" class="danger">Remove this person</button>
    <span class="note">Their readings stay in the database — only the
      panel goes away.</span>
  </div>
</form>"""
        name = pick("name", user.get("name", ""))
        heading = user.get("name") or "Add a person"
        return f"""<h1>{ui.esc(heading)}</h1>
<p class="lede">{"They get their own panel, port and API secret."
                 if adding else "Where their readings come from, and the "
                 "ranges they are coloured against."}</p>
{banner}
<form method="POST" action="/settings/person?i={ui.esc(index)}"
      data-index="{ui.esc(index)}">
  {ui.row("Name", ui.text_input("name", name, input_id="name"),
          inline=False, for_id="name")}
  <label class="lbl">Where the data comes from</label>
  {ui.choice_cards("source", self.SOURCE_CARDS, stype, controls=control)}
  {ui.group(control, "push", push, current=stype)}
  {ui.group(control, "tidepool", tidepool, current=stype)}
  {ui.group(control, "nightscout", nightscout, current=stype)}
  {ui.group(control, ["tidepool", "nightscout"], pull_extra, current=stype)}
  {ranges}
  <div class="actions stick">
    <button type="submit">{"Add" if adding else "Save"} &amp; apply</button>
    <span class="note">Restarts the display, about five seconds.</span>
  </div>
</form>{remove}"""

    def _page_ranges(self) -> str:
        display = self._raw_config().get("display", {})
        return f"""<h1>Ranges</h1>
<p class="lede">What counts as in range, and where the numbers turn red.
Everyone shares these unless their own page overrides them.</p>
{self._flash()}
<form method="POST" action="/settings/ranges">
<fieldset><legend>mg/dL</legend>
  {ui.row("In range", '<div class="pair">'
          + ui.text_input("low", _g(display.get("low"), 70), kind="number")
          + ui.text_input("high", _g(display.get("high"), 180), kind="number")
          + "</div>", inline=False, hint="low and high")}
  {ui.row("Urgent", '<div class="pair">'
          + ui.text_input("urgent_low", _g(display.get("urgent_low"), 55),
                          kind="number")
          + ui.text_input("urgent_high", _g(display.get("urgent_high"), 250),
                          kind="number")
          + "</div>", inline=False,
          hint="outside these the whole panel turns red")}
  {ui.row("Stale after", ui.text_input(
      "stale_minutes", _g(display.get("stale_minutes"), 12), kind="number",
      input_id="stale_minutes"), for_id="stale_minutes",
      hint="minutes without a reading before the panel greys out")}
</fieldset>
<div class="actions stick">
  <button type="submit">Save &amp; apply</button>
  <span class="note">Restarts the display, about five seconds.</span>
</div>
</form>"""

    def _page_clock(self) -> str:
        display = self._raw_config().get("display", {})
        current = display.get("timezone", "")
        return f"""<h1>Clock</h1>
<p class="lede">Where the device is, so the clock and the times on the
chart are right. A device straight off the image has no zone set and
reads UTC.</p>
{self._flash()}
<div class="banner info" id="tzdetected" hidden>
  This phone says <b></b>.
  <button type="button" class="quiet">Use that</button>
</div>
<form method="POST" action="/settings/clock">
  {ui.row("Time zone", ui.select("timezone", timezone_options(), current,
                                 input_id="timezone"), inline=False,
          for_id="timezone")}
  <p class="note" id="tzpreview"></p>
  <div class="actions stick">
    <button type="submit">Save &amp; apply</button>
    <span class="note">Restarts the display, about five seconds.</span>
  </div>
</form>"""

    def _page_network(self) -> str:
        if not network.available():
            return ("<h1>Wi-Fi</h1><p>This device has no NetworkManager, so "
                    "its network is managed elsewhere.</p>")
        hotspot = network.hotspot_active()
        wifi = network.state()
        notices = [self._flash()]
        if hotspot:
            notices.append(ui.banner(
                "info", "Setup hotspot is on — pick your home network below."))
        if wifi.get("state") == "failed":
            notices.append(ui.banner(
                "err", f"Could not join <b>{ui.esc(wifi.get('ssid', ''))}</b> — "
                       f"{ui.esc(wifi.get('error', 'unknown error'))}"))
            if wifi.get("detail"):
                notices.append("<details><summary>Technical detail</summary>"
                               f"<pre class=\"detail\">{ui.esc(wifi['detail'])}"
                               "</pre></details>")
        elif wifi.get("state") == "joining":
            notices.append(ui.banner(
                "info", f"Joining <b>{ui.esc(wifi.get('ssid', ''))}</b>&hellip;"))
        elif wifi.get("state") == "ok" and not hotspot:
            notices.append(ui.banner(
                "ok", f"Connected to <b>{ui.esc(wifi.get('ssid', ''))}</b>"))
        if wifi.get("reboot_error"):
            notices.append(ui.banner(
                "err", "Could not restart automatically: "
                       f"{ui.esc(wifi['reboot_error'])} — power-cycle the "
                       "device to finish."))
        if wifi.get("hotspot_error"):
            notices.append(ui.banner(
                "err", "Setup hotspot could not start: "
                       f"{ui.esc(wifi['hotspot_error'])}"))

        networks = network.cached_networks()
        age = network.scan_age_seconds()
        if network.scan_in_progress():
            hint = "Scanning&hellip;"
        elif networks:
            when = ("just now" if age is None or age < 90
                    else f"{int(age // 60)} min ago")
            hint = f"{len(networks)} networks found, scanned {when}."
        else:
            hint = ("No scan results — use “Other network” and type the name."
                    if hotspot else "No networks found; try Rescan.")
        rescan = (
            '<form method="POST" action="/wifi/rescan">'
            '<button type="submit" class="quiet">Rescan</button></form>'
            if not hotspot else
            '<p class="note">The radio cannot scan while the setup hotspot is '
            'running, so this is the list from just before it started.</p>'
        )
        address = config_mod.admin_url(self._lan_ip(),
                                       self.server.config.admin_port)
        return f"""<h1>Wi-Fi</h1>
{''.join(notices)}
{ui.facts([("This device", ui.esc(address))])}
<form method="POST" action="/wifi">
  {ui.network_picker(networks, selected=wifi.get("ssid", ""))}
  {ui.row("Password", ui.password_input("wifi_password", "",
                                        input_id="wifi_password"),
          inline=False, for_id="wifi_password")}
  <p class="note">{hint}</p>
  <div class="actions"><button type="submit">Join network</button></div>
</form>
{rescan}"""

    def _update_status(self, state: dict) -> tuple[str, str]:
        """(banner kind, sentence) for whatever the last check found."""
        if not state.get("checked_at"):
            return "info", "Not checked yet — checks run every six hours."
        checked = time.strftime("%H:%M",
                                time.localtime(state["checked_at"] / 1000))
        if state.get("error"):
            return "err", (f"The check at {checked} did not work: "
                           f"{ui.esc(state['error'])}")
        notes = (f' — <a href="{ui.esc(state.get("url", ""))}">release notes</a>'
                 if state.get("url") else "")
        if state.get("available"):
            if state.get("rejoin"):
                return "warn", (
                    "This device is running a pre-release. The newest full "
                    f"release is <b>{ui.esc(state.get('latest', ''))}</b>; "
                    f"installing it steps back onto the standard channel"
                    f"{notes}.")
            return "ok", (f"<b>{ui.esc(state.get('latest', ''))}</b> is "
                          f"available, checked {checked}{notes}.")
        if not state.get("latest"):
            return "warn", ("Nothing has been published on this channel yet "
                            f"(checked {checked}).")
        return "info", f"Up to date, checked {checked}."

    def _page_updates(self) -> str:
        state = self.server.store.get_params(updater.PARAMS_KEY)
        channel = config_mod.normalize_channel(
            self.server.config.update_channel)
        kind, status = self._update_status(state)
        install = ""
        if state.get("available") and state.get("latest_tag"):
            install = f"""
  <form method="POST" action="/update/apply">
    <input type="hidden" name="tag" value="{ui.esc(state.get('latest_tag', ''))}">
    <button type="submit">Install {ui.esc(state.get('latest', ''))}</button>
  </form>"""
        running = ui.esc(updater.current_version())
        return f"""<h1>Updates</h1>
<p class="lede">Running GlucoCube <b>{running}</b>.</p>
{self._flash()}
{ui.banner(kind, status)}
<div class="actions">
  <form method="POST" action="/update/check">
    <button type="submit" class="secondary">Check now</button>
  </form>{install}
</div>
<h2>Release channel</h2>
<form method="POST" action="/settings/updates/channel">
  {ui.choice_cards("channel", self.CHANNEL_CARDS, channel)}
  <p class="note">Changing this installs the newest release on the channel
  you pick, right away — including stepping back onto the last full release
  when you leave Beta. The display restarts, which takes about a minute.</p>
  <div class="actions">
    <button type="submit" class="secondary">Switch channel</button>
  </div>
</form>
<p class="note">Updates install from GitHub releases. A release marked
<code>[force-update]</code> in its notes installs itself at the next
check, on whichever channel published it.</p>"""

    def _page_access(self) -> str:
        config = self.server.config
        password = config.admin_password
        mode = "on" if password else "off"
        link = config_mod.admin_url(
            self._lan_ip(), config.admin_port,
            f"/settings?key={password}" if password else "/settings")
        known = [("Username", "admin")] if password else []
        lede = ("This page and the dashboard need a password."
                if password else
                "This page and the dashboard are open to anyone on this "
                "network.")
        field = ui.row(
            "New password" if password else "Password",
            ui.password_input("admin_password", "",
                              placeholder="leave blank to keep the current one"
                                          if password else "",
                              input_id="admin_password"),
            inline=False, for_id="admin_password",
            hint="At least six characters. You will stay logged in on this "
                 "phone." + (" Leave it blank to keep the one in use."
                             if password else ""))
        # Said here rather than only in the card, because turning the
        # password off is the one choice on this page that cannot be
        # undone from somewhere else if the network turns out to be
        # shared.
        open_note = (
            '<p class="note">Fine on a home network you trust — the device'
            " is only reachable from it, and there is nothing to look up on"
            " a phone to get in. Not fine on a network guests, flatmates or"
            " an office share.</p>")
        return f"""<h1>Access</h1>
<p class="lede">{lede}</p>
{self._flash()}
{ui.facts(known)}
{ui.row("Current password", ui.copy_input("current", password,
                                          input_id="current"), inline=False,
        hint="The device's own screen shows this too, so you cannot lock "
             "yourself out.") if password else ""}
{ui.row("Link that opens settings without logging in",
        ui.copy_input("autolink", link, input_id="autolink"), inline=False,
        hint="The same link the QR code on the device carries. Anyone with "
             "it can change these settings.")}
<form method="POST" action="/settings/access">
  <label class="lbl">Getting in</label>
  {ui.choice_cards("mode", self.ACCESS_CARDS, mode, controls="access")}
  {ui.group("access", "on", field, current=mode)}
  {ui.group("access", "off", open_note, current=mode)}
  <div class="actions stick">
    <button type="submit">Save &amp; apply</button>
    <span class="note">Restarts the display, about five seconds.</span>
  </div>
</form>"""


    @staticmethod
    def _source_ready(source: dict) -> bool:
        """Same check start_pollers applies, so the page can say so."""
        kind = source.get("type")
        if kind == "tidepool":
            return bool(source.get("email") and source.get("password"))
        if kind == "nightscout":
            return bool(source.get("url"))
        return True

    @staticmethod
    def _updating_page(version: str) -> bytes:
        return ui.page(
            "Installing", f"<h1>Installing {ui.esc(version)}&hellip;</h1>"
            "<p>The display restarts on the new version — this page reloads"
            " in about a minute.</p>",
            refresh="45;url=/settings/updates").encode()

    @staticmethod
    def _error_page(message: str, back: str) -> bytes:
        return ui.page(
            "That did not save",
            f"<h1>That did not save</h1><p>{ui.esc(message)}</p>"
            f'<p><a href="{ui.esc(back)}">Back</a></p>').encode()

    @staticmethod
    def _applying_page(back: str, heading: str = "Saved",
                       message: str = "") -> bytes:
        """The interstitial for the restart every config change causes.

        It waits for the *new* process to answer rather than guessing at
        a number of seconds: the old fixed reload landed on a connection
        error about half the time.
        """
        script = """<script>
(function(){
  var next = %s, gone = false;
  setInterval(function(){
    fetch('/api/health.json', {cache: 'no-store'}).then(function(r){
      if (!r.ok) throw new Error(r.status);
      if (gone) location.replace(next);
    }).catch(function(){ gone = true; });
  }, 1200);
})();
</script>""" % json.dumps(back)
        body = (f"<h1>{ui.esc(heading)}</h1><p>"
                + (message or "The display is restarting on the new "
                              "settings.")
                + '</p><p class="note">This page comes back by itself.</p>')
        return ui.page(heading, body, script=script,
                       refresh=f"12;url={back}").encode()

    def _settings_get(self, path: str) -> None:
        back, script = "/settings", ""
        if path == "/settings/screen":
            title, body, script = "The screen", self._page_screen(), SCREEN_SCRIPT
        elif path == "/settings/people":
            title, body = "People", self._page_people()
        elif path == "/settings/person":
            index = self._person_index()
            if index is None:
                self._send(b"", "text/html", 303,
                           {"Location": "/settings/people"})
                return
            title, script = "Person", PERSON_SCRIPT
            body, back = self._page_person(index), "/settings/people"
        elif path == "/settings/ranges":
            title, body = "Ranges", self._page_ranges()
        elif path == "/settings/network":
            title, body, script = "Wi-Fi", self._page_network(), WIFI_SCRIPT
        elif path == "/settings/clock":
            title, body, script = "Clock", self._page_clock(), CLOCK_SCRIPT
        elif path == "/settings/updates":
            title, body = "Updates", self._page_updates()
        elif path == "/settings/access":
            title, body = "Access", self._page_access()
        else:
            self._send(b"", "text/html", 303, {"Location": "/settings"})
            return
        self._send(ui.page(f"GlucoCube — {title}", body, nav=True, back=back,
                           script=script).encode(),
                   "text/html; charset=utf-8")

    def _person_index(self):
        """Which person this request is about: an index, "new", or None."""
        value = self._query().get("i", "new")
        if value == "new":
            return "new"
        try:
            index = int(value)
        except ValueError:
            return None
        users = self._raw_config().get("users") or []
        return index if 0 <= index < len(users) else None

    def do_POST(self):
        post_path = self.path.split("?")[0]
        if (not self._authorized()
                and not onboarding.open_without_login(post_path)):
            self._deny()
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        if post_path == "/api/source/test":
            self._test_source(raw_body)
            return
        # keep_blank_values matters: blank means "unchanged" for every
        # credential field, and a dropped key cannot say that.
        form = {
            k: v[0]
            for k, v in parse_qs(raw_body.decode(),
                                 keep_blank_values=True).items()
        }
        if onboarding.handles(post_path):
            onboarding.do_post(self, post_path, form)
            return
        if post_path == "/display/theme":
            theme = form.get("theme")
            if theme in ("dark", "light"):
                # The display picks this up on its next frame.
                self.server.store.set_params("__display", {"theme": theme})
            back = form.get("back", "/settings")
            if not back.startswith("/settings"):
                back = "/settings"
            self._send(b"", "text/html", 303, {"Location": back})
            return
        if post_path in self.SECTION_SAVES:
            self._save_section(post_path, form)
            return
        if post_path == "/settings/person":
            self._post_person(form)
            return
        if post_path == "/settings/person/remove":
            self._post_person_remove()
            return
        if post_path == "/settings/updates/channel":
            self._post_channel(form)
            return
        if post_path == "/wifi":
            # A typed name always wins: it is only filled in when the
            # person chose "Other network".
            ssid = (form.get("wifi_other_ssid", "").strip()
                    or form.get("wifi_ssid", "").strip())
            if ssid == "__other__":
                ssid = ""
            password = form.get("wifi_password", "")
            hidden = bool(form.get("wifi_hidden"))
            if not ssid:
                self._send(ui.page(
                    "No network chosen",
                    "<h1>No network chosen</h1><p>Tap a network in the list, "
                    "or choose <b>Other network</b> and type its name.</p>"
                    '<p><a href="/settings">Back to settings</a></p>',
                ).encode(), "text/html; charset=utf-8", 400)
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
            self._send(self._joining_page(ssid), "text/html; charset=utf-8")
            return
        if post_path == "/wifi/rescan":
            # Only meaningful with the radio in station mode; in AP mode
            # the scan cache from before the hotspot came up is all there is.
            # The scan runs in the background and the page asks when it is
            # done, rather than the handler sleeping on it.
            network.refresh_scan_async(force=True)
            self._send(b"", "text/html", 303,
                       {"Location": "/settings/network?scanning=1"})
            return
        if post_path == "/update/check":
            state = updater.check_and_maybe_force(
                self.server.store, self.server.config.update_channel)
            if state.get("forcing"):
                self._send(self._updating_page(state.get("latest", "")),
                           "text/html; charset=utf-8")
            else:
                self._send(b"", "text/html", 303,
                           {"Location": "/settings/updates?msg=checked"})
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
                self._send(ui.page(
                    "Update failed",
                    f"<h1>Update failed</h1><p>{ui.esc(detail)}</p>"
                    '<p><a href="/settings/updates">Back to updates</a></p>',
                ).encode(), "text/html; charset=utf-8", 500)
            return
        self._send(b"not found", "text/plain", 404)

    # ---- settings: saving ----

    # Each of these owns one page, and each ends the same way: validate,
    # write config.json atomically, restart. Nothing takes effect without
    # a restart — ports, pollers and the display all read the file once,
    # at startup — so the page that follows waits for the new process.
    SECTION_SAVES = {
        "/settings/ranges": "_save_ranges",
        "/settings/clock": "_save_clock",
        "/settings/access": "_save_access",
    }

    def _save_section(self, path: str, form: dict) -> None:
        try:
            getattr(self, self.SECTION_SAVES[path])(form)
        except Exception as exc:  # noqa: BLE001 - shown, never a crash
            self._send(self._error_page(str(exc), path),
                       "text/html; charset=utf-8", 400)
            return
        log.info("Config saved from web admin (%s); restarting", path)
        self._send(self._applying_page(f"{path}?msg=saved"),
                   "text/html; charset=utf-8")
        restart_soon()

    def _post_person(self, form: dict) -> None:
        index = self._person_index()
        if index is None:
            self._send(b"", "text/html", 303, {"Location": "/settings/people"})
            return
        try:
            self._save_person(form, index)
        except Exception as exc:  # noqa: BLE001 - re-render, keep the typing
            self._send(ui.page(
                "GlucoCube — Person",
                self._page_person(index, form=form,
                                  banner=ui.banner("err", ui.esc(str(exc)))),
                nav=True, back="/settings/people", script=PERSON_SCRIPT,
            ).encode(), "text/html; charset=utf-8", 400)
            return
        log.info("Person saved from web admin; restarting")
        self._send(self._applying_page("/settings/people?msg=saved"),
                   "text/html; charset=utf-8")
        restart_soon()

    def _post_person_remove(self) -> None:
        index = self._person_index()
        raw = self._raw_config()
        users = raw.get("users") or []
        if index in (None, "new"):
            self._send(b"", "text/html", 303, {"Location": "/settings/people"})
            return
        if len(users) <= 1:
            self._send(self._error_page(
                "The display needs at least one person.",
                "/settings/people"), "text/html; charset=utf-8", 400)
            return
        users.pop(index)
        raw["users"] = users
        try:
            config_mod.write_atomic(raw, self.server.config_path)
        except Exception as exc:  # noqa: BLE001
            self._send(self._error_page(str(exc), "/settings/people"),
                       "text/html; charset=utf-8", 400)
            return
        log.info("Person removed from web admin; restarting")
        self._send(self._applying_page("/settings/people?msg=removed"),
                   "text/html; charset=utf-8")
        restart_soon()

    def _post_channel(self, form: dict) -> None:
        """Change channel, and move this device onto it.

        Recording the choice is the easy half. The half that matters is
        that the device then actually runs what the channel offers —
        forwards onto a pre-release, or back onto the last full release.
        """
        channel = config_mod.normalize_channel(form.get("channel"))
        raw = self._raw_config()
        raw.setdefault("updates", {})["channel"] = channel
        try:
            config_mod.write_atomic(raw, self.server.config_path)
        except Exception as exc:  # noqa: BLE001
            self._send(self._error_page(str(exc), "/settings/updates"),
                       "text/html; charset=utf-8", 400)
            return
        # The checker thread holds this same object, so the next scheduled
        # check follows the new channel without waiting for a restart.
        self.server.config.update_channel = channel
        log.info("Update channel set to %s", channel)
        state = updater.check_and_switch(self.server.store, channel)
        if state.get("switching"):
            self._send(self._updating_page(state.get("latest", "")),
                       "text/html; charset=utf-8")
            return
        if state.get("error"):
            self._send(self._error_page(
                f"The channel is now {config_mod.CHANNEL_LABELS[channel]}, "
                f"but the check failed: {state['error']}",
                "/settings/updates"), "text/html; charset=utf-8", 502)
            return
        msg = "channel" if state.get("latest") else "nothing"
        self._send(b"", "text/html", 303,
                   {"Location": f"/settings/updates?msg={msg}"})

    @staticmethod
    def _joining_page(ssid: str) -> bytes:
        return ui.page("Joining " + ssid, f"""
<h1>Joining {ui.esc(ssid)}&hellip;</h1>
<p>This takes up to a minute. The setup hotspot drops while the device
tries to connect, so your phone will lose this page — that is expected.</p>
<h2>If it worked</h2>
<p>The device restarts and its screen shows its new address. Put your phone
back on your home Wi-Fi, then open that address.</p>
<h2>If it did not</h2>
<p>The setup hotspot comes back within a minute or two. Rejoin it and the
reason is shown at the top of the Wi-Fi page — the device's own screen
shows it too.</p>""").encode()

    def _test_source(self, raw_body: bytes) -> None:
        """Check one person's credentials without saving anything."""
        try:
            body = json.loads(raw_body or b"{}")
        except ValueError:
            self._send(b'{"ok": false, "message": "bad request"}',
                       "application/json", 400)
            return
        source = {"type": body.get("type"),
                  "email": (body.get("email") or "").strip(),
                  "password": body.get("password") or "",
                  "url": (body.get("url") or "").strip(),
                  "api_secret": (body.get("api_secret") or "").strip()}
        # Blank means "unchanged" in the form, so fall back to what is
        # saved — otherwise testing an untouched person always fails.
        stored = self._stored_source(body.get("index"))
        if stored.get("type") == source["type"]:
            for field in ("email", "password", "url", "api_secret"):
                if not source[field]:
                    source[field] = stored.get(field) or (
                        stored.get("token") if field == "api_secret" else "")
        result = verify.source(source)
        self._send(json.dumps(result.as_dict()).encode(), "application/json")

    def _stored_source(self, index) -> dict:
        try:
            raw = json.loads(open(self.server.config_path).read())
            return raw["users"][int(index)].get("source") or {}
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return {}

    def _save_person(self, form: dict, index) -> None:
        raw = self._raw_config()
        users = raw.get("users") or []
        adding = index == "new"
        prior = {} if adding else dict(users[index])
        prior_source = prior.get("source") or {}
        name = form.get("name", "").strip()
        if not name:
            raise ValueError("A name is needed — it labels the panel on "
                             "the screen.")
        # The port is only shown for push people; for everyone else it
        # arrives from a hidden input. assign_ports() fills any gap.
        port = (form.get("port") or "").strip()
        user = {
            "name": name,
            "port": int(port) if port.isdigit() else prior.get("port"),
            "api_secret": (form.get("api_secret", "").strip()
                           or prior.get("api_secret")
                           or secrets_mod.token_hex(12)),
        }
        thresholds = {}
        for key, label in (("low", "Low"), ("high", "High"),
                           ("urgent_low", "Urgent low"),
                           ("urgent_high", "Urgent high")):
            value = _number(form, f"th_{key}", label)
            if value is not None:
                thresholds[key] = value
        if thresholds:
            _check_ranges({**self._range_defaults(), **thresholds})
            user["thresholds"] = thresholds
        kind = form.get("source", "push")
        poll = max(15, int(_number(form, "poll", "Check every") or 60))
        if kind == "tidepool":
            # Blank means "keep what is saved": stored secrets are never
            # rendered back into the page, so an untouched field must not
            # wipe them.
            source = {
                "type": "tidepool",
                "email": form.get("tp_email", "").strip(),
                "password": (form.get("tp_password", "")
                             or self._kept_secret(prior_source, "tidepool",
                                                  "password")),
                "poll_seconds": poll,
            }
            if not (source["email"] and source["password"]):
                raise ValueError("Both the Tidepool email and password are "
                                 "needed.")
            user["source"] = source
        elif kind == "nightscout":
            url = form.get("ns_url", "").strip()
            if not url:
                raise ValueError("The Nightscout site address is needed.")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            # The poller auto-detects whether the key is a classic API
            # secret or an access token, so one field covers both.
            user["source"] = {
                "type": "nightscout",
                "url": url,
                "api_secret": (form.get("ns_key", "").strip()
                               or self._kept_secret(prior_source, "nightscout",
                                                    "api_secret", "token")),
                "poll_seconds": poll,
            }
        if adding:
            users.append(user)
        else:
            users[index] = user
        # Ports stay unique even though most people never see one; a
        # duplicate would make config.load() reject the file at boot.
        config_mod.assign_ports(users, reserved={self.server.config.admin_port})
        raw["users"] = users
        config_mod.write_atomic(raw, self.server.config_path)
        # Everything in the database is keyed by name, so a rename would
        # otherwise read as "this person has never had a reading" — which
        # is exactly what happens the first time someone replaces the
        # "Person A" the image ships with.
        previous = prior.get("name")
        if previous and previous != name:
            self.server.store.rename_user(previous, name)

    def _range_defaults(self) -> dict:
        display = self._raw_config().get("display", {})
        return {"low": float(display.get("low", 70)),
                "high": float(display.get("high", 180)),
                "urgent_low": float(display.get("urgent_low", 55)),
                "urgent_high": float(display.get("urgent_high", 250))}

    def _save_ranges(self, form: dict) -> None:
        raw = self._raw_config()
        display = raw.setdefault("display", {})
        values = {}
        for key, label in (("low", "The low"), ("high", "The high"),
                           ("urgent_low", "The urgent low"),
                           ("urgent_high", "The urgent high"),
                           ("stale_minutes", "Stale after")):
            value = _number(form, key, label)
            if value is not None:
                values[key] = value
        if values.get("stale_minutes", 1) <= 0:
            raise ValueError("Stale after has to be at least one minute.")
        _check_ranges({**self._range_defaults(), **values})
        display.update(values)
        config_mod.write_atomic(raw, self.server.config_path)

    def _save_clock(self, form: dict) -> None:
        raw = self._raw_config()
        asked = form.get("timezone", "")
        timezone = config_mod.canonical_timezone(asked)
        if asked.strip() and not timezone:
            raise ValueError(f"{asked} is not a time zone this device knows "
                             "about.")
        # Set even when blank, so it can be cleared back to the system's.
        raw.setdefault("display", {})["timezone"] = timezone
        config_mod.write_atomic(raw, self.server.config_path)

    def _save_access(self, form: dict) -> None:
        raw = self._raw_config()
        admin = raw.setdefault("admin", {})
        if form.get("mode", "on").strip() == "off":
            # password_off is what tells the settings hub this is a choice
            # and not a device somebody forgot to finish setting up, so it
            # stops warning about it.
            admin["password"] = ""
            admin["password_off"] = True
            config_mod.write_atomic(raw, self.server.config_path)
            return
        password = form.get("admin_password", "").strip()
        current = self.server.config.admin_password
        if not password and not current:
            raise ValueError("Type a password, or choose No password.")
        if password and len(password) < 6:
            raise ValueError("The password must be at least six characters.")
        password = password or current
        admin["password"] = password
        admin.pop("password_off", None)
        config_mod.write_atomic(raw, self.server.config_path)
        # Otherwise this browser's cookie stops matching the moment the new
        # process starts, and the page it was just on is gone.
        self._cookie_value = password

    @staticmethod
    def _kept_secret(prior_source: dict, kind: str, *keys: str) -> str:
        if prior_source.get("type") != kind:
            return ""
        for key in keys:
            if prior_source.get(key):
                return prior_source[key]
        return ""


def restart_soon(delay: float = 0.8) -> None:
    """Exit once the response has flushed; systemd restarts us (Restart=always).

    Config changes take effect by starting over: ports, pollers and the
    display all read the file once at startup.
    """
    threading.Timer(delay, lambda: os._exit(0)).start()


def start_admin(config: Config, config_path, store: Store) -> AdminServer | None:
    if not config.admin_port:
        return None
    server = AdminServer(config, config_path, store)
    thread = threading.Thread(target=server.serve_forever, name="webadmin", daemon=True)
    thread.start()
    log.info("Web admin listening on port %d", config.admin_port)
    return server
