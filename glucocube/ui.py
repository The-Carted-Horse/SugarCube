"""Shared HTML, CSS and JS for the web UI.

Everything the device serves is typed on a phone first: onboarding happens
over the setup hotspot, where the browser has no internet, so every page is
self-contained — no CDN, no fonts to fetch, no build step. The stylesheet is
mobile-first and widens at 640px rather than the other way round.

The components here exist because the same three mistakes kept recurring in
hand-written markup: fields that can't be revealed, options that can't be
tapped, and conditional sections that flash into view before the script at
the bottom of the page hides them. Each helper renders complete, correct
markup server-side and degrades to something usable with JavaScript off.
"""

import html

# ---------------------------------------------------------------- style ----

STYLE = """
:root, [data-theme=dark] { color-scheme: dark;
  --bg:#0d1117; --card:#161b22; --raise:#1c232c; --line:#2d333b; --fg:#ebeef1;
  --dim:#9aa4af; --faint:#6e7681; --accent:#58a6ff; --btn:#238636;
  --btn-fg:#ffffff; --danger:#f85149; --ok:#3fb950; --warn:#d29922; }
[data-theme=light] { color-scheme: light;
  --bg:#f4f6f8; --card:#ffffff; --raise:#eaeef2; --line:#c6ccd3; --fg:#1a2027;
  --dim:#5c6670; --faint:#8a939c; --accent:#0969da; --btn:#1a7f37;
  --btn-fg:#ffffff; --danger:#ce2626; --ok:#1a7f37; --warn:#9a6700; }

*, *::before, *::after { box-sizing: border-box; }
/* Load-bearing: nearly every rule below sets `display`, and a bare
   [hidden] (specificity 0,1,0, from the UA sheet) loses to all of them.
   Without this, a section the server rendered hidden shows anyway. */
[hidden] { display: none !important; }
/* ...except with JavaScript off, where no script can ever reveal them:
   better a page showing every field than one with no way forward. */
.reveal-nojs[hidden] { display: block !important; }
html { -webkit-text-size-adjust: 100%; }
body { margin:0; background:var(--bg); color:var(--fg);
  font-family:-apple-system, BlinkMacSystemFont, system-ui, "Segoe UI", sans-serif;
  font-size:16px; line-height:1.45;
  padding:0 max(1rem, env(safe-area-inset-left))
          calc(1.5rem + env(safe-area-inset-bottom)); }
.wrap { max-width:46rem; margin:0 auto; }

h1 { font-size:1.5rem; line-height:1.2; margin:1.2rem 0 .4rem; }
h2 { font-size:1rem; color:var(--dim); margin:2rem 0 .6rem;
     text-transform:uppercase; letter-spacing:.08em; }
p { margin:.5rem 0; }
a { color:var(--accent); }
.lede { color:var(--dim); margin:0 0 1.2rem; }
.note { color:var(--faint); font-size:.85rem; margin:.4rem 0; }
hr { border:0; border-top:1px solid var(--line); margin:1.5rem 0; }

/* ---- nav ---- */
nav { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap;
      padding:.9rem 0 .2rem; }
nav a, nav button { color:var(--dim); background:none; border:1px solid var(--line);
  border-radius:999px; padding:.45rem .9rem; font-size:.85rem; min-height:38px;
  text-decoration:none; display:inline-flex; align-items:center; }
nav .grow { flex:1 1 auto; }

/* ---- forms ---- */
fieldset { border:1px solid var(--line); border-radius:14px; margin:1rem 0;
           padding:1rem; background:var(--card); }
legend { padding:0 .45rem; color:var(--accent); font-size:.9rem; }
.row { margin:0 0 1rem; }
.row:last-child { margin-bottom:0; }
.row > label, label.lbl { display:block; width:auto; margin:0 0 .3rem;
  color:var(--dim); font-size:.85rem; }
input[type=text], input[type=password], input[type=email], input[type=url],
input[type=number], input[type=tel], select {
  /* 16px is the threshold below which iOS zooms the page on focus. */
  display:block; width:100%; font:inherit; font-size:16px; min-height:44px;
  padding:.6rem .75rem; color:var(--fg); background:var(--bg);
  border:1px solid var(--line); border-radius:10px; }
input:focus-visible, select:focus-visible, button:focus-visible,
.opt:focus-within { outline:2px solid var(--accent); outline-offset:1px; }
input::placeholder { color:var(--faint); }
.pair { display:flex; gap:.6rem; }
.pair > * { flex:1 1 0; min-width:0; }

/* ---- buttons ---- */
button, .btn { font:inherit; font-size:1rem; min-height:44px; padding:.7rem 1.2rem;
  border:1px solid transparent; border-radius:10px; background:var(--btn);
  color:var(--btn-fg); cursor:pointer; text-decoration:none;
  display:inline-flex; align-items:center; justify-content:center; gap:.4rem; }
button.secondary, .btn.secondary { background:var(--raise); color:var(--fg);
  border-color:var(--line); }
button.quiet { background:none; border-color:var(--line); color:var(--dim);
  min-height:38px; padding:.4rem .9rem; font-size:.85rem; }
button.danger { background:none; border-color:var(--danger); color:var(--danger);
  min-height:38px; padding:.4rem .9rem; font-size:.85rem; }
button[disabled] { opacity:.55; cursor:default; }
.actions { display:flex; gap:.6rem .8rem; align-items:center; flex-wrap:wrap;
  margin-top:1.2rem; }
/* Only the page's primary action bar follows you down the page; a second
   sticky bar mid-page reads as a floating button with no context. */
.actions.stick { position:sticky; bottom:0; z-index:5;
  padding:.9rem 0 calc(.9rem + env(safe-area-inset-bottom));
  background:var(--bg); box-shadow:0 -12px 16px -12px rgba(0,0,0,.45); }
.actions .grow { flex:1 1 auto; }
/* On a phone the note goes above a full-width button rather than
   squeezing it into whatever is left. */
.actions .note { flex:1 0 100%; order:-1; margin:0; }
.actions button[type=submit], .actions .primary { flex:1 1 auto; }
/* Two buttons that belong side by side but need separate forms (check
   and install, say) still have to share the row evenly. */
.actions form { flex:1 1 auto; display:flex; margin:0; }
.actions form button { flex:1 1 auto; }

/* ---- password / copy field ---- */
.withbtn { display:flex; gap:.5rem; align-items:stretch; }
.withbtn input { flex:1 1 auto; min-width:0; }
.withbtn button { flex:0 0 auto; min-width:5.2rem; background:var(--raise);
  color:var(--dim); border-color:var(--line); font-size:.85rem; padding:.6rem .8rem; }

/* ---- tappable option cards (data source, Wi-Fi networks) ---- */
.opts { display:grid; gap:.55rem; margin:.2rem 0 1rem; }
.opt { position:relative; display:flex; align-items:center; gap:.85rem;
  min-height:56px; padding:.75rem .9rem; background:var(--bg);
  border:1px solid var(--line); border-radius:12px; cursor:pointer; }
.opt input { position:absolute; opacity:0; width:1px; height:1px; margin:0; }
.opt .body { flex:1 1 auto; min-width:0; }
.opt .name { display:block; overflow:hidden; text-overflow:ellipsis;
             white-space:nowrap; }
.opt .sub { display:block; color:var(--faint); font-size:.8rem; }
.opt .tick { flex:0 0 auto; width:20px; color:var(--accent); visibility:hidden; }
.opt.sel, .opt:has(input:checked) { border-color:var(--accent);
  box-shadow:inset 0 0 0 1px var(--accent); background:var(--card); }
.opt.sel .tick, .opt:has(input:checked) .tick { visibility:visible; }

/* signal strength, drawn rather than described in a label nobody sees */
.bars { flex:0 0 auto; display:inline-flex; align-items:flex-end; gap:2px;
        height:15px; }
.bars i { width:3px; border-radius:1px; background:var(--line); }
.bars i:nth-child(1) { height:5px; } .bars i:nth-child(2) { height:8px; }
.bars i:nth-child(3) { height:11px; } .bars i:nth-child(4) { height:15px; }
.bars i.on { background:var(--dim); }
.lock { flex:0 0 auto; color:var(--faint); font-size:.85rem; }

/* ---- check row ---- */
.check { display:flex; align-items:center; gap:.7rem; min-height:44px;
         cursor:pointer; margin:.2rem 0; }
.check input { width:22px; height:22px; min-height:0; flex:0 0 auto; margin:0; }
.check span { color:var(--fg); }

/* ---- banners & status ---- */
.banner { margin:.7rem 0; padding:.7rem .9rem; font-size:.92rem;
  background:var(--card); border:1px solid var(--line); border-left-width:3px;
  border-radius:10px; }
.banner.err { border-left-color:var(--danger); color:var(--danger); }
.banner.ok { border-left-color:var(--ok); color:var(--ok); }
.banner.warn { border-left-color:var(--warn); color:var(--warn); }
.banner.info { border-left-color:var(--accent); }
/* A code somebody points a phone at: centred, never wider than the screen,
   and on white in both themes because that is what scanners read. */
.pairqr { text-align:center; }
.pairqr .note, .pairqr .row { text-align:left; }
svg.qr { width:min(240px, 70vw); height:auto; display:block; margin:.6rem auto;
  border:8px solid #fff; border-radius:6px; background:#fff; }
pre.detail { background:var(--bg); border:1px solid var(--line); border-radius:8px;
  padding:.6rem; font-size:.75rem; color:var(--dim); overflow-x:auto;
  white-space:pre-wrap; word-break:break-word; }
details summary { cursor:pointer; min-height:38px; display:flex;
                  align-items:center; color:var(--dim); font-size:.85rem; }
/* display:flex on a summary drops the disclosure triangle, and a
   heading nobody knows is tappable is worse than no heading. */
details > summary::marker, details > summary::-webkit-details-marker
  { content:""; display:none; }
details > summary::before { content:"\\203A"; display:inline-block;
  width:1em; font-size:1.15em; line-height:1; color:var(--faint);
  transition:transform .15s ease; }
details[open] > summary::before { transform:rotate(90deg); }

/* ---- wizard progress ---- */
.steps { display:flex; gap:.3rem; margin:1rem 0 .2rem; }
.steps i { flex:1 1 0; height:4px; border-radius:2px; background:var(--line); }
.steps i.done { background:var(--accent); }
.stepno { color:var(--faint); font-size:.8rem; letter-spacing:.08em;
          text-transform:uppercase; }

/* ---- settings hub: one tappable row per area ---- */
.menu { display:grid; gap:.55rem; margin:.7rem 0 1.2rem; }
.item { display:flex; align-items:center; gap:.85rem; min-height:62px;
  padding:.7rem .9rem; background:var(--card); border:1px solid var(--line);
  border-radius:12px; color:var(--fg); text-decoration:none; }
.item:active { background:var(--raise); }
.item .body { flex:1 1 auto; min-width:0; }
.item .name { display:block; }
.item .sub { display:block; color:var(--faint); font-size:.82rem;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.item .chev { flex:0 0 auto; color:var(--faint); font-size:1.4rem;
  line-height:1; margin-left:.1rem; }
.item .thumb { flex:0 0 auto; width:78px; display:block; border-radius:6px;
  border:1px solid var(--line); }
/* The live thumbnail, with the icon standing in until (or unless) it
   loads — otherwise that row's text sits out of line with the rest. */
.item .lead { flex:0 0 auto; display:flex; align-items:center; }
.ico { flex:0 0 auto; width:22px; height:22px; color:var(--dim); }
.pill { flex:0 0 auto; font-size:.72rem; line-height:1.5; padding:.1rem .55rem;
  border:1px solid var(--line); border-radius:999px; color:var(--dim);
  white-space:nowrap; }
.pill.ok { color:var(--ok); border-color:var(--ok); }
.pill.warn { color:var(--warn); border-color:var(--warn); }
.pill.err { color:var(--danger); border-color:var(--danger); }

/* ---- facts: a short read-only list, e.g. "version", "address" ---- */
.facts { display:grid; gap:.35rem .9rem; margin:.6rem 0 1rem;
  grid-template-columns:auto 1fr; font-size:.9rem; }
.facts dt { color:var(--dim); }
.facts dd { margin:0; overflow-wrap:anywhere; }

/* ---- misc ---- */
.sr-only { position:absolute; width:1px; height:1px; padding:0; overflow:hidden;
  clip:rect(0 0 0 0); white-space:nowrap; border:0; }
.mono { font-family:ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size:15px; }
.stack { display:grid; gap:.9rem; }
img.screen { display:block; width:100%; border:1px solid var(--line);
             border-radius:12px; }
table { width:100%; border-collapse:collapse; font-size:.85rem; }
td, th { padding:.45rem .5rem; border-bottom:1px solid var(--line);
         text-align:left; }
th { color:var(--dim); font-weight:600; }
td.err { color:var(--danger); }
td.time { white-space:nowrap; color:var(--dim); }
.tablewrap { overflow-x:auto; }

@media (min-width:640px) {
  .row.inline { display:grid; grid-template-columns:12rem 1fr;
                align-items:center; gap:.9rem; }
  .row.inline > label { margin:0; }
  .row.inline > .note { grid-column:2; }
  .actions button[type=submit], .actions .primary { flex:0 0 auto;
    min-width:11rem; }
  .actions .note { flex:1 1 auto; order:0; }
}
"""

