"""Web admin UI — edit config.json from a browser.

Serves the dashboard and a settings page (default port 80; falls back to
8080 when 80 isn't available) where users, ports, API secrets, Tidepool
sources, and display thresholds can be edited. Saving validates the new
config, writes it atomically, and exits the process so systemd restarts
the app with the new settings (Restart=always).

Protected with HTTP Basic auth when config.admin.password is set.
"""

import base64
import hashlib
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import secrets as secrets_mod
import socket

from . import config as config_mod
from . import multipart as multipart_mod
from . import wallpaper as wallpaper_mod
from . import weather as weather_mod
from . import captive, contract, network, onboarding, predict, sources, synclog, ui
from . import updater
from . import glucocore, pairing, sync, verify
from . import units as units_mod
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
    img.addEventListener('load', function(){
      last = Date.now();
      // A refresh that failed hid it; one that worked brings it back.
      img.hidden = false;
    });
  });
  setInterval(function(){
    shots().forEach(function(img){ img.src = '/screen.png?t=' + Date.now(); });
  }, 5000);
  setInterval(function(){
    var age = Math.round((Date.now() - last) / 1000);
    document.querySelectorAll('.shotage').forEach(function(el){
      el.textContent = age < 2 ? 'just now' : 'updated ' + age + 's ago';
      // Three missed refreshes is not "live" any more, and the dot should
      // stop claiming it is.
      var dot = el.parentNode.querySelector('.dot');
      if (dot) dot.className = age > 15 ? 'dot warn' : 'dot ok';
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

PAIRING_SCRIPT = """<script>
// While a QR code is on the page, somebody may be approving it on their
// phone this second. The display pairs itself when that happens and
// restarts; this notices and follows, so the page does not sit there
// saying "waiting" beside a display that has already moved on.
(function(){
  var waiting = document.getElementById('pairwait');
  if (!waiting) return;
  var tick = async function(){
    try {
      var response = await fetch('/api/pairing.json', {cache: 'no-store'});
      var state = await response.json();
      if (state.paired) { location.replace('/settings/glucocore?msg=paired'); return; }
      if (state.error) {
        waiting.className = 'banner warn';
        waiting.textContent = state.error;
      }
    } catch (err) {
      // The restart the pairing causes looks exactly like this. Keep
      // asking: the next answer either comes back paired or comes back.
    }
    setTimeout(tick, 3000);
  };
  setTimeout(tick, 3000);
})();
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
            list.value = zone;
            detected.hidden = true;
            // Assigning .value fires nothing. The save bar counts change
            // events, and this is a change; preview() runs off the same
            // document listener.
            list.dispatchEvent(new Event('change', {bubbles: true}));
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
    t === 'dark' ? 'Night &#9790;' : 'Day &#9788;'; }
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

// How a reading is written here. Everything the page is given is mg/dL,
// the way everything inside the device is; these are the last step before
// it reaches a screen, and the only place that knows which unit it is in.
let UNITS = 'mg/dL';
const isMmol = () => UNITS === 'mmol/L';
function shown(v){ return v == null ? null : (isMmol() ? v / 18 : v); }
function fmtGlucose(v, blank){
  const x = shown(v);
  if (x == null) return blank === undefined ? '---' : blank;
  return isMmol() ? x.toFixed(1) : String(Math.round(x));
}
function fmtDelta(v){
  const x = shown(v);
  if (x == null) return '';
  return (x >= 0 ? '+' : '') + (isMmol() ? x.toFixed(1) : String(Math.round(x)));
}
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
  s += `<text x="${W-5}" y="${Y(th.high)+13}" font-size="11" fill="var(--faint)" text-anchor="end">${fmtGlucose(th.high)}</text>`;
  s += `<text x="${W-5}" y="${Y(th.low)-4}" font-size="11" fill="var(--faint)" text-anchor="end">${fmtGlucose(th.low)}</text>`;
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
    ? `<span class="val" style="color:${colorFor(f2h, th, false)}">${tilde}${fmtGlucose(f2h)}</span>
       <span class="eta">${eta}</span>` : '';
  const stat = (lbl, val, unit, sub) =>
    `<div><div class="lbl">${lbl}</div><div class="val">${val}<small>${unit}</small></div>` +
    (sub ? `<div class="sub2">${sub}</div>` : '') + '</div>';
  return `<div class="card${urgent ? ' urgent' : ''}">
    <div class="head"><span class="who">${esc(u.name)}</span>
      <span class="badge"><span class="dot" style="background:${dotCol}"></span>${u.source_label || 'TRIO'} \\u00b7 ${ageC(now, u.sgv_date)}</span></div>
    <div class="bigrow">
      <div class="big" style="color:${col}">${fmtGlucose(u.sgv)}</div>
      <div class="side">
        <span class="arrow" style="color:${col}">${!stale && ARROWS[u.direction] || ''}</span>
        <span class="delta">${!stale ? fmtDelta(u.delta) : ''}</span>
        <span class="unit">${isMmol() ? 'MMOL/L' : 'MG/DL'}</span>
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
    // Set before render(): every number on the page goes through the
    // formatters above, and they read this.
    UNITS = d.units || 'mg/dL';
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


def _unique_name(name: str, taken) -> str:
    """A display name nothing else is using.

    Everything in the database is keyed by the name, so two people
    sharing one would read each other's readings.
    """
    name = (name or "").strip() or "Unnamed"
    if name.casefold() not in taken:
        return name
    for suffix in range(2, 100):
        candidate = f"{name} {suffix}"
        if candidate.casefold() not in taken:
            return candidate
    return f"{name} {secrets_mod.token_hex(2)}"


def _as_list(value) -> list[str]:
    """Checkbox fields arrive as a string, a list, or not at all."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in value if item]


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
        # Committed before the caller returns. Every save ends by scheduling
        # the process to exit; a response still sitting in a buffer when
        # that timer fires is a page nobody ever sees.
        self.wfile.flush()

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
        # Also before it: the typefaces every page is now set in carry no
        # data, and captive.ALWAYS_SERVE already exempts them from the
        # portal redirect. Behind Basic auth they meant a phone that typed
        # the address by hand got a login prompt fired from a stylesheet,
        # and then a settings page in the system sans it exists to replace.
        if path.startswith("/fonts/"):
            self._send_font(path)
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
                           back="/settings", home=True, script=LOG_SCRIPT)
            self._send(page.encode(), "text/html; charset=utf-8")
        elif path == "/api/pairing.json":
            gc = self._pairing_config()
            state = pairing.public_state(self.server.store)
            self._send(json.dumps({
                "paired": bool(gc),
                # Never the secret: this is a page's view of the request,
                # and the secret is what turns the request into a token.
                "request_id": state.get("request_id", ""),
                "approve_url": state.get("approve_url", ""),
                "expires_at": state.get("expires_at", 0),
                "error": state.get("error", ""),
            }).encode(), "application/json")
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
        else:
            self._send(ui.page("Not found", "<h1>Not found</h1>"
                               '<p><a href="/settings">Back to settings</a></p>'
                               ).encode(), "text/html; charset=utf-8", 404)

    def _send_font(self, path: str) -> None:
        """Every page is set in these, and no page can fetch them elsewhere.

        The OFL text is served alongside them because the license asks that
        each copy of the fonts carry it — and this handler hands a copy to
        every browser that loads any page on the device.
        """
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
                "source_label": contract.SOURCE_LABELS.get(
                    source_type, contract.SOURCE_LABEL_DEFAULT),
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
            # Every glucose number in this payload is mg/dL, whatever this
            # says — the same contract the rest of the device runs on, and
            # what anything reading this endpoint has always been given.
            # It says how they are to be *written*.
            "units": units_mod.normalize(dc.units),
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
                    "nightscout": "Nightscout", "glucocore": "GlucoCore"}
    # The glyphs the physical screen draws and the dashboard prints, so a
    # reading means the same thing wherever it is read.
    ARROWS = {"DoubleUp": "\u2191\u2191", "SingleUp": "\u2191",
              "FortyFiveUp": "\u2197", "Flat": "\u2192",
              "FortyFiveDown": "\u2198", "SingleDown": "\u2193",
              "DoubleDown": "\u2193\u2193"}
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
        "paired": ("ok", "Paired with GlucoCore."),
        "signedout": ("info", "Sign-in discarded."),
        "unpaired": ("ok", "Unpaired. This display no longer talks to "
                           "GlucoCore."),
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
            if entry.get("ok"):
                return ""
            # The log line carries the retry interval too; this line has
            # its own second half to add, so it takes only the phrase.
            return (entry.get("message") or "").split(
                sources.RETRY_SUFFIX)[0]
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
        state = {"label": label, "value": "", "arrow": "", "age": "",
                 "unit": units_mod.normalize(self.server.config.display.units),
                 "tone": "v-stale", "trend": "", "headline": "", "short": "",
                 "pill": "", "kind": "err", "say": "not arriving", "text": ""}
        if source.get("type") == "glucocore" and not self._pairing_config():
            return {**state, "short": "not paired", "pill": "not paired",
                    "kind": "err", "say": "not paired",
                    "text": "GlucoCore — this display is not paired",
                    "headline": "This display is not paired with GlucoCore"}
        if source.get("type") and not self._source_ready(source):
            return {**state, "short": "needs setup", "pill": "needs setup",
                    "kind": "err", "say": "needs setup",
                    "text": f"{label} — credentials missing",
                    "headline": "No credentials yet, so nothing is arriving"}
        snap = self.server.store.snapshot(name)
        failure = self._last_failure(name)
        if not snap.sgv_date or snap.sgv is None:
            return {**state, "short": "no data", "pill": "", "kind": "warn",
                    "say": "nothing has arrived yet",
                    "text": f"{label} — nothing has arrived yet",
                    "headline": ui.esc(failure) or "Nothing has arrived yet"}
        minutes = max(0, int((now_ms - snap.sgv_date) / 60000))
        when = ("just now" if minutes < 1
                else f"{minutes}m ago" if minutes < 120
                else f"{minutes // 60}h ago")
        stale = minutes > self.server.config.display.stale_minutes
        shown_in = self.server.config.display.units
        unit = units_mod.normalize(shown_in)
        bands = self._thresholds_for(user)
        arrow = self.ARROWS.get(snap.direction or "", "")
        # The minus is the typographic one, to match the arrow beside it.
        delta = (units_mod.fmt_delta(snap.delta, shown_in).replace("-", "\u2212")
                 if snap.delta is not None else "")
        reading = units_mod.fmt(snap.sgv, shown_in)
        headline = (
            f'<span class="bad">{ui.esc(failure)}</span> \u00b7 last reading '
            f"{when}" if failure
            else f"Arriving \u00b7 <b>{reading} {unit}</b>, {when}")
        return {
            "label": label, "value": reading, "arrow": arrow, "unit": unit,
            "trend": " ".join(part for part in (arrow, delta) if part),
            "age": when, "tone": self._tone(snap.sgv, stale, bands),
            "text": f"{label} — {reading} {unit}, {when}",
            "short": when, "pill": "stale" if stale else "",
            "kind": "err" if failure else "warn" if stale else "ok",
            # Colour is not the only thing carrying this: the dot says it
            # out loud for anyone who cannot see the colour.
            "say": ("not arriving" if failure else "stale" if stale
                    else "arriving"),
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
        if not (config.admin_password or config.admin_password_off):
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
                                        age=state["age"], tone=state["tone"],
                                        unit=state["unit"])
            else:
                value_html = ('<span class="val v-stale">'
                              f'{ui.esc(state["short"] or "no data")}</span>')
            people.append(ui.menu_item(
                f"/settings/person?i={index}", label, value_html=value_html,
                lead=ui.dot(state["kind"], label=state["say"])))
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
        shown_in = display.get("units")
        unit = units_mod.normalize(shown_in)
        low = units_mod.fmt_field(display.get("low", 70), shown_in)
        high = units_mod.fmt_field(display.get("high", 180), shown_in)
        urgent_low = units_mod.fmt_field(display.get("urgent_low", 55), shown_in)
        urgent_high = units_mod.fmt_field(display.get("urgent_high", 250),
                                          shown_in)
        screen_rows.append(ui.menu_item(
            "/settings/ranges", f"Ranges \u00b7 {unit}",
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
        place = (raw.get("weather") or {}).get("place")
        screen_rows.append(ui.menu_item(
            "/settings/weather", "Weather",
            value_html=f'<span class="val">{ui.esc(place)}</span>' if place
            else '<span class="val v-stale">Off</span>'))

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
        paired = self._pairing_config()
        on_gc = sum(1 for user in users
                    if (user.get("source") or {}).get("type") == "glucocore")
        if paired:
            who = ("" if not on_gc else " <small>\u00b7 1 person</small>"
                   if on_gc == 1 else f" <small>\u00b7 {on_gc} people</small>")
            gc_html = (f'<span class="val">{ui.esc(paired.name or "Paired")}'
                       f"{who}</span>")
        else:
            gc_html = '<span class="val v-stale">Not paired</span>'
        # A badge only when something is actually broken: people pulled from
        # GlucoCore on a display that has no token to pull with. "Not paired"
        # on a device happily fed by Trio is not a problem, and a pill that
        # never means anything is one people stop seeing.
        device_rows.append(ui.menu_item(
            "/settings/glucocore", "GlucoCore", value_html=gc_html,
            badge="Not paired" if on_gc and not paired else "",
            badge_kind="err"))
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
            badge=badge, badge_kind="warn"))
        if config.admin_password:
            access_html = ('<span class="val">Password set '
                           "<small>\u00b7 admin</small></span>")
        elif config.admin_password_off:
            access_html = ('<span class="val">No password '
                           "<small>\u00b7 on purpose</small></span>")
        else:
            access_html = '<span class="val v-low">No password set</span>'
        device_rows.append(ui.menu_item("/settings/access", "Access",
                                        value_html=access_html))

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
        display = self._raw_config().get("display", {})
        layout = config_mod.normalize_layout(display.get("layout"))
        direction = config_mod.normalize_split_direction(
            display.get("split_direction"))
        cap = display.get("split_max")
        people = len(self._raw_config().get("users") or [])
        caps = [("", "Everyone")] + [(str(n), f"{n} person" if n == 1
                                      else f"{n} people") for n in range(1, 7)]

        def switch(to: str, label: str) -> str:
            return (f'<form method="POST" action="/display/theme">'
                    f'<input type="hidden" name="theme" value="{to}">'
                    '<input type="hidden" name="back" value="/settings/screen">'
                    f'<button type="submit">{label}</button></form>')

        # Two states side by side with the live one already pressed, rather
        # than a button naming the state you are not in.
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
on a phone with no password to type.</p>
<form method="POST" action="/settings/screen" data-dirty>
  {ui.choice_cards("layout", self.LAYOUT_CARDS, layout, controls="lay",
                   legend="How the people share the screen")}
  {ui.group("lay", "split",
            ui.row("Panels run",
                   ui.select("split_direction", self.SPLIT_DIRECTIONS,
                             direction, input_id="split_direction"),
                   for_id="split_direction",
                   hint="Side by side on a landscape screen, unless you say "
                        "otherwise.")
            + ui.row("At most on screen",
                     ui.select("split_max", caps,
                               "" if not cap else str(cap),
                               input_id="split_max"),
                     for_id="split_max",
                     hint=f"{people} on this display. More than two on a 7-inch "
                          "panel makes each number small; a smaller number "
                          "here shows them a page at a time."),
            current=layout)}
  {ui.group("lay", "rotate",
            ui.row("Seconds each person",
                   ui.text_input("rotate_seconds",
                                 display.get("rotate_seconds", 12),
                                 kind="number", input_id="rotate_seconds"),
                   for_id="rotate_seconds",
                   hint="An urgent reading holds the screen until it clears, "
                        "whoever&rsquo;s turn it was."),
            current=layout)}
{ui.rule("Background")}
  {ui.row("Behind everyone",
          ui.select("wallpaper", self._wallpaper_options(),
                    display.get("wallpaper", ""), input_id="wallpaper"),
          for_id="wallpaper",
          hint="What sits behind a person who has none of their own. Add "
               "pictures on each person&rsquo;s page.")}
  {ui.row("Dim the art by",
          ui.text_input("wallpaper_dim", display.get("wallpaper_dim", 60),
                        kind="number", input_id="wallpaper_dim"),
          for_id="wallpaper_dim",
          hint="Per cent. The reading has to stay readable over whatever is "
               "behind it &mdash; less dimming shows more of the picture.")}
  {ui.row("Dim further overnight by",
          ui.text_input("night_dim_boost",
                        display.get("night_dim_boost", 24), kind="number",
                        input_id="night_dim_boost"),
          for_id="night_dim_boost",
          hint="Added to the figure above between the hours set on the "
               "ranges page.")}
  {ui.save_bar()}
</form>"""

    # The two ways a display can arrange the people on it. Cards rather
    # than a select because this is the choice that changes what the
    # screen *is*, and it deserves to be the thing you see.
    LAYOUT_CARDS = (
        ("split", "Everyone at once",
         "A panel each, side by side — what this display has always done."),
        ("rotate", "One at a time",
         "Full-screen, over a background, moving on every few seconds."),
    )
    SPLIT_DIRECTIONS = (
        ("auto", "However the screen is turned"),
        ("columns", "Side by side"),
        ("rows", "Stacked"),
    )

    def _wallpaper_options(self):
        """Nothing, the art on the device, and anything uploaded here."""
        options = [("", "Nothing")]
        options += [(f"bundled:{name}", wallpaper_mod.bundled_label(name))
                    for name in wallpaper_mod.bundled_names()]
        for entry in self._uploaded_wallpapers():
            options.append((entry, "Uploaded picture"))
        return options

    def _uploaded_wallpapers(self):
        directory = wallpaper_mod.cache_dir(self.server.config.database)
        if not directory.is_dir():
            return []
        return sorted(e.name for e in directory.iterdir()
                      if e.is_file() and wallpaper_mod.is_id(e.name))

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
                unit=state["unit"],
                tone=state["tone"], dot_kind=state["kind"],
                dot_label=state["say"], note=state["short"] or "no data"))
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
        # Who a paired display shows is decided in GlucoCore, and the next
        # config push would undo anything chosen here. The cards are
        # replaced by a note that says where to go instead — the rest of
        # the page (their name, their ranges) still works.
        managed = source.get("type") == "glucocore"
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
                dot_kind=state["kind"], dot_label=state["say"], strip=True,
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
        shown_in = units_mod.normalize(
            self._raw_config().get("display", {}).get("units"))
        step = units_mod.step(shown_in)
        shared = self._range_defaults()
        bands = self._thresholds_for(user)
        overridden = bool(th)

        def shown(mgdl):
            return units_mod.fmt_field(mgdl, shown_in)

        def override(key, label):
            """One threshold box: blank stays blank, a number is converted."""
            saved = th.get(key)
            return ui.field(label, ui.text_input(
                f"th_{key}",
                pick(f"th_{key}",
                     "" if saved in (None, "") else shown(saved)),
                kind="number", placeholder="default", input_id=f"th_{key}",
                extra=f'step="{step}"'))

        # The summary answers the question the disclosure is hiding, so
        # there is usually no reason to open it at all.
        summary_state = (
            f"{shown(bands['low'])}\u2013{shown(bands['high'])}" if overridden
            else f"using shared {shown(shared['low'])}"
                 f"\u2013{shown(shared['high'])}")
        ranges = ui.disclosure(
            f"Ranges just for {name or 'this person'}",
            '<div class="pair">'
            + override("low", "Low") + override("high", "High")
            + '</div><div class="gap"></div><div class="pair">'
            + override("urgent_low", "Urgent under")
            + override("urgent_high", "Urgent over")
            + '</div><p class="note">Blank uses the ranges everyone shares. '
              f"In {ui.esc(shown_in)}.</p>",
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
        if managed:
            # Who a paired display shows is decided in GlucoCore, and the
            # next config push would undo anything chosen here. The cards
            # give way to a note that says where to go instead.
            sources_html = (
                ui.rule("Where the data comes from")
                + ui.facts([("Source", "GlucoCore"),
                            ("Patient ID",
                             f'<code>{ui.esc(source.get("patient_id", ""))}'
                             "</code>")])
                + '<p class="note">This display pulls their readings from '
                  "GlucoCore. Change who appears on it in GlucoCore, or "
                  '<a href="/settings/glucocore">unpair this display</a> to '
                  "go back to setting sources here.</p>")
        else:
            sources_html = ui.choice_cards(
                "source", self.SOURCE_CARDS, stype, controls=control,
                legend="Where the data comes from",
                bodies={"push": push, "tidepool": tidepool,
                        "nightscout": nightscout})
        # Their own background, and how long they hold the screen when the
        # display shows one person at a time. Both are theirs rather than
        # the display's, so both live here.
        art = ui.row(
            "Behind them",
            ui.select("wallpaper",
                      [("", "Whatever the display is using"),
                       ("none", "Nothing, even if the display has art")]
                      + self._wallpaper_options()[1:],
                      pick("wallpaper", user.get("wallpaper", "")),
                      input_id="wallpaper"),
            for_id="wallpaper")
        art += ui.row(
            "Seconds on screen",
            ui.text_input("rotate_seconds",
                          pick("rotate_seconds",
                               user.get("rotate_seconds") or ""),
                          kind="number", placeholder="default",
                          input_id="rotate_seconds"),
            for_id="rotate_seconds",
            hint="Blank uses the display&rsquo;s own. Only used when the "
                 "screen shows one person at a time.")
        upload = ""
        if not adding:
            # Its own form: a file cannot ride along with the rest, and
            # this one is the only thing on the site that posts bytes.
            upload = f"""
<form method="POST" action="/settings/person/wallpaper?i={ui.esc(index)}"
      enctype="multipart/form-data">
  {ui.row("Add a picture", ui.file_input("image"),
          hint="JPEG or PNG, up to 2 MB. Nothing is resized &mdash; the "
               "screen is 800&times;480, and anything much bigger is bytes "
               "for no visible gain.")}
  <div class="actions">
    <button type="submit" class="secondary">Upload &amp; apply</button>
  </div>
</form>"""
        return f"""<h1>{ui.esc(heading)}</h1>
{lede}
{status}
{banner}
<form method="POST" action="/settings/person?i={ui.esc(index)}"
      data-index="{ui.esc(index)}" data-dirty>
  {ui.row("Name", ui.text_input("name", name, input_id="name"),
          for_id="name")}
  {sources_html}
  {ranges}
  {ui.disclosure("Background", art, top=True)}
  {ui.save_bar("Add &amp; restart display" if adding
               else "Save &amp; restart display")}
</form>{upload}{remove}"""

    # ---- settings: GlucoCore ----

    def _pairing_config(self):
        """The pairing this device already has, or None."""
        gc = self.server.config.glucocore
        return gc if gc and gc.device_token else None

    @staticmethod
    def _patient_label(config: dict, patient_id: str) -> str:
        """What GlucoCore says this person is called on this display."""
        per = (config.get("perPatient") or {}).get(patient_id) or {}
        return str(per.get("label") or "").strip() or patient_id

    @staticmethod
    def _patient_id(patient: dict) -> str:
        return str(patient.get("userId") or patient.get("userid") or "")

    @staticmethod
    def _patient_name(patient: dict) -> str:
        return str(patient.get("name") or patient.get("email")
                   or AdminHandler._patient_id(patient))

    # A sign-in waiting for somebody to choose who to show. It holds a
    # session token, so it is short-lived and is thrown away the moment the
    # display is registered — what the device keeps is its own token.
    SIGNIN_KEY = "__signin"
    SIGNIN_TTL_MS = 15 * 60 * 1000

    def _signin_draft(self) -> dict:
        draft = self.server.store.get_params(self.SIGNIN_KEY)
        if not draft.get("token"):
            return {}
        age = int(time.time() * 1000) - int(draft.get("started_at") or 0)
        if age > self.SIGNIN_TTL_MS:
            self._clear_signin_draft()
            return {}
        return draft

    def _clear_signin_draft(self) -> None:
        # replace, not set: set_params drops falsy values, so an emptied
        # draft would leave the old token behind.
        self.server.store.replace_params(self.SIGNIN_KEY, {})

    def _page_glucocore(self, *, form=None, banner: str = "") -> str:
        gc = self._pairing_config()
        if gc:
            return self._page_glucocore_paired(gc, banner)
        draft = self._signin_draft()
        if draft:
            return self._page_glucocore_people(draft, form=form, banner=banner)
        return self._page_glucocore_claim(form=form, banner=banner)

    PAIR_CARDS = (
        ("qr", "Scan it with your phone",
         "Approve this display in GlucoCore — nothing to type"),
        ("signin", "Sign in here",
         "Your GlucoCore email and password, used once"),
        ("code", "Type a pairing code",
         "Six digits, from Devices in GlucoCore"),
    )

    def _page_glucocore_claim(self, *, form=None, banner: str = "") -> str:
        form = form or {}
        chosen = form.get("how", "qr")
        keeps = [user.get("name") or "unnamed" for user in
                 onboarding.keep_local_users(self._raw_config().get("users")
                                             or [])]
        keep_note = ""
        if keeps:
            keep_note = (
                '<p class="note">' + ui.esc(", ".join(keeps))
                + (" keeps" if len(keeps) == 1 else " keep")
                + " the source they have now — pairing adds to this display"
                  " rather than replacing what is on it.</p>")
        host = glucocore.GLUCOCORE_BASE.split("//")[-1]
        return f"""<h1>GlucoCore</h1>
<p class="lede">Pair this display with a GlucoCore account and it pulls each
person's readings from there. Three ways in — they end in the same place.</p>
{banner}
{ui.choice_cards("how", self.PAIR_CARDS, chosen, controls="how")}
{ui.group("how", "qr", self._pair_by_qr(), current=chosen)}
{ui.group("how", "signin", self._pair_by_signin(form), current=chosen)}
{ui.group("how", "code", self._pair_by_code(form), current=chosen)}
{keep_note}
<p class="note">No account yet? Create one at <b>{ui.esc(host)}</b> on your
phone, then come back.</p>"""

    def _pair_by_qr(self) -> str:
        """The request the waiter is holding open, as something to scan."""
        state = pairing.public_state(self.server.store)
        url = state.get("approve_url") or ""
        if not url:
            trouble = state.get("error") or ""
            return (
                '<div class="pairqr">'
                + ui.banner("warn", "This display has not been able to ask "
                                    "GlucoCore for a code yet."
                            + (f" Last error: {ui.esc(trouble)}"
                               if trouble else ""))
                + '<p class="note">It keeps trying. Sign in or type a code'
                  " above if you would rather not wait.</p></div>")
        code = ui.qr_svg(url, alt="Approve this display in GlucoCore")
        shown = url.split("//")[-1]
        return f"""<div class="pairqr">
  {code or ''}
  <p class="note">Scan it with a phone that is signed in to GlucoCore, choose
  who this display shows, and approve. It pairs itself — there is nothing to
  type back in here.</p>
  {ui.row("Or open this address", ui.copy_input("approve", shown,
                                                input_id="approve"),
          inline=False)}
  <div class="banner info" id="pairwait">Waiting to be approved&hellip;</div>
</div>"""

    def _pair_by_signin(self, form: dict) -> str:
        return f"""<form method="POST" action="/settings/glucocore/signin">
  {ui.row("Email", ui.text_input("email", form.get("email", ""), kind="email",
                                 input_id="gc_email",
                                 extra='autocapitalize="none" autocorrect="off"'
                                       ' spellcheck="false"'
                                       ' autocomplete="username"'),
          inline=False, for_id="gc_email")}
  {ui.row("Password", ui.password_input("password", "",
                                        input_id="gc_password"),
          inline=False, for_id="gc_password",
          hint="Used once, to create this display in GlucoCore. Only a"
               " read-only device token is kept afterwards.")}
  <div class="actions">
    <button type="submit">Sign in</button>
    <span class="note">Then choose who this display shows.</span>
  </div>
</form>"""

    def _pair_by_code(self, form: dict) -> str:
        return f"""<form method="POST" action="/settings/glucocore/pair">
  {ui.row("Pairing code",
          ui.text_input("code", form.get("code", ""),
                        placeholder="123456", input_id="pair_code",
                        extra='inputmode="numeric" autocomplete="one-time-code"'
                              ' pattern="[0-9 ]*" maxlength="9"'
                              ' autocapitalize="off" spellcheck="false"'),
          inline=False, for_id="pair_code",
          hint="In GlucoCore, open Devices and create a pairing code. It"
               " lasts ten minutes and works once.")}
  {ui.row("Name this display",
          ui.text_input("device_name", form.get("device_name", ""),
                        placeholder="Kitchen display",
                        input_id="device_name"),
          inline=False, for_id="device_name",
          hint="Optional — blank keeps the name you gave it in GlucoCore.")}
  <div class="actions">
    <button type="submit">Pair this display</button>
    <span class="note">Restarts the display, about five seconds.</span>
  </div>
</form>"""

    def _page_glucocore_people(self, draft: dict, *, form=None,
                               banner: str = "") -> str:
        """After signing in: who this display shows."""
        form = form or {}
        patients = draft.get("patients") or []
        chosen = set(_as_list(form.get("patient_ids"))
                     or [self._patient_id(p) for p in patients])
        boxes = "".join(
            ui.checkbox("patient_ids", self._patient_name(patient),
                        self._patient_id(patient) in chosen,
                        value=self._patient_id(patient))
            for patient in patients if self._patient_id(patient)
        )
        if not boxes:
            boxes = ('<p class="note">This account cannot see anyone yet. Add '
                     "a patient in GlucoCore, then sign in again.</p>")
        return f"""<h1>Who to show</h1>
<p class="lede">Signed in as <b>{ui.esc(draft.get('email', ''))}</b>. Choose
whose glucose this display pulls from GlucoCore.</p>
{banner}
<form method="POST" action="/settings/glucocore/register">
  {ui.row("Name this display",
          ui.text_input("device_name",
                        form.get("device_name", "") or draft.get("suggested", ""),
                        placeholder="Kitchen display", input_id="device_name"),
          inline=False, for_id="device_name")}
  <label class="lbl">Who to show</label>
  {boxes}
  <div class="actions stick">
    <button type="submit">Pair this display</button>
    <span class="note">Restarts the display, about five seconds.</span>
  </div>
</form>
<form method="POST" action="/settings/glucocore/cancel">
  <button type="submit" class="quiet">Not now — discard this sign-in</button>
</form>"""

    def _page_glucocore_paired(self, gc, banner: str = "") -> str:
        raw = self._raw_config()
        now_ms = int(time.time() * 1000)
        rows = []
        for user in raw.get("users") or []:
            source = user.get("source") or {}
            if source.get("type") != "glucocore":
                continue
            state = self._person_state(user, now_ms)
            rows.append((user.get("name") or "unnamed", ui.esc(state["text"])))
        if not rows:
            rows = [("Nobody yet", "Choose who this display shows in "
                                   "GlucoCore.")]
        return f"""<h1>GlucoCore</h1>
<p class="lede">This display is paired. Who appears on it, and their ranges,
follow what GlucoCore says — changes there arrive here on their own.</p>
{banner}
{ui.facts([("This display", ui.esc(gc.name or "unnamed")),
           ("Device ID", f'<code>{ui.esc(gc.device_id or "—")}</code>'),
           ("Hardware ID", f'<code>{ui.esc(gc.hardware_id or "—")}</code>')])}
<h2>Pulled from GlucoCore</h2>
{ui.facts(rows)}
<p class="note">The <a href="/log">sync log</a> shows what has arrived, and
when.</p>
<form method="POST" action="/settings/glucocore/unpair"
      onsubmit="return confirm('Unpair this display from GlucoCore?')">
  <div class="actions">
    <button type="submit" class="danger">Unpair this display</button>
    <span class="note">Everyone pulled from GlucoCore switches to their own
      uploader port and API secret, so the display keeps working while you
      point something at it. Readings already stored stay. Remove it in
      GlucoCore too, to take its token out of the account.</span>
  </div>
</form>"""

    UNIT_CARDS = (
        ("mg/dL", "mg/dL", "What the United States reads"),
        ("mmol/L", "mmol/L", "What most of the rest of the world reads"),
    )

    def _page_weather(self) -> str:
        raw = self._raw_config().get("weather") or {}
        enabled = bool(raw.get("enabled"))
        place = raw.get("place") or ""
        located = raw.get("latitude") is not None
        state = ""
        if enabled and located:
            reading = weather_mod.current(self.server.store)
            state = ui.banner("ok", f"Showing the weather for "
                                    f"{ui.esc(place or 'the saved location')}."
                              + ("" if reading else
                                 " Nothing fetched yet — the first reading "
                                 "arrives within a quarter of an hour."))
        elif enabled:
            state = ui.banner("warn", "Turned on, but this device does not "
                                      "know where it is yet.")
        return f"""<h1>Weather</h1>
<p class="lede">The temperature in the corner of the ambient screen. Off
until you say where the device is — a guess from the time zone would
confidently show the wrong town.</p>
{state}
<form method="POST" action="/settings/weather" data-dirty>
  {ui.row("Show the weather",
          ui.checkbox("enabled", "On", enabled), inline=False)}
  {ui.row("Town",
          ui.text_input("place", place, input_id="place",
                        placeholder="Sheffield"),
          for_id="place", inline=False,
          hint="Looked up once when you save, then never again. Clear it to "
               "forget where this device is.")}
  {ui.row("Temperature in",
          ui.select("units", (("fahrenheit", "Fahrenheit"),
                              ("celsius", "Celsius")),
                    raw.get("units", "fahrenheit"), input_id="units"),
          for_id="units")}
  {ui.save_bar()}
</form>"""

    def _page_ranges(self) -> str:
        display = self._raw_config().get("display", {})
        shown_in = units_mod.normalize(display.get("units"))
        step = units_mod.step(shown_in)
        decimals = 1 if units_mod.is_mmol(shown_in) else 0

        def value_of(key, default):
            stored = display.get(key)
            return units_mod.fmt_field(
                default if stored in (None, "") else stored, shown_in)

        def number(key, default) -> str:
            """A threshold, written in the unit this display reads in."""
            return ui.text_input(key, value_of(key, default), kind="number",
                                 input_id=key, css="num",
                                 extra=f'step="{step}"')

        low, high = value_of("low", 70), value_of("high", 180)
        urgent_low = value_of("urgent_low", 55)
        urgent_high = value_of("urgent_high", 250)
        # The bar spans the same range of glucose whichever unit it is
        # read in, so the axis is converted along with the numbers on it.
        axis = (units_mod.to_display(ui.RANGE_AXIS[0], shown_in),
                units_mod.to_display(ui.RANGE_AXIS[1], shown_in))
        return f"""<h1>Ranges</h1>
<p class="lede">What counts as in range, and where the numbers turn red.
Everyone shares these unless their own page overrides them.</p>
{self._flash()}
{ui.range_preview(low, high, urgent_low, urgent_high, axis=axis,
                  decimals=decimals)}
<form method="POST" action="/settings/ranges" data-dirty>
  {ui.choice_cards("units", self.UNIT_CARDS, shown_in, legend="Read in")}
  <input type="hidden" name="typed_units" value="{ui.esc(shown_in)}">
  <p class="note spaced">Switching this converts what is below rather than
  reinterpreting it &mdash; the boxes were filled in {ui.esc(shown_in)}, and
  saving keeps the thresholds where they are. They come back in the unit you
  chose.</p>
{ui.rule("In range · " + shown_in)}
<div class="pair">
  {ui.field("Low", number("low", 70))}
  {ui.field("High", number("high", 180))}
</div>
{ui.rule("Urgent · panel turns red")}
<div class="pair">
  {ui.field("Under", number("urgent_low", 55))}
  {ui.field("Over", number("urgent_high", 250))}
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
<div class="banner ok strip" id="tzdetected" hidden>
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
        # A name typed into "Other network" has to survive a failed join, or
        # the only way to retry a hidden network is to type it again.
        attempted = wifi.get("ssid", "")
        in_list = any(net.get("ssid") == attempted for net in networks)
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
  {ui.network_picker(networks, selected=attempted,
                     other_ssid="" if in_list else attempted,
                     hidden=bool(wifi.get("hidden")))}
  <p class="note spaced">{hint}</p>
  <div data-wifi-password>{ui.row("Password",
      ui.password_input("wifi_password", "", input_id="wifi_password"),
      for_id="wifi_password",
      hint="Check it before joining &mdash; the device drops off the network "
           "while it tries, and a wrong password takes a minute or two to "
           "come back.")}</div>
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
        mode = "on" if password else "off"
        link = config_mod.admin_url(
            self._lan_ip(), config.admin_port,
            f"/settings?key={password}" if password else "/settings")
        lede = ("This page and the dashboard need a password."
                if password else
                "This page and the dashboard are open to anyone on this "
                "network.")
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
        field = ui.row(
            "New password" if password else "Password",
            ui.password_input("admin_password", "",
                              input_id="admin_password"),
            for_id="admin_password",
            hint="At least six characters. You will stay logged in on this "
                 "phone." + (" Leave it blank to keep the one in use."
                             if password else ""))
        # Said here rather than only in the card, because turning the
        # password off is the one choice on this page that cannot be
        # undone from somewhere else if the network turns out to be
        # shared.
        open_note = (
            '<p class="note">Fine on a home network you trust &mdash; the '
            "device is only reachable from it, and there is nothing to look "
            "up on a phone to get in. Not fine on a network guests, flatmates "
            "or an office share.</p>")
        return f"""<h1>Access</h1>
<p class="lede">{lede}</p>
{self._flash()}
{current}
{autolink}
<form method="POST" action="/settings/access" data-dirty>
  {ui.choice_cards("mode", self.ACCESS_CARDS, mode, controls="access",
                   legend="Getting in")}
  {ui.group("access", "on", field, current=mode)}
  {ui.group("access", "off", open_note, current=mode)}
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
        if kind == "glucocore":
            # start_pollers also needs the device token, which lives on
            # the config rather than on the person — _person_state passes
            # that in by checking the pairing before it asks.
            return bool(source.get("patient_id"))
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
        elif path == "/settings/glucocore":
            title, body = "GlucoCore", self._page_glucocore()
            script = PAIRING_SCRIPT
        elif path == "/settings/weather":
            title, body = "Weather", self._page_weather()
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
        # Ahead of the read below, because that one decodes the body as
        # text and a JPEG is not text. This is the only route that takes
        # bytes, and it reads its own body with its own cap.
        if post_path == "/settings/person/wallpaper":
            self._post_wallpaper()
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        if post_path == "/api/source/test":
            self._test_source(raw_body)
            return
        # keep_blank_values matters: blank means "unchanged" for every
        # credential field, and a dropped key cannot say that.
        form = {
            k: (v[0] if len(v) == 1 else v)
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
        if post_path in ("/settings/glucocore/pair",
                         "/settings/glucocore/signin",
                         "/settings/glucocore/register",
                         "/settings/glucocore/cancel",
                         "/settings/glucocore/unpair"):
            self._post_glucocore(post_path, form)
            return
        if post_path == "/wifi":
            # The typed name answers only when "Other network" is what was
            # chosen. With scripts off that box is always on the page, so a
            # name left in it used to override the network actually tapped.
            picked = form.get("wifi_ssid", "").strip()
            if picked and picked != "__other__":
                ssid, hidden = picked, False
            else:
                ssid = form.get("wifi_other_ssid", "").strip()
                hidden = bool(form.get("wifi_hidden"))
            password = form.get("wifi_password", "")
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
        "/settings/screen": "_save_screen",
        "/settings/weather": "_save_weather",
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

    # ---- settings: pairing with GlucoCore ----

    def _post_glucocore(self, path: str, form: dict) -> None:
        if path == "/settings/glucocore/unpair":
            self._unpair_glucocore()
            return
        if path == "/settings/glucocore/cancel":
            self._clear_signin_draft()
            self._send(b"", "text/html", 303,
                       {"Location": "/settings/glucocore?msg=signedout"})
            return
        if self._pairing_config():
            # Already paired. Unpairing is the way to move a display to
            # another account, and doing it deliberately beats a second
            # pairing landing on top of the first.
            self._send(b"", "text/html", 303,
                       {"Location": "/settings/glucocore"})
            return
        if path == "/settings/glucocore/signin":
            self._glucocore_signin(form)
        elif path == "/settings/glucocore/register":
            self._glucocore_register(form)
        else:
            self._glucocore_pair(form)

    def _glucocore_page(self, form, banner: str, code: int = 400) -> None:
        self._send(ui.page("GlucoCube — GlucoCore",
                           self._page_glucocore(form=form, banner=banner),
                           nav=True, back="/settings",
                           script=PAIRING_SCRIPT).encode(),
                   "text/html; charset=utf-8", code)

    def _paired(self, device: dict, device_token: str, form: dict) -> None:
        """The one ending all three ways of pairing share."""
        try:
            sync.write_pairing(self.server.config_path, device, device_token,
                               network.hardware_id(),
                               admin_port=self.server.config.admin_port,
                               store=self.server.store)
        except Exception as exc:  # noqa: BLE001
            self._glucocore_page(form, ui.banner("err", ui.esc(str(exc))))
            return
        self._clear_signin_draft()
        pairing.clear(self.server.store)
        self._send(self._applying_page("/settings/glucocore?msg=paired",
                                       "Paired"),
                   "text/html; charset=utf-8")
        restart_soon()

    def _glucocore_pair(self, form: dict) -> None:
        """A six-digit code, typed here."""
        device_name = form.get("device_name", "").strip()
        result, claimed = verify.glucocore_claim(form.get("code", ""),
                                                 network.hardware_id(),
                                                 device_name)
        if not result.ok:
            self._glucocore_page({**form, "how": "code"},
                                 ui.failure(result.message, result.detail))
            return
        self._paired(claimed.get("device") or {}, claimed["deviceToken"],
                     {**form, "how": "code"})

    def _glucocore_signin(self, form: dict) -> None:
        """An account signed in to, here, to create the display."""
        email = form.get("email", "").strip()
        result, session = verify.glucocore_session(email,
                                                   form.get("password", ""))
        if not result.ok or not session.get("token"):
            self._glucocore_page({**form, "how": "signin"},
                                 ui.failure(result.message, result.detail))
            return
        self.server.store.replace_params(self.SIGNIN_KEY, {
            "token": session["token"],
            "email": email,
            "patients": session.get("patients") or [],
            "suggested": socket.gethostname().split(".")[0],
            "started_at": int(time.time() * 1000),
        })
        self._send(b"", "text/html", 303, {"Location": "/settings/glucocore"})

    def _glucocore_register(self, form: dict) -> None:
        draft = self._signin_draft()
        if not draft:
            self._glucocore_page({}, ui.banner(
                "err", "That sign-in has expired — sign in again."))
            return
        known = {self._patient_id(patient)
                 for patient in draft.get("patients") or []}
        known.discard("")
        # Only ids this account was actually shown: a hand-edited form must
        # not pair a display to somebody else's data.
        patient_ids = [pid for pid in _as_list(form.get("patient_ids"))
                       if pid in known]
        if not patient_ids:
            self._glucocore_page(form, ui.banner(
                "err", "Choose at least one person to show."))
            return
        name = (form.get("device_name", "").strip()
                or draft.get("suggested") or "SugarCube")
        result, registered = verify.glucocore_register(
            draft["token"], name, network.hardware_id(), patient_ids,
            display=self._raw_config().get("display") or {})
        if not result.ok:
            self._glucocore_page(form, ui.failure(result.message,
                                                  result.detail), 502)
            return
        self._paired(registered.get("device") or {},
                     registered["deviceToken"], form)

    def _unpair_glucocore(self) -> None:
        raw = self._raw_config()
        users = raw.get("users") or []
        for user in users:
            if (user.get("source") or {}).get("type") != "glucocore":
                continue
            # Left with no source at all they become push people: their own
            # port and API secret, ready for an uploader. Anything else
            # would take their panel off the screen along with the pairing.
            user.pop("source", None)
            user["port"] = None
            user["api_secret"] = (user.get("api_secret")
                                  or config_mod.readable_secret(16))
        raw.pop("glucocore", None)
        config_mod.assign_ports(users,
                                reserved={self.server.config.admin_port})
        raw["users"] = users
        try:
            config_mod.write_atomic(raw, self.server.config_path)
        except Exception as exc:  # noqa: BLE001
            self._send(self._error_page(str(exc), "/settings/glucocore"),
                       "text/html; charset=utf-8", 400)
            return
        # The config version the push listener was tracking belongs to a
        # pairing that no longer exists.
        self.server.store.replace_params(sync.LAST_VERSION_KEY, {})
        log.info("Unpaired from GlucoCore; restarting")
        self._send(self._applying_page("/settings/glucocore?msg=unpaired",
                                       "Unpaired"),
                   "text/html; charset=utf-8")
        restart_soon()

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
        shown_in = units_mod.normalize(
            raw.get("display", {}).get("units"))
        for key, label in (("low", "Low"), ("high", "High"),
                           ("urgent_low", "Urgent low"),
                           ("urgent_high", "Urgent high")):
            value = _number(form, f"th_{key}", label)
            if value is not None:
                thresholds[key] = units_mod.from_display(value, shown_in)
        if thresholds:
            _check_ranges({**self._range_defaults(), **thresholds})
            user["thresholds"] = thresholds
        art = (form.get("wallpaper") or "").strip()
        if art and not self._known_wallpaper(art):
            raise ValueError("That background is not on this device.")
        if art:
            user["wallpaper"] = art
        elif prior.get("wallpaper") and "wallpaper" not in form:
            # The form did not render the field — keep what is there rather
            # than reading its absence as a choice.
            user["wallpaper"] = prior["wallpaper"]
        seconds = _number(form, "rotate_seconds", "Seconds on screen")
        if seconds is not None:
            if not 3 <= seconds <= 300:
                raise ValueError("Seconds on screen has to be between 3 "
                                 "and 300.")
            user["rotate_seconds"] = seconds
        kind = form.get("source", "push")
        # Each pull source carries its own interval inside its own card, so
        # both fields reach the server and only the chosen one is read.
        poll_field = ("ns_poll" if kind == "nightscout" and "ns_poll" in form
                      else "poll")
        poll = max(15, int(_number(form, poll_field, "Check every") or 60))
        if prior_source.get("type") == "glucocore" and "source" not in form:
            # The page showed this person's source as read-only, so the
            # form says nothing about it. Without this, saving a rename or
            # a threshold would quietly unlink them from GlucoCore and
            # leave a panel with nothing feeding it.
            user["source"] = prior_source
        elif kind == "tidepool":
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

    # The most this device will hold in memory for a picture, and the
    # most a 7-inch panel can use. GlucoCore caps uploads at the same
    # figure; a display should not be the looser of the two.
    MAX_WALLPAPER_BYTES = 2 * 1024 * 1024

    def _save_screen(self, form: dict) -> None:
        raw = self._raw_config()
        display = raw.setdefault("display", {})
        display["layout"] = config_mod.normalize_layout(form.get("layout"))
        display["split_direction"] = config_mod.normalize_split_direction(
            form.get("split_direction"))
        # Blank means everyone, which is the absence of a cap rather than a
        # number — so the key goes away instead of holding the last value.
        cap = (form.get("split_max") or "").strip()
        if cap.isdigit() and 1 <= int(cap) <= 6:
            display["split_max"] = int(cap)
        else:
            display.pop("split_max", None)

        seconds = _number(form, "rotate_seconds", "Seconds each person")
        if seconds is not None:
            if not 3 <= seconds <= 300:
                raise ValueError("Seconds each person has to be between 3 "
                                 "and 300.")
            display["rotate_seconds"] = seconds
        for key, label in (("wallpaper_dim", "Dim the art by"),
                           ("night_dim_boost", "Dim further overnight by")):
            value = _number(form, key, label)
            if value is not None:
                if not 0 <= value <= 100:
                    raise ValueError(f"{label} has to be between 0 and 100.")
                display[key] = value

        art = (form.get("wallpaper") or "").strip()
        if art and not self._known_wallpaper(art):
            raise ValueError("That background is not on this device.")
        display["wallpaper"] = art
        config_mod.write_atomic(raw, self.server.config_path)

    def _known_wallpaper(self, value: str) -> bool:
        """A background this device can actually draw.

        Checked rather than trusted, because a form field is not a fact:
        an unknown value is a black screen with nothing to say why.
        """
        if value == "none":
            return True
        bundled = wallpaper_mod.BUNDLED_RE.match(value)
        if bundled:
            return bundled.group(1) in wallpaper_mod.bundled_names()
        if wallpaper_mod.is_id(value):
            return wallpaper_mod.cached_path(
                self.server.config.database, value).exists()
        return False

    def _save_weather(self, form: dict) -> None:
        raw = self._raw_config()
        block = raw.setdefault("weather", {})
        block["enabled"] = bool(form.get("enabled"))
        block["units"] = config_mod.normalize_temperature_units(
            form.get("units"))
        asked = (form.get("place") or "").strip()
        if not asked:
            # Cleared on purpose: forget where this device is rather than
            # keeping coordinates nobody can see on a page that says none.
            block.pop("latitude", None)
            block.pop("longitude", None)
            block["place"] = ""
        elif asked != (block.get("place") or ""):
            # Resolved once, here, rather than on every poll: the poller
            # should need nothing but two numbers.
            try:
                found = weather_mod.geocode(asked)
            except Exception as exc:  # noqa: BLE001 - shown, never a crash
                raise ValueError(
                    f"Could not look that up: {exc}") from exc
            if not found:
                raise ValueError(f"Nowhere called {asked} was found.")
            block.update(found)
        config_mod.write_atomic(raw, self.server.config_path)

    def _post_wallpaper(self) -> None:
        """Take a picture for one person, or for the display.

        Stored under a name that is the digest of its own bytes, which
        makes it the same shape as an id from GlucoCore — so everything
        downstream, the cache and the draw loop included, needs to know
        nothing about where a background came from. It also means the same
        picture uploaded twice is one file.
        """
        index = self._person_index()
        back = ("/settings/people" if index is None
                else f"/settings/person?i={index}")
        try:
            boundary = multipart_mod.boundary_of(
                self.headers.get("Content-Type", ""))
            if not boundary:
                raise ValueError("That form did not send a file.")
            body = multipart_mod.read_body(self, self.MAX_WALLPAPER_BYTES)
            fields = multipart_mod.parse(body, boundary)
            part = fields.get("image")
            if not isinstance(part, tuple) or not part[1]:
                raise ValueError("No picture was chosen.")
            data = part[1]
            if len(data) > self.MAX_WALLPAPER_BYTES:
                raise multipart_mod.TooLarge(
                    "that file is larger than 2 MB")
            if not multipart_mod.looks_like_image(data):
                raise ValueError("That file is not a JPEG or a PNG.")
        except multipart_mod.TooLarge as exc:
            self._send(self._error_page(str(exc), back),
                       "text/html; charset=utf-8", 413)
            return
        except Exception as exc:  # noqa: BLE001 - shown, never a crash
            self._send(self._error_page(str(exc), back),
                       "text/html; charset=utf-8", 400)
            return

        name = hashlib.sha256(data).hexdigest()[:32]
        wallpaper_mod.cached_path(self.server.config.database, name).parent \
            .mkdir(parents=True, exist_ok=True)
        wallpaper_mod._write_atomic(
            wallpaper_mod.cached_path(self.server.config.database, name), data)

        raw = self._raw_config()
        if index is None or index == "new":
            raw.setdefault("display", {})["wallpaper"] = name
        else:
            users = raw.get("users") or []
            if index < len(users):
                users[index]["wallpaper"] = name
        try:
            config_mod.write_atomic(raw, self.server.config_path)
        except Exception as exc:  # noqa: BLE001
            self._send(self._error_page(str(exc), back),
                       "text/html; charset=utf-8", 400)
            return
        log.info("Wallpaper uploaded (%d bytes); restarting", len(data))
        self._send(self._applying_page(f"{back}?msg=saved"),
                   "text/html; charset=utf-8")
        restart_soon()

    def _save_ranges(self, form: dict) -> None:
        raw = self._raw_config()
        display = raw.setdefault("display", {})
        # Two units in play, and telling them apart is the whole of it. The
        # boxes hold numbers in the unit the page was *rendered* in, which
        # a switch on this very form does not retroactively change; the
        # radio says what to read in from now on. Reading the boxes in the
        # newly chosen unit would silently move somebody's urgent low.
        typed_in = units_mod.normalize(form.get("typed_units")
                                       or display.get("units"))
        chosen = units_mod.normalize(form.get("units") or typed_in)
        values = {"units": chosen}
        for key, label in (("low", "The low"), ("high", "The high"),
                           ("urgent_low", "The urgent low"),
                           ("urgent_high", "The urgent high")):
            value = _number(form, key, label)
            if value is not None:
                values[key] = units_mod.from_display(value, typed_in)
        stale = _number(form, "stale_minutes", "Stale after")
        if stale is not None:
            values["stale_minutes"] = stale
        if values.get("stale_minutes", 1) <= 0:
            raise ValueError("Stale after has to be at least one minute.")
        _check_ranges({**self._range_defaults(),
                       **{k: v for k, v in values.items() if k != "units"}})
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
