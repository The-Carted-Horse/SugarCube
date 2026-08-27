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
// Keep every live screenshot on the page current (the hub hero and the
// full preview are the same image), and say how fresh it is — "live" on
// its own is a claim, "updated 2s ago" is the evidence.
(function(){
  var last = Date.now();
  function shots(){ return document.querySelectorAll('img.live'); }
  shots().forEach(function(img){
    img.addEventListener('load', function(){ last = Date.now(); });
  });
  setInterval(function(){
    shots().forEach(function(img){ img.src = '/screen.png?t=' + Date.now(); });
  }, 5000);
  setInterval(function(){
    var age = Math.round((Date.now() - last) / 1000);
    document.querySelectorAll('.shotage').forEach(function(el){
      el.textContent = age < 2 ? 'just now' : 'updated ' + age + 's ago';
    });
  }, 1000);
})();
</script>"""

PERSON_SCRIPT = """<script>
// "Test this login": check the credentials before saving, rather than
// finding out from the sync log hours later that a letter was wrong. The
// answer appears inside the source card that was tested, under the fields
// it is about.
document.addEventListener('click', async (event) => {
  const button = event.target.closest('button.test');
  if (!button) return;
  event.preventDefault();
  const form = button.closest('form');
  const card = button.closest('.optbody') || form;
  const out = card.querySelector('.testresult');
  const mark = out ? out.querySelector('.mark') : null;
  const msg = out ? out.querySelector('.msg') : null;
  const value = (name) => {
    const el = form.querySelector('[name="' + name + '"]');
    return el ? el.value : '';
  };
  const picked = form.querySelector('[name=source]:checked');
  const say = (kind, glyph, text) => {
    if (!out) return;
    out.hidden = false;
    out.className = 'testresult ' + kind;
    if (mark) mark.textContent = glyph;
    if (msg) msg.textContent = text;
  };
  say('', '\u00b7', 'Testing\u2026');
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
    say(result.ok ? 'ok' : 'err', result.ok ? '\u2713' : '\u00d7',
        result.message);
    // Say which field was refused, not just that something was.
    const secret = card.querySelector('input[type=password]');
    if (secret) {
      if (result.ok) secret.removeAttribute('aria-invalid');
      else secret.setAttribute('aria-invalid', 'true');
    }
  } catch (err) {
    say('err', '\u00d7', 'Could not run the test.');
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
  // The big readout is what the device believes the time is; keep it
  // honest rather than letting it go stale on an open page.
  var readout = document.getElementById('devclock');
  function tick(){
    if (!readout) return;
    var zone = readout.dataset.zone;
    if (!zone) return;
    try {
      var now = new Date();
      readout.querySelector('.clock').textContent =
        now.toLocaleTimeString(undefined, {timeZone: zone, hour: '2-digit',
                                           minute: '2-digit', hour12: false});
      readout.querySelector('.weekday').textContent =
        now.toLocaleDateString(undefined, {timeZone: zone, weekday: 'long'});
    } catch (err) {}
  }
  document.addEventListener('change', preview);
  preview();
  tick();
  setInterval(preview, 30000);
  setInterval(tick, 10000);
})();
</script>"""


LOG_BODY = """<h1>Sync log</h1>
<p class="stepno">Most recent first &#183; cleared on restart</p>
<div id="rows"><p class="note">Loading&hellip;</p></div>"""

# A table asks you to read across four columns to find out whether
# anything is wrong. A list can say it in the colour of one dot, and put
# the failures against the edge of the page where they are hard to scroll
# past.
LOG_SCRIPT = """<script>
function logRow(e){
  const t = new Date(e.ts).toLocaleTimeString(undefined,
    {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const who = [e.user, e.source].filter(Boolean).map(esc).join(' \u00b7 ');
  return `<div class="logrow${e.ok ? '' : ' bad'}">` +
    `<span class="t">${esc(t)}</span>` +
    `<span class="dot ${e.ok ? 'ok' : 'err'}" aria-hidden="true"></span>` +
    `<span class="body"><span class="who">${who}</span>` +
    `<span class="msg">${esc(e.message)}</span></span></div>`;
}
async function refreshLog(){
  try {
    const r = await fetch('/api/log.json', {cache:'no-store'});
    const d = await r.json();
    document.getElementById('rows').innerHTML = d.entries.length
      ? d.entries.map(logRow).join('')
      : '<p class="note">Nothing has synced yet.</p>';
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
                           back="/settings", script=LOG_SCRIPT)
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
    # The glyphs the physical screen draws and the dashboard prints, so a
    # reading means the same thing wherever it is read.
    ARROWS = {"DoubleUp": "\u2191\u2191", "SingleUp": "\u2191",
              "FortyFiveUp": "\u2197", "Flat": "\u2192",
              "FortyFiveDown": "\u2198", "SingleDown": "\u2193",
              "DoubleDown": "\u2193\u2193"}
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
            return "unchanged — type to replace"
        return ""

    def _thresholds_for(self, user: dict) -> dict:
        """This person's colour boundaries: the shared ones, then theirs."""
        display = self.server.config.display
        bands = {"low": float(display.low), "high": float(display.high),
                 "urgent_low": float(display.urgent_low),
                 "urgent_high": float(display.urgent_high)}
        for key, value in (user.get("thresholds") or {}).items():
            if key in bands and value not in (None, ""):
                try:
                    bands[key] = float(value)
                except (TypeError, ValueError):
                    pass
        return bands

    @staticmethod
    def _tone(sgv, stale: bool, bands: dict) -> str:
        """The class that paints a reading the colour the screen paints it.

        Deliberately the same ladder as display.glucose_color: a number
        that is amber on the device must not be green on the phone.
        """
        if sgv is None or stale:
            return "v-stale"
        if sgv <= bands["urgent_low"] or sgv >= bands["urgent_high"]:
            return "v-urgent"
        if sgv < bands["low"]:
            return "v-low"
        if sgv > bands["high"]:
            return "v-high"
        return "v-ok"

    def _last_failure(self, name: str) -> str:
        """The newest sync event for this person, if it was a failure.

        "Nothing is arriving" is not a diagnosis. The log already knows
        that Tidepool said 401; the person's own page should say so
        rather than making someone go and find it.
        """
        for entry in synclog.recent():
            if entry.get("user") != name:
                continue
            return "" if entry.get("ok") else (entry.get("message") or "")
        return ""

    def _person_state(self, user: dict, now_ms: int) -> dict:
        """How this person is doing, in the words every list uses.

        One place, because the hub, the people list and the person page
        all have to agree about whether something needs attention — and
        now about what colour the number is, too.
        """
        name = user.get("name", "")
        source = user.get("source") or {}
        label = self.SOURCE_NAMES.get(source.get("type") or "push", "")
        state = {"label": label, "value": "", "arrow": "", "delta": "",
                 "age": "", "tone": "v-stale", "trend": "", "headline": "",
                 "text": "", "short": "", "pill": "", "kind": "err"}
        if source.get("type") and not self._source_ready(source):
            return {**state, "text": f"{label} — credentials missing",
                    "short": "needs setup", "pill": "needs setup",
                    "kind": "err",
                    "headline": "No credentials yet, so nothing is arriving"}
        snap = self.server.store.snapshot(name)
        failure = self._last_failure(name)
        if not snap.sgv_date or snap.sgv is None:
            return {**state, "text": f"{label} — nothing has arrived yet",
                    "short": "no data", "pill": "", "kind": "warn",
                    "headline": failure or "Nothing has arrived yet"}
        minutes = max(0, int((now_ms - snap.sgv_date) / 60000))
        when = ("just now" if minutes < 1
                else f"{minutes}m ago" if minutes < 120
                else f"{minutes // 60}h ago")
        stale = minutes > self.server.config.display.stale_minutes
        bands = self._thresholds_for(user)
        arrow = self.ARROWS.get(snap.direction or "", "")
        delta = ""
        if snap.delta is not None:
            step = round(snap.delta)
            delta = (f"+{step:.0f}" if step > 0
                     else f"\u2212{abs(step):.0f}" if step < 0 else "0")
        reading = f"{snap.sgv:.0f}"
        headline = (f"{ui.esc(failure)} \u00b7 last reading {when}" if failure
                    else f"Arriving \u00b7 <b>{reading} mg/dL</b>, {when}")
        return {
            "label": label, "value": reading, "arrow": arrow, "delta": delta,
            "trend": " ".join(part for part in (arrow, delta) if part),
            "age": when, "tone": self._tone(snap.sgv, stale, bands),
            "text": f"{label} — {reading} mg/dL, {when}",
            "short": when, "pill": "stale" if stale else "",
            "kind": "err" if failure else "warn" if stale else "ok",
            "headline": headline,
        }

    # ---- settings: the hub ----

    def _page_hub(self) -> str:
        """The hub, read as a status report you occasionally tap.

        Every row leads with what is true — "Sabanis", "07:57", "124" —
        and only then with the page that would change it. Someone opening
        this at 3am usually wants an answer, not a table of contents.
        """
        raw = self._raw_config()
        config = self.server.config
        display = raw.get("display", {})
        users = raw.get("users") or []
        update = self.server.store.get_params(updater.PARAMS_KEY)
        now_ms = int(time.time() * 1000)
        wifi_up = network.available()
        hotspot = network.hotspot_active_cached() if wifi_up else False
        wifi = network.state() if wifi_up else {}
        theme = self._theme()
        states = [(user, self._person_state(user, now_ms)) for user in users]

        notices = [self._flash()]
        if update.get("available") and update.get("rejoin"):
            notices.append(ui.banner(
                "warn", "This device is on a pre-release, but set to the "
                "standard channel \u2014 put it back on a full release",
                href="/settings/updates"))
        elif update.get("available"):
            notices.append(ui.banner(
                "warn", f"<b>{ui.esc(update.get('latest', ''))}</b> is ready "
                "to install \u2014 see what changed",
                href="/settings/updates"))
        if hotspot:
            notices.append(ui.banner(
                "info", "The setup hotspot is on, so this device has no "
                "internet yet \u2014 join a network",
                href="/settings/network"))
        elif wifi.get("state") == "failed":
            notices.append(ui.banner(
                "err", f"Could not join <b>{ui.esc(wifi.get('ssid', ''))}</b>"
                " \u2014 try again", href="/settings/network"))
        # A person with no credentials is the one fault the hub cannot
        # show in a row: their row has no reading to colour.
        for index, (user, state) in enumerate(states):
            if state["pill"] == "needs setup":
                notices.append(ui.banner(
                    "err", f"<b>{ui.esc(user.get('name') or 'Someone')}</b> has "
                    "no credentials yet, so nothing is arriving",
                    href=f"/settings/person?i={index}"))
        if not config.admin_password:
            notices.append(ui.banner(
                "warn", "Anyone on this network can change these settings "
                "\u2014 set a password", href="/settings/access"))

        # ---- who it's for ----
        people = []
        for index, (user, state) in enumerate(states):
            name = user.get("name") or f"Person {index + 1}"
            label = f"{name} \u00b7 {state['label']}" if state["label"] else name
            if state["value"]:
                value_html = ui.reading(state["value"], arrow=state["arrow"],
                                        age=state["age"], tone=state["tone"])
            else:
                value_html = ('<span class="val v-stale">'
                              f'{ui.esc(state["short"] or "no data")}</span>')
            people.append(ui.menu_item(
                f"/settings/person?i={index}", label, value_html=value_html,
                lead=ui.dot(state["kind"])))
        people.append(ui.menu_errand("/settings/person?i=new", "Add a person",
                                     plus=True))

        # ---- the display ----
        has_shot = os.path.isfile(SCREEN_PNG)
        colours = "Night colours" if theme == "dark" else "Day colours"
        hero = ""
        if has_shot:
            hero = (
                '<a class="hero" href="/settings/screen">'
                '<img class="screen live" src="/screen.png"'
                ' alt="what the display shows now" onerror="this.hidden=true">'
                f'<span class="cap">{ui.dot("ok")}Live \u00b7 '
                f'{ui.esc(colours.lower())}<span class="fill"></span>'
                '<span class="go">Change &rsaquo;</span></span></a>')
        screen_rows = []
        if not hero:
            # No screenshot yet (the display has not drawn one, or is off):
            # the hero has nothing to show, so the row carries the door.
            screen_rows.append(ui.menu_item("/settings/screen", "The screen",
                                            colours))
        low, high = _g(display.get("low"), 70), _g(display.get("high"), 180)
        urgent_low = _g(display.get("urgent_low"), 55)
        urgent_high = _g(display.get("urgent_high"), 250)
        screen_rows.append(ui.menu_item(
            "/settings/ranges", "Ranges",
            value_html=f'<span class="val">{ui.esc(low)}\u2013{ui.esc(high)} '
                       f'<small>\u00b7 urgent {ui.esc(urgent_low)} / '
                       f'{ui.esc(urgent_high)}</small></span>'))
        zone = display.get("timezone") or ""
        screen_rows.append(ui.menu_item(
            "/settings/clock", "Clock",
            value_html=f'<span class="val">{time.strftime("%H:%M")}'
                       + (f' <small>\u00b7 {ui.esc(zone.replace("_", " "))}'
                          "</small>" if zone else
                          ' <small>\u00b7 no zone set</small>')
                       + "</span>"))

        # ---- this device ----
        device_rows = []
        if wifi_up:
            trail = ""
            if hotspot:
                wifi_value = "Setup hotspot is on"
            elif wifi.get("state") == "ok" and wifi.get("ssid"):
                wifi_value = wifi["ssid"]
                signal = next((net.get("signal")
                               for net in network.cached_networks()
                               if net.get("ssid") == wifi["ssid"]), None)
                if signal is not None:
                    trail = ui.signal_bars(int(signal))
            elif wifi.get("state") == "failed":
                wifi_value = f"Could not join {wifi.get('ssid', '')}"
            else:
                wifi_value = (f"{len(network.cached_networks())} networks "
                              "nearby")
            device_rows.append(ui.menu_item("/settings/network", "Wi-Fi",
                                            wifi_value, trail=trail))
        channel = config_mod.normalize_channel(config.update_channel)
        channel_label = config_mod.CHANNEL_LABELS[channel]
        running = updater.current_version()
        if update.get("rejoin"):
            updates_value, badge = "On a pre-release", "Rejoin"
        elif update.get("available"):
            updates_value = f"{update.get('latest', '')} ready to install"
            badge = "New"
        elif update.get("checked_at"):
            updates_value, badge = f"{running} \u00b7 up to date", ""
        else:
            updates_value, badge = running, ""
        device_rows.append(ui.menu_item(
            "/settings/updates", f"Updates \u00b7 {channel_label} channel",
            updates_value,
            trail=f'<span class="pill warn">{ui.esc(badge)}</span>'
                  if badge else ""))
        device_rows.append(ui.menu_item(
            "/settings/access", "Access",
            value_html='<span class="val">Password set '
                       "<small>\u00b7 admin</small></span>"
                       if config.admin_password else
                       '<span class="val v-low">No password set</span>'))

        address = config_mod.admin_url(self._lan_ip(), config.admin_port)
        return f"""{ui.eyebrow(f"GlucoCube {running}")}
<h1>Settings</h1>
<p class="meta">{ui.esc(address)}</p>
{hero}
{''.join(notices)}
{ui.rule("Who it's for")}
{ui.menu(people)}
{ui.rule("The display")}
{ui.menu(screen_rows)}
{ui.rule("This device")}
{ui.menu(device_rows)}
{ui.rule("More")}
{ui.menu([
    ui.menu_errand("/setup?again=1", "Run guided setup again"),
    ui.menu_errand("/log", "Sync log"),
])}"""

    # ---- settings: one page per thing ----

    def _page_screen(self) -> str:
        theme = self._theme()

        def switch(to: str, label: str) -> str:
            return (f'<form method="POST" action="/display/theme">'
                    f'<input type="hidden" name="theme" value="{to}">'
                    '<input type="hidden" name="back" value="/settings/screen">'
                    f'<button type="submit">{label}</button></form>')

        colours = ui.segmented([
            (theme == "light", "Day &#9788;", switch("light", "Day &#9788;")),
            (theme == "dark", "Night &#9790;", switch("dark", "Night &#9790;")),
        ])
        return f"""<h1>The screen</h1>
<p class="lede">Exactly what the device is showing, refreshed every few
seconds.</p>
<img class="screen live" id="screen" src="/screen.png"
     alt="what the display shows" onerror="this.hidden=true">
<p class="livecap">{ui.dot("ok")}Live &#183;
<span class="shotage">just now</span></p>
{ui.rule("Colours")}
{colours}
<p class="note">Applies immediately &mdash; no restart. The sun or moon on
the device does the same thing, and the QR beside it opens these settings
on a phone with no password to type.</p>"""

    def _page_people(self) -> str:
        users = self._raw_config().get("users") or []
        now_ms = int(time.time() * 1000)
        cards = []
        for index, user in enumerate(users):
            state = self._person_state(user, now_ms)
            cards.append(ui.person_card(
                f"/settings/person?i={index}",
                user.get("name") or f"Person {index + 1}",
                source=state["label"], value=state["value"],
                trend=ui.esc(state["trend"]), age=state["age"],
                tone=state["tone"], dot_kind=state["kind"],
                note=state["short"] or "no data"))
        add = ('<a class="item quiet dashed" href="/settings/person?i=new">'
               '<span class="plus" aria-hidden="true">+</span>'
               '<span class="body">Add a person</span>'
               '<span class="chev" aria-hidden="true">&rsaquo;</span></a>')
        return f"""<h1>People</h1>
<p class="lede">One panel on the screen each.</p>
{self._flash()}
{''.join(cards)}
{add}"""

    def _next_port(self, users: list[dict]) -> int:
        taken = {user.get("port") for user in users}
        taken.add(self.server.config.admin_port)
        port = config_mod.FIRST_USER_PORT
        while port in taken:
            port += 1
        return port

    def _page_person(self, index, *, form=None, banner: str = "") -> str:
        """One person, and everything that decides what their panel says.

        Credentials live inside the source card that needs them: the
        answer to "where do I type the password?" should be "in the thing
        you just picked", not "in one of three blocks further down".
        """
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
        name = pick("name", user.get("name", ""))
        heading = user.get("name") or "Add a person"

        # Whether anything is actually arriving, said at the top rather
        # than left for the sync log to reveal.
        status = ""
        if not adding:
            state = self._person_state(user, int(time.time() * 1000))
            status = ui.banner(
                state["kind"],
                f'<span class="status">{state["headline"]}</span>',
                dot_kind=state["kind"],
                trail='<a class="link" href="/log">Log &rsaquo;</a>')

        def test_block(label: str) -> str:
            return (f'<button type="button" class="test" data-needs-js hidden>'
                    f"{label}</button>"
                    '<p class="testresult" hidden>'
                    '<span class="mark" aria-hidden="true"></span>'
                    '<span class="msg"></span></p>')

        def poll_block(field: str, value) -> str:
            return (ui.rule("How often")
                    + '<div class="inline-field">'
                    + ui.text_input(field, value, kind="number",
                                    input_id=field,
                                    extra='min="15" step="5"'
                                          ' aria-label="Seconds between checks"')
                    + '<span class="note">seconds between checks</span></div>')

        push = (
            ui.rule("Enter these in Trio")
            + ui.copy_input("push_url", f"http://{self._lan_ip()}:{port}",
                            input_id="push_url",
                            label="URL for the uploader")
            + ui.copy_input("api_secret", secret, input_id="api_secret",
                            label="API secret")
            + '<p class="note">Trio &rsaquo; Settings &rsaquo; Services &rsaquo;'
              " Nightscout. The first box is the URL, the second the API"
              " secret.</p>"
            # The port belongs to this source and to nothing else — someone
            # on twiist was being asked for a "Port (Nightscout API)" they
            # will never use. It stays in the form so it round-trips.
            + ui.disclosure(
                f"Port {port}",
                ui.row("Port", ui.text_input("port", port, kind="number",
                                             input_id="port"),
                       for_id="port",
                       hint="The uploader connects to this port on this"
                            " device. Only change it if something else is"
                            " using it."),
                slim=True)
        )
        tidepool = (
            ui.rule("Tidepool login")
            + ui.text_input("tp_email",
                            pick("tp_email", source.get("email", "")
                                 if stype == "tidepool" else ""),
                            kind="email", input_id="tp_email",
                            placeholder="Tidepool email",
                            extra='autocapitalize="none" autocorrect="off"'
                                  ' spellcheck="false"'
                                  ' aria-label="Tidepool email"')
            + '<div class="gap"></div>'
            # Stored credentials are never rendered back into the page:
            # blank means "keep what is saved".
            + ui.password_input("tp_password", "",
                                placeholder=self._secret_placeholder(
                                    stype == "tidepool",
                                    source.get("password"))
                                or "Tidepool password",
                                input_id="tp_password",
                                extra='aria-label="Tidepool password"')
            + test_block("Test this login")
            + poll_block("poll", pick("poll", source.get("poll_seconds", 60)
                                      if stype == "tidepool" else 60))
        )
        ns_key = source.get("api_secret") or source.get("token") or ""
        nightscout = (
            ui.rule("The Nightscout site")
            + ui.text_input("ns_url",
                            pick("ns_url", source.get("url", "")
                                 if stype == "nightscout" else ""),
                            kind="url", placeholder="mysite.example.com",
                            input_id="ns_url",
                            extra='autocapitalize="none" autocorrect="off"'
                                  ' spellcheck="false"'
                                  ' aria-label="Nightscout address"')
            + '<div class="gap"></div>'
            + ui.password_input("ns_key", "",
                                placeholder=self._secret_placeholder(
                                    stype == "nightscout", ns_key)
                                or "API secret or token",
                                input_id="ns_key",
                                extra='aria-label="Nightscout API secret'
                                      ' or token"')
            + '<p class="note">Either works &mdash; GlucoCube works out'
              " which.</p>"
            + test_block("Test this site")
            + poll_block("ns_poll",
                         pick("ns_poll", source.get("poll_seconds", 60)
                              if stype == "nightscout" else 60))
        )

        # Open when this person has overrides, and when a failed save is
        # being re-rendered with some — collapsing over what someone just
        # typed is how you lose it.
        typed = any((form or {}).get(f"th_{key}") for key in
                    ("low", "high", "urgent_low", "urgent_high"))
        shared = self._range_defaults()
        bands = self._thresholds_for(user)
        overridden = bool(th)
        summary_state = (
            f"{_g(bands['low'])}\u2013{_g(bands['high'])}" if overridden
            else f"using shared {_g(shared['low'])}\u2013{_g(shared['high'])}")
        ranges = ui.disclosure(
            f"Ranges just for {name or 'this person'}",
            '<div class="pair">'
            + ui.field("Low", ui.text_input(
                "th_low", pick("th_low", _g(th.get("low"))), kind="number",
                placeholder="default", input_id="th_low"))
            + ui.field("High", ui.text_input(
                "th_high", pick("th_high", _g(th.get("high"))), kind="number",
                placeholder="default", input_id="th_high"))
            + '</div><div class="gap"></div><div class="pair">'
            + ui.field("Urgent under", ui.text_input(
                "th_urgent_low", pick("th_urgent_low",
                                      _g(th.get("urgent_low"))),
                kind="number", placeholder="default", input_id="th_urgent_low"))
            + ui.field("Urgent over", ui.text_input(
                "th_urgent_high", pick("th_urgent_high",
                                       _g(th.get("urgent_high"))),
                kind="number", placeholder="default",
                input_id="th_urgent_high"))
            + '</div><p class="note">Blank uses the ranges everyone'
              " shares.</p>",
            state=summary_state, state_set=overridden,
            open_=typed, top=True)

        remove = ""
        if not adding and len(users) > 1:
            remove = f"""
<div class="farewell">
<form method="POST" action="/settings/person/remove?i={index}"
      onsubmit="return confirm('Remove this person from the display?')">
  <button type="submit" class="danger">Remove
    {ui.esc(user.get("name") or "this person")}</button>
</form>
<p class="note">Their readings stay in the database &mdash; only the panel
goes away.</p>
</div>"""
        lede = ('<p class="lede">They get their own panel on the screen, '
                "their own port and their own API secret.</p>" if adding
                else "")
        return f"""<h1>{ui.esc(heading)}</h1>
{lede}
{status}
{banner}
<form method="POST" action="/settings/person?i={ui.esc(index)}"
      data-index="{ui.esc(index)}" data-dirty>
  {ui.row("Name", ui.text_input("name", name, input_id="name"),
          for_id="name")}
  <label class="lbl">Where the data comes from</label>
  {ui.choice_cards("source", self.SOURCE_CARDS, stype, controls=control,
                   bodies={"push": push, "tidepool": tidepool,
                           "nightscout": nightscout})}
  {ranges}
  {ui.save_bar("Add &amp; restart display" if adding
               else "Save &amp; restart display")}
</form>{remove}"""

    def _page_ranges(self) -> str:
        display = self._raw_config().get("display", {})
        low = _g(display.get("low"), 70)
        high = _g(display.get("high"), 180)
        urgent_low = _g(display.get("urgent_low"), 55)
        urgent_high = _g(display.get("urgent_high"), 250)

        def number(name: str, value) -> str:
            return ui.text_input(name, value, kind="number", input_id=name,
                                 css="num")

        return f"""<h1>Ranges</h1>
<p class="lede">What counts as in range, and where the numbers turn red.
Everyone shares these unless their own page overrides them.</p>
{self._flash()}
{ui.range_preview(low, high, urgent_low, urgent_high)}
<form method="POST" action="/settings/ranges" data-dirty>
{ui.rule("In range · mg/dL")}
<div class="pair">
  {ui.field("Low", number("low", low))}
  {ui.field("High", number("high", high))}
</div>
{ui.rule("Urgent · panel turns red")}
<div class="pair">
  {ui.field("Under", number("urgent_low", urgent_low))}
  {ui.field("Over", number("urgent_high", urgent_high))}
</div>
{ui.rule("Stale after")}
<div class="inline-field">
  {ui.text_input("stale_minutes", _g(display.get("stale_minutes"), 12),
                 kind="number", input_id="stale_minutes", css="num",
                 extra='aria-label="Minutes before a panel greys out"')}
  <span class="note">minutes without a reading before the panel greys
    out</span>
</div>
{ui.save_bar()}
</form>"""

    def _page_clock(self) -> str:
        display = self._raw_config().get("display", {})
        current = display.get("timezone", "")
        clock = ('<div class="clockrow">'
                 f'<span class="clock">{time.strftime("%H:%M")}</span>'
                 f'<span class="weekday">{time.strftime("%A")}</span></div>')
        return f"""<h1>Clock</h1>
<p class="lede">Where the device is, so the clock and the times on the
chart are right. A device straight off the image has no zone set and
reads UTC.</p>
{self._flash()}
<div class="panel" id="devclock" data-zone="{ui.esc(current)}">
  <span class="cap"><span class="grow">The device reads</span></span>
  {clock}
</div>
<div class="banner ok" id="tzdetected" hidden>
  <span class="grow"><span class="status">This phone says <b></b></span></span>
  <button type="button" class="tiny go">Use that</button>
</div>
<form method="POST" action="/settings/clock" data-dirty>
  {ui.row("Time zone", ui.select("timezone", timezone_options(), current,
                                 input_id="timezone"), for_id="timezone")}
  <p class="note" id="tzpreview"></p>
  {ui.save_bar()}
</form>"""

    def _page_network(self) -> str:
        if not network.available():
            return ("<h1>Wi-Fi</h1><p class=\"lede\">This device has no "
                    "NetworkManager, so its network is managed elsewhere.</p>")
        hotspot = network.hotspot_active()
        wifi = network.state()
        notices = [self._flash()]
        if hotspot:
            notices.append(ui.banner(
                "info", "Setup hotspot is on \u2014 pick your home network "
                "below."))
        if wifi.get("state") == "failed":
            notices.append(ui.banner(
                "err", f"Could not join <b>{ui.esc(wifi.get('ssid', ''))}</b> "
                       f"\u2014 {ui.esc(wifi.get('error', 'unknown error'))}"))
            if wifi.get("detail"):
                notices.append("<details class=\"slim\"><summary>"
                               "<span class=\"label\">Technical detail</span>"
                               "</summary><div class=\"inner\">"
                               f"<pre class=\"detail\">{ui.esc(wifi['detail'])}"
                               "</pre></div></details>")
        elif wifi.get("state") == "joining":
            notices.append(ui.banner(
                "info", f"Joining <b>{ui.esc(wifi.get('ssid', ''))}</b>&hellip;"))
        if wifi.get("reboot_error"):
            notices.append(ui.banner(
                "err", "Could not restart automatically: "
                       f"{ui.esc(wifi['reboot_error'])} \u2014 power-cycle the "
                       "device to finish."))
        if wifi.get("hotspot_error"):
            notices.append(ui.banner(
                "err", "Setup hotspot could not start: "
                       f"{ui.esc(wifi['hotspot_error'])}"))

        networks = network.cached_networks()
        address = config_mod.admin_url(self._lan_ip(),
                                       self.server.config.admin_port)
        # What you are on now, before what you could move to.
        connected = ""
        if wifi.get("state") == "ok" and wifi.get("ssid") and not hotspot:
            signal = next((net.get("signal") for net in networks
                           if net.get("ssid") == wifi["ssid"]), None)
            bars = (ui.signal_bars(int(signal), live=True)
                    if signal is not None else "")
            connected = ui.panel(
                "Connected to",
                f'<span class="big"><span class="grow">'
                f'{ui.esc(wifi["ssid"])}</span>{bars}</span>'
                f'<span class="sub">This device &#183; {ui.esc(address)}</span>',
                edge=True)

        age = network.scan_age_seconds()
        if network.scan_in_progress():
            hint = "Scanning&hellip;"
        elif networks:
            when = ("just now" if age is None or age < 90
                    else f"{int(age // 60)} min ago")
            hint = f"{len(networks)} networks found, scanned {when}."
        else:
            hint = ("No scan results \u2014 use \u201cOther network\u201d and "
                    "type the name." if hotspot
                    else "No networks found; try Rescan.")
        # The rescan button lives in the section heading, and so needs its
        # own form: a form inside the join form would not be valid markup.
        if hotspot:
            nearby = (ui.rule("Also nearby")
                      + '<p class="note">The radio cannot scan while the setup'
                        " hotspot is running, so this is the list from just"
                        " before it started.</p>")
        else:
            nearby = ('<form method="POST" action="/wifi/rescan">'
                      + ui.rule("Also nearby",
                                trail='<button type="submit" class="tiny">'
                                      "Rescan</button>")
                      + "</form>")
        return f"""<h1>Wi-Fi</h1>
{''.join(notices)}
{connected}
{nearby}
<form method="POST" action="/wifi">
  {ui.network_picker(networks, selected=wifi.get("ssid", ""))}
  <p class="note spaced">{hint}</p>
  {ui.row("Password", ui.password_input("wifi_password", "",
                                        input_id="wifi_password"),
          for_id="wifi_password",
          hint="Check it before joining &mdash; the device drops off the "
               "network while it tries, and a wrong password takes a minute "
               "or two to come back.")}
  <div class="actions"><button type="submit">Join network</button></div>
</form>"""

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
        running = ui.esc(updater.current_version())
        latest = ui.esc(state.get("latest", ""))
        offered = bool(state.get("available") and state.get("latest_tag"))

        # Running, and what it could be running: two numbers with an arrow
        # between them says "there is somewhere to go" faster than a
        # sentence does.
        versions = ('<div class="versions">'
                    '<span class="v"><span class="cap">Running</span>'
                    f'<span class="n">{running}</span></span>')
        if state.get("available") and latest:
            versions += ('<span class="to" aria-hidden="true">&rarr;</span>'
                         '<span class="v new"><span class="cap">Available'
                         f'</span><span class="n">{latest}</span></span>')
        versions += "</div>"

        checked = (time.strftime("%H:%M",
                                 time.localtime(state["checked_at"] / 1000))
                   if state.get("checked_at") else "")
        notes = (f' &#183; <a href="{ui.esc(state.get("url", ""))}">release '
                 "notes &rsaquo;</a>" if state.get("url") else "")
        if not checked:
            when = "Not checked yet &#183; checks run every six hours"
        elif state.get("available"):
            when = f"Checked {checked}{notes}"
        else:
            when = f"Checked {checked} &#183; up to date{notes}"

        # The sentence still earns its place when the news is bad, or odd.
        trouble = (ui.banner(kind, status)
                   if state.get("error") or state.get("rejoin")
                   or (checked and not state.get("latest")) else "")
        install = ""
        if offered:
            install = f"""
<div class="actions">
  <form method="POST" action="/update/apply">
    <input type="hidden" name="tag" value="{ui.esc(state.get('latest_tag', ''))}">
    <button type="submit">Install {latest}</button>
  </form>
</div>
<p class="note spaced">The display restarts on the new version, about a
minute. Readings are untouched.</p>"""
        return f"""<h1>Updates</h1>
{self._flash()}
{versions}
<p class="meta">{when}</p>
{trouble}
{install}
<div class="actions">
  <form method="POST" action="/update/check">
    <button type="submit" class="quiet">Check now</button>
  </form>
</div>
{ui.rule("Release channel")}
<form method="POST" action="/settings/updates/channel">
  {ui.choice_cards("channel", self.CHANNEL_CARDS, channel)}
  <p class="note">Switching installs that channel&#8217;s newest release
  straight away &mdash; including stepping back onto the last full release
  when you leave Beta. The display restarts, which takes about a
  minute.</p>
  <div class="actions">
    <button type="submit" class="quiet">Switch channel</button>
  </div>
</form>
<p class="note nudge">Updates install from GitHub releases. A release marked
<span class="mono">[force-update]</span> in its notes installs itself at the
next check, on whichever channel published it.</p>"""

    def _page_access(self) -> str:
        config = self.server.config
        password = config.admin_password
        link = config_mod.admin_url(
            self._lan_ip(), config.admin_port,
            f"/settings?key={password}" if password else "/settings")
        current = ui.panel(
            "Sign in as admin",
            ui.copy_value(password, input_id="current")
            + '<p class="note">The device&#8217;s own screen shows this too,'
              " so you cannot lock yourself out.</p>",
            cap_trail=ui.copy_button("current")) if password else ""
        autolink = ui.panel(
            "Link that skips the login",
            ui.copy_value(link, input_id="autolink", css="link")
            + '<p class="warnnote"><span aria-hidden="true">!</span><span>The '
              "same link the QR code on the device carries. Anyone with it "
              "can change these settings.</span></p>",
            quiet=True, cap_trail=ui.copy_button("autolink"))
        return f"""<h1>Access</h1>
<p class="lede">{"This page and the dashboard need a password."
                 if password else
                 "This page has no password: anyone on the network can "
                 "change these settings."}</p>
{self._flash()}
{current}
{autolink}
<form method="POST" action="/settings/access" data-dirty>
  {ui.row("New password", ui.password_input("admin_password", "",
          input_id="admin_password"), for_id="admin_password",
          hint="At least six characters. You stay logged in on this phone.")}
  {ui.save_bar()}
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
            "Installing",
            ui.BRAND_NAV
            + f'{ui.eyebrow("Installing")}<h1>{ui.esc(version)}</h1>'
            '<p class="lede">The display restarts on the new version. This '
            "page reloads by itself in about a minute.</p>"
            + ui.steps_bar(3, 1)
            + ui.checklist([
                (True, "Release downloaded"),
                (False, "Installing\u2026"),
                (False, "Waiting for the new version to answer\u2026"),
            ]),
            refresh="45;url=/settings/updates").encode()

    @staticmethod
    def _error_page(message: str, back: str) -> bytes:
        return ui.page(
            "That did not save",
            f'{ui.eyebrow("Not saved")}<h1>That did not save</h1>'
            f'<p class="lede">{ui.esc(message)}</p>'
            f'<p class="note">Nothing on the device has changed.</p>'
            f'<div class="actions"><a class="btn" href="{ui.esc(back)}">'
            "Back</a></div>").encode()

    @staticmethod
    def _applying_page(back: str, heading: str = "Saved",
                       message: str = "") -> bytes:
        """The interstitial for the restart every config change causes.

        It waits for the *new* process to answer rather than guessing at
        a number of seconds: the old fixed reload landed on a connection
        error about half the time. Naming the three beats of that wait —
        written, stopped, answering — turns dead air into progress.
        """
        script = """<script>
(function(){
  var next = %s, gone = false;
  var bars = document.querySelectorAll('.steps i');
  var steps = document.querySelectorAll('.checklist > span');
  function done(i, text){
    if (bars[i]) bars[i].className = 'done';
    if (!steps[i]) return;
    steps[i].className = '';
    steps[i].querySelector('.mark').innerHTML = '&#10003;';
    if (text) steps[i].lastChild.textContent = text;
  }
  setInterval(function(){
    fetch('/api/health.json', {cache: 'no-store'}).then(function(r){
      if (!r.ok) throw new Error(r.status);
      if (gone) { done(2, 'The new one answered'); location.replace(next); }
    }).catch(function(){
      if (!gone) done(1, 'Old process stopped');
      gone = true;
    });
  }, 1200);
})();
</script>""" % json.dumps(back)
        lede = message or ("Your settings are written. This page comes back "
                           "by itself as soon as the device answers again "
                           "\u2014 usually five seconds.")
        body = (
            ui.BRAND_NAV
            + f'<p class="eyebrow ok">{ui.esc(heading)}</p>'
            "<h1>The display is restarting</h1>"
            f'<p class="lede">{lede}</p>'
            + ui.steps_bar(3, 1)
            + ui.checklist([
                (True, "Settings written to config.json"),
                (False, "Stopping the old process\u2026"),
                (False, "Waiting for the new one to answer\u2026"),
            ])
            + '<p class="note nudge">Readings that arrive during the restart '
              "are queued by the uploader and land as soon as it is back.</p>"
        )
        return ui.page(heading, body, script=script,
                       refresh=f"12;url={back}").encode()

    def _settings_get(self, path: str) -> None:
        back, script, back_label = "/settings", "", "Settings"
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
            back_label = "People"
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
                           back_label=back_label, script=script).encode(),
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
                nav=True, back="/settings/people", back_label="People",
                script=PERSON_SCRIPT,
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
        return ui.page("Joining " + ssid, f"""{ui.BRAND_NAV}
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
        # Each pull source carries its own interval inside its own card, so
        # both fields reach the server and only the chosen one is read.
        poll_field = "ns_poll" if kind == "nightscout" else "poll"
        poll = max(15, int(_number(form, poll_field, "Check every") or 60))
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
        password = form.get("admin_password", "").strip()
        if not password:
            raise ValueError("Type a new password, or go back to keep the "
                             "one in use.")
        if len(password) < 6:
            raise ValueError("The password must be at least six characters.")
        raw = self._raw_config()
        raw.setdefault("admin", {})["password"] = password
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
