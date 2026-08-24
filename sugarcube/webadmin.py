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
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import secrets as secrets_mod

from . import config as config_mod
from . import network, predict, synclog, ui, updater, verify
from .server import DualStackServer
from .config import SCREEN_PNG, Config, merged_thresholds
from .store import Store

log = logging.getLogger("sugarcube.webadmin")

SETTINGS_SCRIPT = """<script>
setInterval(() => {
  const img = document.getElementById('screen');
  if (img) img.src = '/screen.png?t=' + Date.now();
}, 5000);

function removePerson(i) {
  document.querySelector('[name=u' + i + '_remove]').value = '1';
  document.getElementById('fs' + i).hidden = true;
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
  window.sugarSync();
}

// "Test connection": check the credentials before saving, rather than
// finding out from the sync log hours later that a letter was wrong.
document.addEventListener('click', async (event) => {
  const button = event.target.closest('button.test');
  if (!button) return;
  const i = button.dataset.i;
  const out = document.getElementById('testresult' + i);
  const value = (field) => {
    const el = document.querySelector('[name="u' + i + '_' + field + '"]');
    return el ? el.value : '';
  };
  const picked = document.querySelector('[name="u' + i + '_source"]:checked');
  out.hidden = false;
  out.className = 'banner info';
  out.textContent = 'Testing\u2026';
  button.disabled = true;
  try {
    const response = await fetch('/api/source/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        index: i, type: picked ? picked.value : 'push',
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

// A rescan runs in the background; the page asks whether it has finished
// instead of the server holding the request open while it waits.
if (location.search.indexOf('scanning=1') >= 0) {
  const tick = async () => {
    try {
      const r = await fetch('/api/wifi.json', {cache: 'no-store'});
      if (!(await r.json()).scanning) { location.replace('/settings'); return; }
    } catch (err) {}
    setTimeout(tick, 1500);
  };
  setTimeout(tick, 1500);
}
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
        cookie = getattr(self, "_cookie_value", None)
        if cookie is None and getattr(self, "_grant_cookie", False):
            cookie = self.server.password
        if cookie:
            # After a password change this has to carry the NEW one, or the
            # browser is locked out the instant the process restarts — on a
            # page the user has already navigated away from.
            self.send_header(
                "Set-Cookie",
                f"sugarcube_key={cookie}; Path=/;"
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
            page = ui.page("SugarCube sync log", LOG_BODY, nav=True,
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
            name = os.path.basename(path)
            font_path = os.path.join(os.path.dirname(__file__), "fonts", name)
            if name.endswith(".ttf") and os.path.isfile(font_path):
                with open(font_path, "rb") as f:
                    self._send(f.read(), "font/ttf",
                               extra={"Cache-Control": "max-age=604800"})
            else:
                self._send(b"not found", "text/plain", 404)
        else:
            self._send(ui.page("Not found", "<h1>Not found</h1>"
                               '<p><a href="/settings">Back to settings</a></p>'
                               ).encode(), "text/html; charset=utf-8", 404)

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

    def _user_fieldset(self, i, user: dict, status: str, defaults: dict) -> str:
        source = user.get("source") or {}
        stype = source.get("type") or "push"
        control = f"src{i}"
        th = user.get("thresholds") or {}
        th_val = lambda key: str(th[key]) if th.get(key) else ""

        # The port and the push secret belong to the push source and to
        # nothing else — someone on twiist was being asked for a "Port
        # (Nightscout API)" they will never use. They stay in the form
        # (hidden inputs still submit, so the port round-trips) but they
        # are only *shown* for the source that needs them.
        # Built by hand rather than via admin_url(): the "add a person"
        # template carries __PORT__ here, which the browser substitutes.
        push_url = f"http://{self._lan_ip()}:{user.get('port', '')}"
        push = (
            ui.row("Port", ui.text_input(f"u{i}_port", user.get("port", ""),
                                         kind="number",
                                         input_id=f"u{i}_port"),
                   for_id=f"u{i}_port",
                   hint="The uploader connects to this port on this device.")
            + ui.row("URL for the uploader",
                     ui.copy_input(f"u{i}_url", push_url,
                                   input_id=f"u{i}_url"))
            + ui.row("API secret",
                     ui.copy_input(f"u{i}_secret", user.get("api_secret", ""),
                                   input_id=f"u{i}_secret"),
                     for_id=f"u{i}_secret",
                     hint="Enter both in Trio under Settings &rarr; Services"
                          " &rarr; Nightscout.")
        )
        tidepool = (
            ui.row("Tidepool email",
                   ui.text_input(f"u{i}_tp_email", source.get("email", "")
                                 if stype == "tidepool" else "",
                                 kind="email", input_id=f"u{i}_tp_email",
                                 extra='autocapitalize="none" autocorrect="off"'
                                       ' spellcheck="false"'),
                   for_id=f"u{i}_tp_email")
            # Stored credentials are never rendered back into the page:
            # blank means "keep what is saved".
            + ui.row("Tidepool password",
                     ui.password_input(f"u{i}_tp_password", "",
                                       placeholder=self._secret_placeholder(
                                           stype == "tidepool",
                                           source.get("password")),
                                       input_id=f"u{i}_tp_password"),
                     for_id=f"u{i}_tp_password")
        )
        ns_key = source.get("api_secret") or source.get("token") or ""
        nightscout = (
            ui.row("Nightscout address",
                   ui.text_input(f"u{i}_ns_url", source.get("url", "")
                                 if stype == "nightscout" else "",
                                 kind="url", placeholder="mysite.example.com",
                                 input_id=f"u{i}_ns_url",
                                 extra='autocapitalize="none" autocorrect="off"'
                                       ' spellcheck="false"'),
                   for_id=f"u{i}_ns_url")
            + ui.row("API secret or token",
                     ui.password_input(f"u{i}_ns_key", "",
                                       placeholder=self._secret_placeholder(
                                           stype == "nightscout", ns_key),
                                       input_id=f"u{i}_ns_key"),
                     for_id=f"u{i}_ns_key",
                     hint="Either works — SugarCube works out which.")
        )
        pull_extra = (
            ui.row("Check every", ui.text_input(
                f"u{i}_poll", source.get("poll_seconds", 60), kind="number",
                input_id=f"u{i}_poll"), for_id=f"u{i}_poll", hint="seconds")
            + '<button type="button" class="test secondary" data-i="{}"'
              ' data-needs-js hidden>Test connection</button>'.format(i)
            + f'<div class="banner" id="testresult{i}" hidden></div>'
        )
        return f"""