# Runs in <head>, before first paint, so a light-theme user never sees a
# dark flash. Kept separate from SCRIPT for that reason.
THEME_INIT = """<script>
(function(){
  try {
    var t = localStorage.theme ||
      (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    document.documentElement.dataset.theme = t;
  } catch (e) { document.documentElement.dataset.theme = 'dark'; }
  window.toggleTheme = function(){
    var n = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.theme = n; } catch (e) {}
    document.documentElement.dataset.theme = n;
  };
})();
</script>"""

SCRIPT = """<script>
(function(){
  var d = document;
  function on(evt, sel, fn){
    d.addEventListener(evt, function(e){
      var t = e.target && e.target.closest ? e.target.closest(sel) : null;
      if (t) fn(e, t);
    });
  }

  on('click', 'button.reveal', function(e, b){
    var input = d.getElementById(b.dataset.reveal);
    if (!input) return;
    var show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    b.textContent = show ? 'Hide' : 'Show';
    b.setAttribute('aria-pressed', show ? 'true' : 'false');
  });

  on('click', 'button.copy', function(e, b){
    var input = d.getElementById(b.dataset.copy);
    if (!input) return;
    function flash(){
      var old = b.dataset.label || b.textContent;
      b.dataset.label = old;
      b.textContent = 'Copied';
      setTimeout(function(){ b.textContent = old; }, 1200);
    }
    function legacy(){
      var ro = input.readOnly;
      input.readOnly = false;
      input.focus(); input.select();
      try { input.setSelectionRange(0, 99999); } catch (err) {}
      try { d.execCommand('copy'); flash(); } catch (err) {}
      input.readOnly = ro;
    }
    // The device is served over plain http, where navigator.clipboard is
    // undefined in every modern browser - the fallback is the real path.
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(input.value).then(flash, legacy);
    } else { legacy(); }
  });

  function syncCards(){
    var inputs = d.querySelectorAll('.opt input');
    for (var i = 0; i < inputs.length; i++){
      var opt = inputs[i].closest('.opt');
      if (opt) opt.classList.toggle('sel', inputs[i].checked);
    }
  }

  // Conditional sections: a container marked data-group="NAME" is shown
  // only while the control marked data-controls="NAME" holds one of the
  // values in its data-when list. The server renders the same decision,
  // so nothing flashes before this runs.
  function syncGroups(){
    var seen = {};
    var ctrls = d.querySelectorAll('[data-controls]');
    for (var i = 0; i < ctrls.length; i++){
      var ctl = ctrls[i], name = ctl.dataset.controls;
      var value = null;
      if (ctl.type === 'radio' || ctl.type === 'checkbox') {
        if (ctl.checked) value = ctl.type === 'checkbox'
          ? (ctl.value || 'on') : ctl.value;
        else if (ctl.type === 'checkbox' && !seen[name]) value = '';
      } else { value = ctl.value; }
      if (value === null) continue;
      seen[name] = true;
      var groups = d.querySelectorAll('[data-group="' + name + '"]');
      for (var j = 0; j < groups.length; j++){
        var when = (groups[j].dataset.when || '').split(' ');
        groups[j].hidden = when.indexOf(value) < 0;
      }
    }
  }

  // Affordances that only work with JS should not exist without it.
  function enable(){
    var b = d.querySelectorAll('button.reveal[hidden], button.copy[hidden],'
                               + ' [data-needs-js][hidden]');
    for (var i = 0; i < b.length; i++) b[i].hidden = false;
  }

  function sync(){ enable(); syncCards(); syncGroups(); }
  d.addEventListener('change', sync);
  d.addEventListener('input', syncGroups);
  sync();
  window.glucoSync = sync;   // for markup added after load
})();
</script>"""

