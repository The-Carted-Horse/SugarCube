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
.actions { position:sticky; bottom:0; z-index:5; display:flex; gap:.6rem;
  align-items:center; margin-top:1.4rem;
  padding:.9rem 0 calc(.9rem + env(safe-area-inset-bottom));
  background:linear-gradient(to top, var(--bg) 72%, transparent); }
.actions .grow { flex:1 1 auto; }
.actions button[type=submit], .actions .primary { flex:1 1 auto; }

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
pre.detail { background:var(--bg); border:1px solid var(--line); border-radius:8px;
  padding:.6rem; font-size:.75rem; color:var(--dim); overflow-x:auto;
  white-space:pre-wrap; word-break:break-word; }
details summary { cursor:pointer; min-height:38px; display:flex;
                  align-items:center; color:var(--dim); font-size:.85rem; }

/* ---- wizard progress ---- */
.steps { display:flex; gap:.3rem; margin:1rem 0 .2rem; }
.steps i { flex:1 1 0; height:4px; border-radius:2px; background:var(--line); }
.steps i.done { background:var(--accent); }
.stepno { color:var(--faint); font-size:.8rem; letter-spacing:.08em;
          text-transform:uppercase; }

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

  function sync(){ syncCards(); syncGroups(); }
  d.addEventListener('change', sync);
  d.addEventListener('input', syncGroups);
  sync();
  window.sugarSync = sync;   // for markup added after load
})();
</script>"""

NAV = """<nav><a href="/">Dashboard</a><a href="/settings">Settings</a>
<a href="/log">Sync log</a><span class="grow"></span>
<button type="button" onclick="toggleTheme()">Theme</button></nav>"""


# ------------------------------------------------------------- helpers ----

def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str, *, nav: bool = False, head: str = "",
         script: str = "", refresh: str = "") -> str:
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
        f"{NAV if nav else ''}{body}</div>{SCRIPT}{script}</body></html>"
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
        ' aria-pressed="false">Show</button></div>'
    )


def copy_input(name: str, value: str, *, input_id: str = "") -> str:
    """Read-only value with a copy button — for things typed into another app."""
    ident = input_id or f"f_{name}"
    return (
        '<div class="withbtn">'
        f'<input type="text" id="{esc(ident)}" name="{esc(name)}"'
        f' value="{esc(value)}" readonly>'
        f'<button type="button" class="copy" data-copy="{esc(ident)}">Copy</button>'
        "</div>"
    )


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