<fieldset class="person" data-i="{i}" id="fs{i}">
  <legend>{ui.esc(user.get("name") or "New person")}</legend>
  <input type="hidden" name="u{i}_remove" value="">
  <input type="hidden" name="u{i}_prev_name" value="{ui.esc(user.get("name", ""))}">
  <p class="note">{ui.esc(status)}</p>
  {ui.row("Name", ui.text_input(f"u{i}_name", user.get("name", ""),
                                input_id=f"u{i}_name"), for_id=f"u{i}_name")}
  <label class="lbl">Where the data comes from</label>
  {ui.choice_cards(f"u{i}_source", self.SOURCE_CARDS, stype, controls=control)}
  {ui.group(control, "push", push, current=stype)}
  {ui.group(control, "tidepool", tidepool, current=stype)}
  {ui.group(control, "nightscout", nightscout, current=stype)}
  {ui.group(control, ["tidepool", "nightscout"], pull_extra, current=stype)}
  {ui.row("Low / high", f'<div class="pair">'
          + ui.text_input(f"u{i}_th_low", th_val("low"), kind="number",
                          placeholder=f"{defaults['low']:g}")
          + ui.text_input(f"u{i}_th_high", th_val("high"), kind="number",
                          placeholder=f"{defaults['high']:g}") + "</div>")}
  {ui.row("Urgent low / high", f'<div class="pair">'
          + ui.text_input(f"u{i}_th_urgent_low", th_val("urgent_low"),
                          kind="number",
                          placeholder=f"{defaults['urgent_low']:g}")
          + ui.text_input(f"u{i}_th_urgent_high", th_val("urgent_high"),
                          kind="number",
                          placeholder=f"{defaults['urgent_high']:g}")
          + "</div>", hint="Blank uses the defaults below.")}
  <button type="button" class="danger" onclick="removePerson('{i}')">Remove this person</button>
</fieldset>"""

    def _lan_ip(self) -> str:
        """One lookup per page render, not one per person."""
        if not getattr(self, "_lan_ip_cache", ""):
            self._lan_ip_cache = network.get_lan_ip()
        return self._lan_ip_cache

    @staticmethod
    def _secret_placeholder(is_current: bool, stored) -> str:
        if is_current and stored:
            return "(unchanged — type to replace)"
        return ""

    def _wifi_section(self) -> str:
        if not network.available():
            return ""
        hotspot = network.hotspot_active()
        wifi = network.state()
        notices = []
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
        return f"""<h2>Wi-Fi</h2>
{''.join(notices)}
<form method="POST" action="/wifi">
  {ui.network_picker(networks, selected=wifi.get("ssid", ""))}
  {ui.row("Password", ui.password_input("wifi_password", "",
                                        input_id="wifi_password"),
          inline=False, for_id="wifi_password")}
  <p class="note">{hint}</p>
  <div class="actions"><button type="submit">Join network</button></div>