NAV = """<nav><a href="/">Dashboard</a><a href="/settings">Settings</a>
<a href="/log">Sync log</a><span class="grow"></span>
<button type="button" onclick="toggleTheme()">Theme</button></nav>"""

# Stroke icons for the settings hub. Inline because every page has to
# work with no internet at all, and currentColor because both themes
# have to look deliberate.
ICONS = {
    "screen": '<rect x="2.6" y="4" width="18.8" height="13" rx="2"/>'
              '<path d="M9 20.5h6M12 17v3.5"/>',
    "people": '<circle cx="9" cy="8" r="3.2"/>'
              '<path d="M3.4 19.2c0-3.1 2.5-5.2 5.6-5.2s5.6 2.1 5.6 5.2"/>'
              '<path d="M16.2 6.3a3.2 3.2 0 0 1 0 6"/>'
              '<path d="M17.4 14.4c2.1.7 3.2 2.4 3.2 4.6"/>',
    "ranges": '<path d="M3.5 8h9M17 8h3.5M3.5 16h4M12 16h8.5"/>'
              '<circle cx="14.7" cy="8" r="2.2"/><circle cx="9.7" cy="16" r="2.2"/>',
    "wifi": '<path d="M2.9 9.1a13.6 13.6 0 0 1 18.2 0"/>'
            '<path d="M6.1 12.5a9 9 0 0 1 11.8 0"/>'
            '<path d="M9.3 15.9a4.4 4.4 0 0 1 5.4 0"/>'
            '<circle cx="12" cy="19.2" r="1.1" fill="currentColor" stroke="none"/>',
    "clock": '<circle cx="12" cy="12" r="8.6"/><path d="M12 6.8v5.5l3.6 2.1"/>',
    "update": '<path d="M12 3.6v11M7.6 10.3 12 14.7l4.4-4.4"/>'
              '<path d="M4.6 18.6h14.8"/>',
    "lock": '<rect x="4.6" y="10.4" width="14.8" height="9.6" rx="2"/>'
            '<path d="M8.1 10.4V7.9a3.9 3.9 0 0 1 7.8 0v2.5"/>',
    "wizard": '<path d="M12 3.5 13.7 8l4.6 1.7-4.6 1.7L12 16l-1.7-4.6L5.7 9.7'
              ' 10.3 8Z"/><path d="M18.5 15.5 19.3 18l2.5.8-2.5.9-.8 2.4'
              '-.9-2.4-2.4-.9 2.4-.8Z"/>',
    "log": '<path d="M5.5 4.5h13v15h-13z"/><path d="M8.5 9h7M8.5 12.5h7M8.5 16h4"/>',
    "cloud": '<path d="M7.3 18.5a4.3 4.3 0 0 1-.4-8.6 5.6 5.6 0 0 1 10.7-1.2'
             ' 3.9 3.9 0 0 1-.7 7.8Z"/>'
             '<path d="M12 10.6v6.2M9.5 14.3 12 16.8l2.5-2.5"/>',
}


def icon(name: str) -> str:
    return (f'<svg class="ico" viewBox="0 0 24 24" fill="none"'
            ' stroke="currentColor" stroke-width="1.6" stroke-linecap="round"'
            f' stroke-linejoin="round" aria-hidden="true">{ICONS.get(name, "")}'
            "</svg>")


def nav_html(back: str = "", back_label: str = "Settings") -> str:
    """The page's chrome. A sub-page leads with the way back out of it."""
    if not back:
        return NAV
    return (f'<nav><a href="{esc(back)}">&lsaquo; {esc(back_label)}</a>'
            '<a href="/">Dashboard</a><span class="grow"></span>'
            '<button type="button" onclick="toggleTheme()">Theme</button></nav>')


def menu(items) -> str:
    """A list of tappable rows — the settings hub, and the people list."""
    return f'<div class="menu">{"".join(items)}</div>'


def menu_item(href: str, title: str, sub: str = "", *, lead: str = "",
              badge: str = "", badge_kind: str = "") -> str:
    """One row of a menu: a whole-row tap target, with a state line.

    The sub-line is the point of the pattern — it means the hub answers
    most questions ("is it on Wi-Fi?", "when did that last update?")
    without anyone opening the page that owns them.
    """
    badge_html = (f'<span class="pill {esc(badge_kind)}">{esc(badge)}</span>'
                  if badge else "")
    return (
        f'<a class="item" href="{esc(href)}">{lead}'
        f'<span class="body"><span class="name">{esc(title)}</span>'
        + (f'<span class="sub">{esc(sub)}</span>' if sub else "")
        + f"</span>{badge_html}"
        '<span class="chev" aria-hidden="true">&rsaquo;</span></a>'
    )