</form>
{rescan}"""

    def _screen_section(self) -> str:
        """Live screen, plus a way to switch its theme from here.

        The sun/moon on the device is the usual way; this is the fallback
        for a panel whose touch controller we cannot read.
        """
        theme = self.server.store.get_params("__display").get("theme", "dark")
        other, label = (("light", "Switch to Day")
                        if theme == "dark" else ("dark", "Switch to Night"))
        return f"""<h2>The screen</h2>
<img class="screen" id="screen" src="/screen.png" alt="what the display shows"
     onerror="this.hidden=true">
<form method="POST" action="/display/theme">
  <input type="hidden" name="theme" value="{other}">
  <div class="actions">
    <button type="submit" class="secondary">{label}</button>
    <span class="note">Currently {"night" if theme == "dark" else "day"}.
      You can also tap the sun or moon on the device.</span>
  </div>
</form>"""

    def _updates_section(self) -> str:
        state = self.server.store.get_params(updater.PARAMS_KEY)
        current = updater.current_version()
        if state.get("checked_at"):
            import time
            checked = time.strftime("%H:%M",
                                    time.localtime(state["checked_at"] / 1000))
            if state.get("error"):
                status = f"last check at {checked} failed: {ui.esc(state['error'])}"
            elif state.get("available"):
                status = (f"version <b>{ui.esc(state.get('latest', '?'))}</b> is "
                          f"available (checked {checked}) — "
                          f"<a href=\"{ui.esc(state.get('url', ''))}\">release notes</a>")
            else:
                status = f"up to date (checked {checked})"
        else:
            status = "not checked yet — checks run every 6 hours"
        install = ""
        if state.get("available"):
            install = f"""
  <form method="POST" action="/update/apply">
    <input type="hidden" name="tag" value="{ui.esc(state.get('latest_tag', ''))}">
    <button type="submit">Install {ui.esc(state.get('latest', ''))}</button>
  </form>"""
        return f"""<h2>Updates</h2>