def qr_svg(data: str, *, size_px: int = 240, alt: str = "") -> str:
    """A QR code as inline SVG, or "" when it cannot be drawn.

    SVG rather than a PNG endpoint: it is one string in the page that
    scales to whatever the phone gives it, with nothing to fetch and
    nothing to cache wrongly. Always dark on white whatever the theme is
    — scanners need the contrast, and a "dark mode" QR is one people hold
    their phone at for a while and then give up on.

    An empty answer is not an error: `qrcode` is a dependency the display
    needs and the web app does not, and every page that shows a code also
    shows the address it stands for.
    """
    try:
        import qrcode
    except ImportError:
        return ""
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                         border=2)
    code.add_data(data)
    code.make(fit=True)
    matrix = code.get_matrix()
    span = len(matrix)
    # One path of rectangles, rather than a rect element per module: a
    # version-6 code is over a thousand of them.
    parts = []
    for y, row in enumerate(matrix):
        run = 0
        for x, dark in enumerate(row + [False]):
            if dark:
                run += 1
                continue
            if run:
                parts.append(f"M{x - run} {y}h{run}v1h-{run}z")
                run = 0
    return (
        f'<svg class="qr" viewBox="0 0 {span} {span}" width="{size_px}"'
        f' height="{size_px}" role="img"'
        f' aria-label="{esc(alt or "QR code")}"'
        ' xmlns="http://www.w3.org/2000/svg" shape-rendering="crispEdges">'
        f'<rect width="{span}" height="{span}" fill="#ffffff"/>'
        f'<path fill="#000000" d="{"".join(parts)}"/></svg>'
    )


def failure(message: str, detail: str = "") -> str:
    """A check that failed, with the technical half behind a disclosure.

    verify.Result carries both halves for a reason — the message says what
    to do, the detail says what actually happened — and a page that shows
    only the first half turns "DNS does not know that host" into a
    guessing game.
    """
    body = banner("err", esc(message))
    if detail:
        body += ("<details><summary>Technical detail</summary>"
                 f'<pre class="detail">{esc(detail)}</pre></details>')
    return body


def facts(pairs) -> str:
    """Read-only label/value lines. Values may contain markup."""
    body = "".join(f"<dt>{esc(label)}</dt><dd>{value}</dd>"
                   for label, value in pairs)
    return f'<dl class="facts">{body}</dl>'


# ------------------------------------------------------------- helpers ----

def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str, *, nav: bool = False, head: str = "",
         script: str = "", refresh: str = "", back: str = "",
         back_label: str = "Settings") -> str:
    """A complete document. Every response goes through here.

    Previously the error and interstitial pages were emitted as bare
    fragments — no doctype, no viewport, no stylesheet — so a failed save
    landed on a white Times New Roman page in the middle of a dark themed
    flow, zoomed out on the phone it was being read on.
    """
    meta_refresh = f'<meta http-equiv="refresh" content="{esc(refresh)}">' if refresh else ""
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1,"
        " viewport-fit=cover\">"
        f"{meta_refresh}<title>{esc(title)}</title>{THEME_INIT}"
        f"<style>{STYLE}</style>"
        '<noscript><style>[data-group][hidden]{display:block !important}'
        '.opt .tick{visibility:visible;color:var(--faint)}</style></noscript>'
        f"{head}</head><body><div class=\"wrap\">"
        f"{nav_html(back, back_label) if nav else ''}"
        f"{body}</div>{SCRIPT}{script}</body></html>"
    )