<fieldset><legend>Software</legend>
  <p>SugarCube {ui.esc(current)} — {status}</p>
  <form method="POST" action="/update/check">
    <button type="submit" class="quiet">Check now</button>
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
            source = user.get("source") or {}
            if snap.sgv_date:
                mins = int((now_ms - snap.sgv_date) / 60000)
                status = f"last reading {snap.sgv:.0f} mg/dL, {mins}m ago"
            elif source.get("type") and not self._source_ready(source):
                status = ("source chosen but not running — its credentials "
                          "are missing")
            else:
                status = "no data yet"
            fieldsets.append(self._user_fieldset(i, user, status, defaults))
        template = self._user_fieldset(
            "__I__", {"port": "__PORT__"}, "not saved yet", defaults
        )
        body = f"""<h1>Settings</h1>
<p class="lede"><a href="/setup">Run guided setup</a> for a step-by-step
version of this page.</p>
{self._screen_section()}
{self._wifi_section()}
{self._updates_section()}
<form method="POST" action="/save">
<h2>People</h2>
<div id="people">{''.join(fieldsets)}</div>
<button type="button" class="secondary" onclick="addPerson()">Add a person</button>
<template id="person-template">{template}</template>
<h2>Display defaults</h2>
<fieldset><legend>Used unless a person overrides them (mg/dL)</legend>
  {ui.row("Low", ui.text_input("low", d('low', 70), kind="number"))}
  {ui.row("High", ui.text_input("high", d('high', 180), kind="number"))}
  {ui.row("Urgent low", ui.text_input("urgent_low", d('urgent_low', 55), kind="number"))}
  {ui.row("Urgent high", ui.text_input("urgent_high", d('urgent_high', 250), kind="number"))}
  {ui.row("Stale after", ui.text_input("stale_minutes", d('stale_minutes', 12),
                                       kind="number"), hint="minutes")}
</fieldset>
<h2>Admin</h2>
<fieldset><legend>Web access</legend>
  {ui.row("New password", ui.password_input("admin_password", "",
          placeholder="(leave blank to keep the current one)",
          input_id="admin_password"), for_id="admin_password",
          hint="Protects this page and the API. The username is"
               " <b>admin</b>. You will be asked to log in again.")}
</fieldset>
<div class="actions stick">
  <button type="submit">Save &amp; apply</button>
  <span class="note">Restarts the display, about 5 seconds.</span>
</div>
</form>"""
        return ui.page("SugarCube settings", body, nav=True,
                       script=SETTINGS_SCRIPT)

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
            " in about a minute.</p>", refresh="45;url=/settings").encode()

    def do_POST(self):
        if not self._authorized():
            self._deny()
            return
        post_path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        if post_path == "/api/source/test":
            self._test_source(raw_body)
            return
        form = {
            k: v[0]
            for k, v in parse_qs(raw_body.decode()).items()
        }
        if post_path == "/display/theme":
            theme = form.get("theme")
            if theme in ("dark", "light"):
                # The display picks this up on its next frame.
                self.server.store.set_params("__display", {"theme": theme})
            self._send(b"", "text/html", 303, {"Location": "/settings"})
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
                       {"Location": "/settings?scanning=1"})
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
                self._send(ui.page(
                    "Update failed",
                    f"<h1>Update failed</h1><p>{ui.esc(detail)}</p>"
                    '<p><a href="/settings">Back to settings</a></p>',
                ).encode(), "text/html; charset=utf-8", 500)
            return
        if post_path != "/save":
            self._send(b"not found", "text/plain", 404)
            return
        try:
            self._save(form)
        except Exception as exc:
            self._send(ui.page(
                "That did not save",
                f"<h1>That did not save</h1><p>{ui.esc(exc)}</p>"
                '<p><a href="/settings">Back to settings</a></p>',
            ).encode(), "text/html; charset=utf-8", 400)
            return
        self._send(ui.page(
            "Saved", "<h1>Saved</h1><p>Restarting the display&hellip; this "
            "page reloads in a few seconds.</p>", refresh="8;url=/settings",
        ).encode(), "text/html; charset=utf-8")
        log.info("Config saved from web admin; restarting")
        restart_soon()

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
reason is shown at the top of the Wi-Fi section — the device's own screen
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

    def _save(self, form: dict) -> None:
        raw = json.loads(open(self.server.config_path).read())
        # Keyed by the name the form was rendered with, so a blank
        # credential field can inherit the saved value.
        previous = {u.get("name"): u for u in raw.get("users", [])}
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
            prior = previous.get(form.get(f"u{idx}_prev_name", "").strip()) or {}
            prior_source = prior.get("source") or {}
            stype = form.get(f"u{idx}_source")
            # The port is only shown for push people; for everyone else it
            # arrives from a hidden input, or not at all on a brand new
            # person. assign_ports() fills the gaps below.
            port = (form.get(f"u{idx}_port") or "").strip()
            user = {
                "name": name,
                "port": int(port) if port.isdigit() else prior.get("port"),
                "api_secret": (form.get(f"u{idx}_secret", "").strip()
                               or prior.get("api_secret")
                               or secrets_mod.token_hex(12)),
            }
            thresholds = {}
            for key in ("low", "high", "urgent_low", "urgent_high"):
                value = form.get(f"u{idx}_th_{key}", "").strip()
                if value:
                    thresholds[key] = float(value)
            if thresholds:
                user["thresholds"] = thresholds
            poll = int(form.get(f"u{idx}_poll", 60) or 60)
            if stype == "tidepool":
                user["source"] = {
                    "type": "tidepool",
                    "email": form.get(f"u{idx}_tp_email", "").strip(),
                    # Blank means "keep what is saved": stored secrets are
                    # never rendered back into the page, so an untouched
                    # field must not wipe them.
                    "password": (form.get(f"u{idx}_tp_password", "")
                                 or self._kept_secret(prior_source, "tidepool",
                                                      "password")),
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
                    "api_secret": (form.get(f"u{idx}_ns_key", "").strip()
                                   or self._kept_secret(prior_source,
                                                        "nightscout",
                                                        "api_secret", "token")),
                    "poll_seconds": poll,
                }
            users.append(user)
        if not users:
            raise ValueError("at least one person is required")
        # Ports stay unique even though most people never see one; a
        # duplicate would make config.load() reject the file at boot.
        config_mod.assign_ports(users, reserved={self.server.config.admin_port})
        raw["users"] = users
        display = raw.setdefault("display", {})
        for key in ("low", "high", "urgent_low", "urgent_high", "stale_minutes"):
            if form.get(key):
                display[key] = float(form[key])

        new_admin_password = form.get("admin_password", "").strip()
        if new_admin_password:
            if len(new_admin_password) < 6:
                raise ValueError("the admin password must be at least 6 characters")
            raw.setdefault("admin", {})["password"] = new_admin_password
            # Otherwise this browser's cookie stops matching the moment the
            # new process starts, and the page it was just on is gone.
            self._cookie_value = new_admin_password

        config_mod.write_atomic(raw, self.server.config_path)

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