def banner(kind: str, body_html: str) -> str:
    """kind: info | ok | warn | err."""
    return f'<div class="banner {esc(kind)}">{body_html}</div>'


def row(label: str, control_html: str, *, hint: str = "", inline: bool = True,
        for_id: str = "") -> str:
    cls = "row inline" if inline else "row"
    label_html = (
        f'<label{f" for={chr(34)}{esc(for_id)}{chr(34)}" if for_id else ""}>'
        f"{esc(label)}</label>" if label else ""
    )
    hint_html = f'<div class="note">{hint}</div>' if hint else ""
    return f'<div class="{cls}">{label_html}{control_html}{hint_html}</div>'


def text_input(name: str, value: str = "", *, kind: str = "text",
               placeholder: str = "", input_id: str = "", extra: str = "") -> str:
    ident = input_id or f"f_{name}"
    ph = f' placeholder="{esc(placeholder)}"' if placeholder else ""
    return (f'<input type="{esc(kind)}" id="{esc(ident)}" name="{esc(name)}"'
            f' value="{esc(value)}"{ph} {extra}>')


def password_input(name: str, value: str = "", *, placeholder: str = "",
                   input_id: str = "", extra: str = "") -> str:
    """A masked field you can actually check before submitting.

    Typing a Wi-Fi passphrase blind on a phone, then waiting two minutes
    for the hotspot to come back to learn it was wrong, was the single
    worst moment in setup.
    """
    ident = input_id or f"f_{name}"
    ph = f' placeholder="{esc(placeholder)}"' if placeholder else ""
    return (
        '<div class="withbtn">'
        f'<input type="password" id="{esc(ident)}" name="{esc(name)}"'
        f' value="{esc(value)}"{ph} autocapitalize="none" autocorrect="off"'
        f' autocomplete="off" spellcheck="false" {extra}>'
        f'<button type="button" class="reveal" data-reveal="{esc(ident)}"'
        ' aria-pressed="false" hidden>Show</button></div>'
    )


def copy_input(name: str, value: str, *, input_id: str = "") -> str:
    """Read-only value with a copy button — for things typed into another app."""
    ident = input_id or f"f_{name}"
    return (
        '<div class="withbtn">'
        f'<input type="text" id="{esc(ident)}" name="{esc(name)}"'
        f' value="{esc(value)}" readonly>'
        f'<button type="button" class="copy" data-copy="{esc(ident)}" hidden>Copy</button>'
        "</div>"
    )


def select(name: str, options, selected: str = "", *, input_id: str = "",
           extra: str = "") -> str:
    """A native <select>. Options are (value, label) pairs.

    Native on purpose: a phone renders it as its own scrollable wheel with
    type-ahead, which beats anything hand-rolled for a list this long.
    """
    ident = input_id or f"f_{name}"
    body = "".join(
        f'<option value="{esc(value)}"'
        f'{" selected" if str(value) == str(selected) else ""}>{esc(label)}'
        "</option>"
        for value, label in options
    )
    return (f'<select id="{esc(ident)}" name="{esc(name)}" {extra}>'
            f"{body}</select>")


def checkbox(name: str, label: str, checked: bool = False, *,
             value: str = "1", extra: str = "") -> str:
    return (
        f'<label class="check"><input type="checkbox" name="{esc(name)}"'
        f' value="{esc(value)}"{" checked" if checked else ""} {extra}>'
        f"<span>{esc(label)}</span></label>"
    )


def option_card(name: str, value: str, title: str, sub: str = "", *,
                checked: bool = False, controls: str = "", lead: str = "",
                trail: str = "") -> str:
    ctl = f' data-controls="{esc(controls)}"' if controls else ""
    return (
        f'<label class="opt{" sel" if checked else ""}">'
        f'<input type="radio" name="{esc(name)}" value="{esc(value)}"'
        f'{" checked" if checked else ""}{ctl}>'
        f"{lead}"
        f'<span class="body"><span class="name">{esc(title)}</span>'
        + (f'<span class="sub">{esc(sub)}</span>' if sub else "")
        + f"</span>{trail}"
        '<span class="tick" aria-hidden="true">&#10003;</span></label>'
    )


def choice_cards(name: str, options, selected: str = "", *,
                 controls: str = "") -> str:
    """Tappable radio cards. A <select> on a phone hides its options behind
    a picker; these are all visible and each is a 56px target."""
    cards = "".join(
        option_card(name, value, title, sub, checked=(value == selected),
                    controls=controls)
        for value, title, sub in options
    )
    return f'<div class="opts">{cards}</div>'


def group(name: str, when, body_html: str, *, current: str = "") -> str:
    """A section shown only for certain values of the control named `name`.

    Rendered `hidden` server-side when it does not apply, so it never
    flashes into view before the script at the end of the body runs (and
    stays hidden entirely with JavaScript off).
    """
    values = when if isinstance(when, (list, tuple)) else [when]
    show = current in values
    return (f'<div data-group="{esc(name)}" data-when="{esc(" ".join(values))}"'
            f'{"" if show else " hidden"}>{body_html}</div>')


def signal_bars(percent: int) -> str:
    lit = 1 if percent < 30 else 2 if percent < 55 else 3 if percent < 78 else 4
    bars = "".join(f'<i class="{"on" if i < lit else ""}"></i>' for i in range(4))
    return f'<span class="bars" aria-label="{int(percent)}% signal">{bars}</span>'


def network_picker(networks, *, selected: str = "", other_ssid: str = "",
                   hidden: bool = False, name: str = "wifi_ssid") -> str:
    """A list of networks you can tap.

    This used to be a text box backed by a <datalist>, which iOS Safari
    does not implement at all — on an iPhone, the phone onboarding is
    done from, there was simply no list. Real radios work everywhere and
    with JavaScript off; "Other network" keeps hidden and out-of-range
    networks reachable.
    """
    other_selected = bool(other_ssid) or (selected and
                                          not any(n["ssid"] == selected
                                                  for n in networks))
    cards = []
    for net in networks:
        lock = ('<span class="lock" aria-label="secured">&#128274;</span>'
                if net.get("secured") else
                '<span class="lock">open</span>')
        cards.append(option_card(
            name, net["ssid"], net["ssid"],
            checked=(net["ssid"] == selected and not other_selected),
            controls="wifiother",
            trail=lock + signal_bars(int(net.get("signal") or 0)),
        ))
    cards.append(option_card(
        name, "__other__", "Other network…",
        "hidden, or not in the list", checked=bool(other_selected),
        controls="wifiother",
    ))
    manual = group(
        "wifiother", "__other__",
        row("Network name",
            text_input("wifi_other_ssid", other_ssid, input_id="wifi_other_ssid",
                       placeholder="exact name, including capitals",
                       extra='autocapitalize="none" autocorrect="off"'
                             ' spellcheck="false"'),
            inline=False, for_id="wifi_other_ssid")
        + checkbox("wifi_hidden", "This network does not broadcast its name",
                   hidden),
        current="__other__" if other_selected else "",
    )
    return f'<div class="opts">{"".join(cards)}</div>{manual}'
