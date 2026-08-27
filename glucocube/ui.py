"""Shared HTML, CSS and JS for the web UI.

Everything the device serves is typed on a phone first: onboarding happens
over the setup hotspot, where the browser has no internet, so every page is
self-contained — no CDN, no build step, and the two typefaces come off the
device's own filesystem at /fonts/. The stylesheet is mobile-first and
never widens into columns: the design is one 420px-ish column at every
size, because that is the shape it is read in.

These pages speak the same language as the screen on the front of the
device and the dashboard at /: Space Grotesk for anything you read as a
value, JetBrains Mono for anything you read as a label, and the display's
own palette. A setting and the pixel it controls should not look like they
came from different products.

The components here exist because the same three mistakes kept recurring in
hand-written markup: fields that can't be revealed, options that can't be
tapped, and conditional sections that flash into view before the script at
the bottom of the page hides them. Each helper renders complete, correct
markup server-side and degrades to something usable with JavaScript off.
"""

import html

# ---------------------------------------------------------------- style ----

STYLE = """
@font-face { font-family:'Space Grotesk'; font-weight:700; font-display:swap;
  src:url('/fonts/SpaceGrotesk-Bold.ttf') format('truetype'); }
@font-face { font-family:'Space Grotesk'; font-weight:500; font-display:swap;
  src:url('/fonts/SpaceGrotesk-Medium.ttf') format('truetype'); }
@font-face { font-family:'JetBrains Mono'; font-weight:400; font-display:swap;
  src:url('/fonts/JetBrainsMono-Regular.ttf') format('truetype'); }
@font-face { font-family:'JetBrains Mono'; font-weight:500; font-display:swap;
  src:url('/fonts/JetBrainsMono-Medium.ttf') format('truetype'); }

/* The palette is the dashboard's, which is the physical screen's. `card`
   is one step off the background, `band` two — in dark they rise, in light
   they sink, which is what "raised" means on paper. */
:root, [data-theme=dark] { color-scheme: dark;
  --bg:#0a0c0f; --card:#0f1318; --band:#14191e; --line:#262d34; --hair:#1a2027;
  --fg:#e9edf1; --dim:#7a848e; --faint:#545d66;
  --ok:#5fde96; --warn:#e9b949; --danger:#f45c54; --urgent:#ff453a;
  --accent:#5fde96; --on-accent:#06120b; --shade:rgba(0,0,0,.7); }
[data-theme=light] { color-scheme: light;
  --bg:#f6f7f5; --card:#eef0ec; --band:#e4e7e1; --line:#c9ccc5; --hair:#dfe2dc;
  --fg:#181c20; --dim:#5c646c; --faint:#7d858c;
  --ok:#109448; --warn:#8a5a06; --danger:#cc2c24; --urgent:#c00000;
  --accent:#109448; --on-accent:#ffffff; --shade:rgba(0,0,0,.18); }

:root {
  --sans:'Space Grotesk', system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  --mono:'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  /* One value for the page gutter, so a full-bleed row can reach back
     out to the edge with a negative margin and still line up. */
  --pad: max(1.125rem, env(safe-area-inset-left));
}

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
  font-family:var(--sans); font-weight:500; font-size:16px; line-height:1.5;
  padding:0 var(--pad) calc(1.75rem + env(safe-area-inset-bottom)); }
/* Never two columns: the phone layout is the layout. A wide window gets
   the same column, centred, rather than a second design to maintain. */
.wrap { max-width:30rem; margin:0 auto; }
/* Wherever it appears, .grow is "take the slack" — it is what pushes a
   signal meter, a copy button or a chevron out to the right edge. */
.grow { flex:1 1 auto; min-width:0; }

h1 { font-family:var(--sans); font-weight:700; font-size:30px; line-height:1.08;
     letter-spacing:-.015em; margin:22px 0 8px; }
h1 + .lede, h1 + .meta { margin-top:0; }
p { margin:.6rem 0; }
a { color:var(--accent); }
b, strong { font-weight:700; }
.lede { font-size:15px; line-height:1.5; color:var(--dim); margin:0 0 22px; }
.note { font-size:12.5px; line-height:1.5; color:var(--faint); margin:10px 0 0; }
.note.spaced { margin-bottom:24px; }
.note a { color:var(--accent); }
.meta { font-family:var(--mono); font-size:12px; color:var(--dim); margin:0 0 20px; }
.meta a { color:var(--accent); text-decoration:none; }
h1 + .versions { margin-top:20px; }
/* The line above an h1: what this device is, in the display's own voice. */
.eyebrow { font-family:var(--mono); font-size:10.5px; letter-spacing:.22em;
  text-transform:uppercase; color:var(--faint); margin:20px 0 6px; }
.eyebrow.ok { color:var(--accent); }
.eyebrow.first { margin-top:20px; }
.mono { font-family:var(--mono); }
hr { border:0; border-top:1px solid var(--line); margin:1.6rem 0; }

/* A heading that is a hairline with a word on it — the dashboard's
   "FORECAST 2H" row, reused wherever a group of settings starts. */
.rule { display:flex; align-items:center; gap:10px; margin:28px 0 12px;
  font-family:var(--mono); font-size:10.5px; letter-spacing:.22em;
  text-transform:uppercase; color:var(--faint); }
.rule .fill { flex:1 1 auto; border-top:1px solid var(--line); }
.rule.tight { margin-top:0; }
h2 { font-family:var(--mono); font-weight:400; font-size:10.5px;
  letter-spacing:.22em; text-transform:uppercase; color:var(--faint);
  margin:28px 0 12px; }

/* ---- nav ---- */
nav { display:flex; gap:16px; align-items:center; flex-wrap:wrap;
      padding:6px 0 4px; border-bottom:1px solid var(--line); }
nav a, nav button { font-family:var(--mono); font-size:10.5px;
  letter-spacing:.2em; text-transform:uppercase; color:var(--dim);
  background:none; border:0; padding:0; min-height:34px; cursor:pointer;
  text-decoration:none; display:inline-flex; align-items:center; }
nav .grow { flex:1 1 auto; }

/* ---- forms ---- */
fieldset { border:0; margin:0; padding:0; }
legend { padding:0; }
.row { margin:0 0 22px; }
.row:last-child { margin-bottom:0; }
.row > label, label.lbl { display:block; width:auto; margin:0 0 8px;
  font-family:var(--mono); font-size:10.5px; letter-spacing:.22em;
  text-transform:uppercase; color:var(--faint); }
.row > .note { margin-top:10px; }
input[type=text], input[type=password], input[type=email], input[type=url],
input[type=number], input[type=tel], select, textarea {
  /* 16px is the threshold below which iOS zooms the page on focus, so it
     is the floor for every field however small the label above it is. */
  display:block; width:100%; font-family:var(--mono); font-weight:400;
  font-size:16px; min-height:48px; padding:12px; color:var(--fg);
  background:var(--card); border:1px solid var(--line); border-radius:2px; }
select { font-size:16px; }
/* A number you read as a value, not as data: the ranges page. */
input.num { font-family:var(--sans); font-weight:700; font-size:20px;
  min-height:52px; padding:10px 12px; }
input.num.short { width:96px; display:inline-block; }
input:focus-visible, select:focus-visible, button:focus-visible,
summary:focus-visible, .opt:focus-within, a:focus-visible
  { outline:2px solid var(--accent); outline-offset:2px; }
input::placeholder { color:var(--faint); }
input[aria-invalid=true] { border-color:var(--danger); }
.pair { display:flex; gap:10px; }
.pair > * { flex:1 1 0; min-width:0; }
.inline-field { display:flex; align-items:center; gap:12px; }
.inline-field .note { margin:0; }

/* ---- buttons ---- */
button, .btn { font-family:var(--sans); font-weight:700; font-size:13px;
  letter-spacing:.14em; text-transform:uppercase; min-height:52px;
  padding:0 18px; border:0; border-radius:2px; background:var(--accent);
  color:var(--on-accent); cursor:pointer; text-decoration:none;
  display:inline-flex; align-items:center; justify-content:center; gap:.5em; }
button.secondary, .btn.secondary { background:transparent; color:var(--fg);
  border:1px solid var(--line); }
button.quiet, .btn.quiet { font-family:var(--mono); font-weight:400;
  font-size:10.5px; letter-spacing:.18em; min-height:40px; padding:0 14px;
  background:transparent; border:1px solid var(--line); color:var(--dim); }
button.tiny { font-family:var(--mono); font-weight:400; font-size:9.5px;
  letter-spacing:.18em; min-height:32px; padding:0 10px;
  background:transparent; border:1px solid var(--line); color:var(--dim); }
button.tiny.go { border-color:var(--accent); color:var(--accent); }
button.danger { font-family:var(--mono); font-weight:400; font-size:10.5px;
  letter-spacing:.18em; min-height:42px; padding:0 14px;
  background:transparent; border:1px solid var(--danger); color:var(--danger); }
button[disabled] { background:transparent; border:1px solid var(--line);
  color:var(--faint); cursor:default; }
.actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap;
  margin-top:24px; }
.actions .grow { flex:1 1 auto; }
.actions button[type=submit], .actions .primary { flex:1 1 auto; }
.actions button.quiet, .actions .btn.quiet { flex:0 0 auto; }
.actions form { flex:1 1 auto; display:flex; margin:0; }
.actions form button { flex:1 1 auto; }
/* Only the page's primary action bar follows you down the page; a second
   sticky bar mid-page reads as a floating button with no context. */
.actions.stick, .savebar { position:sticky; bottom:0; z-index:5;
  margin-top:8px; padding:16px 0 calc(.9rem + env(safe-area-inset-bottom));
  background:var(--bg); box-shadow:0 -14px 18px -14px var(--shade); }
.actions.stick { display:flex; }
.actions .note { flex:1 0 100%; order:-1; margin:0 0 10px; }

/* ---- the save bar: what this will cost, above the button that spends it ---- */
.savebar .cost { display:flex; align-items:center; gap:8px;
  font-family:var(--mono); font-size:10.5px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--faint); margin:0 0 10px; }
.savebar .cost i { flex:0 0 auto; width:5px; height:5px; border-radius:50%;
  background:var(--faint); }
.savebar.dirty .cost i { background:var(--warn); }
.savebar button[type=submit] { width:100%; }

/* ---- password / copy field ---- */
.withbtn { display:flex; gap:8px; align-items:stretch; }
.withbtn input { flex:1 1 auto; min-width:0; }
.withbtn button { flex:0 0 auto; font-family:var(--mono); font-weight:400;
  font-size:10px; letter-spacing:.18em; min-width:4rem; min-height:48px;
  padding:0 12px; background:transparent; border:1px solid var(--line);
  color:var(--dim); }

/* ---- tappable option cards (data source, Wi-Fi networks, channels) ---- */
.opts { display:grid; gap:8px; margin:0 0 4px; }
.optw { border:1px solid var(--line); border-radius:2px; background:var(--card); }
.optw.sel, .optw:has(input:checked) { border-color:var(--accent);
  box-shadow:inset 2px 0 0 var(--accent); background:var(--band); }
.opt { position:relative; display:flex; align-items:center; gap:12px;
  min-height:56px; padding:12px 14px; cursor:pointer; }
.opt input { position:absolute; opacity:0; width:1px; height:1px; margin:0; }
.opt .body { flex:1 1 auto; min-width:0; }
.opt .name { display:block; font-weight:700; font-size:15px; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }
.opt .sub { display:block; font-weight:500; font-size:13px; color:var(--dim);
  margin-top:2px; }
.opt .tick { flex:0 0 auto; color:var(--accent); font-size:15px;
  visibility:hidden; }
.optw.sel .tick, .optw:has(input:checked) .tick { visibility:visible; }
/* What the chosen source needs to know, inside the card that needs it —
   rather than three collapsed blocks below a list of three cards. */
.optbody { padding:2px 14px 16px; }
.optbody input, .optbody select { background:var(--bg); min-height:44px;
  padding:10px; }
.optbody .withbtn button { min-height:44px; }
.optbody .row { margin-bottom:12px; }
.optbody .rule { margin:0 0 12px; font-size:10px; }
.optbody .note { margin:0 0 10px; }

/* signal strength, drawn rather than described in a label nobody sees */
.bars { flex:0 0 auto; display:inline-flex; align-items:flex-end; gap:2px;
        height:14px; }
.bars i { width:3px; border-radius:1px; background:var(--line); }
.bars i:nth-child(1) { height:5px; } .bars i:nth-child(2) { height:8px; }
.bars i:nth-child(3) { height:11px; } .bars i:nth-child(4) { height:14px; }
.bars i.on { background:var(--dim); }
.bars.live i.on { background:var(--ok); }
.lock { flex:0 0 auto; color:var(--faint); font-family:var(--mono);
  font-size:9.5px; letter-spacing:.14em; text-transform:uppercase; }

/* ---- check row ---- */
.check { display:flex; align-items:center; gap:12px; min-height:44px;
         cursor:pointer; margin:4px 0; }
.check input { width:22px; height:22px; min-height:0; flex:0 0 auto; margin:0;
  padding:0; }
.check span { color:var(--fg); font-size:14px; }

/* ---- banners: a fact with a colour, sometimes a place to go ---- */
.banner { display:flex; gap:12px; align-items:center; margin:16px 0;
  padding:14px; font-size:14px; line-height:1.45; background:var(--band);
  border-left:2px solid var(--line); border-radius:2px; color:var(--fg); }
.banner > .grow { flex:1 1 auto; min-width:0; }
.banner.err { border-left-color:var(--danger); }
.banner.ok { border-left-color:var(--ok); }
.banner.warn { border-left-color:var(--warn); }
.banner.info { border-left-color:var(--accent); }
a.banner { text-decoration:none; }
a.banner .chev { flex:0 0 auto; font-size:20px; line-height:1;
  color:var(--faint); }
a.banner.err .chev { color:var(--danger); }
a.banner.warn .chev { color:var(--warn); }
a.banner.ok .chev, a.banner.info .chev { color:var(--accent); }
/* A one-line state readout: dot, sentence, somewhere to look. */
.status { font-family:var(--mono); font-size:11.5px; color:var(--dim); }
.status b { color:var(--fg); font-weight:400; }
.status .link { font-size:10px; letter-spacing:.18em; text-transform:uppercase;
  text-decoration:none; white-space:nowrap; }
pre.detail { background:var(--card); border:1px solid var(--line);
  border-radius:2px; padding:12px; font-family:var(--mono); font-size:11px;
  color:var(--dim); overflow-x:auto; white-space:pre-wrap;
  word-break:break-word; }

/* ---- status dots, shared by rows, banners and the log ---- */
.dot { flex:0 0 auto; display:inline-block; width:7px; height:7px;
  border-radius:50%; background:var(--faint); }
.dot.ok { background:var(--ok); } .dot.warn { background:var(--warn); }
.dot.err { background:var(--danger); } .dot.urgent { background:var(--urgent); }
/* Reading colours: the same five bands the screen paints. */
.v-ok { color:var(--ok); } .v-high { color:var(--warn); }
.v-low { color:var(--danger); } .v-urgent { color:var(--urgent); }
.v-stale { color:var(--dim); }

/* ---- disclosure ---- */
details > summary { cursor:pointer; min-height:48px; display:flex;
  align-items:center; gap:0; font-family:var(--mono); font-size:10.5px;
  letter-spacing:.2em; text-transform:uppercase; color:var(--dim); }
details.top > summary { border-top:1px solid var(--hair); margin-top:20px; }
details.slim > summary { min-height:38px; letter-spacing:.18em; }
/* display:flex on a summary drops the disclosure triangle, and a
   heading nobody knows is tappable is worse than no heading. */
details > summary::marker, details > summary::-webkit-details-marker
  { content:""; display:none; }
details > summary::before { content:"\\203A"; display:inline-block;
  flex:0 0 auto; width:1.1em; font-size:1.15em; line-height:1;
  color:var(--faint); transition:transform .15s ease; }
details[open] > summary::before { transform:rotate(90deg); }
details > summary .label { flex:1 1 auto; }
/* The answer the disclosure is hiding, on the line that hides it. */
details > summary .state { flex:0 0 auto; letter-spacing:0;
  text-transform:none; font-size:11px; color:var(--faint); }
details > summary .state.set { color:var(--warn); }
details > .inner { padding:4px 0 8px; }

/* ---- wizard progress, and the restart's three beats ---- */
.steps { display:flex; gap:4px; margin:16px 0 14px; }
.steps i { flex:1 1 0; height:3px; background:var(--line); }
.steps i.done { background:var(--accent); }
.stepno { font-family:var(--mono); font-size:10.5px; color:var(--faint);
  letter-spacing:.16em; text-transform:uppercase; margin:0 0 4px; }
h1 + .stepno { margin:0 0 20px; }
.checklist { display:grid; gap:10px; font-family:var(--mono); font-size:11.5px; }
.checklist > span { display:flex; gap:10px; color:var(--dim); }
.checklist .mark { flex:0 0 auto; width:1em; text-align:center;
  color:var(--ok); }
.checklist > span.todo { color:var(--faint); }
.checklist > span.todo .mark { color:var(--faint); }

/* ---- menus: one tappable row per thing, read value first ---- */
/* The label is the small line and the value is the big one, because the
   hub is a status report you occasionally tap, not a table of contents. */
.menu { display:block; margin:0 0 8px; }
.item { display:flex; align-items:center; gap:14px; min-height:60px;
  padding:14px 0; border-bottom:1px solid var(--hair); color:var(--fg);
  text-decoration:none; }
.item:active { background:var(--card); }
.item .body { flex:1 1 auto; min-width:0; }
.item .lbl { display:block; font-family:var(--mono); font-size:10px;
  letter-spacing:.2em; text-transform:uppercase; color:var(--faint);
  margin-bottom:3px; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; }
.item .val { display:block; font-weight:500; font-size:17px; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }
.item .val small { font-size:14px; font-weight:500; color:var(--faint); }
.item .chev { flex:0 0 auto; color:var(--faint); font-size:20px;
  line-height:1; }
.item .thumb { flex:0 0 auto; width:64px; display:block; border-radius:2px;
  border:1px solid var(--line); }
/* Rows that are errands rather than settings: no value to report. */
.item.quiet { min-height:52px; color:var(--dim); font-family:var(--mono);
  font-size:11px; letter-spacing:.18em; text-transform:uppercase; }
.item.quiet .body { flex:1 1 auto; }
.item.quiet .chev { letter-spacing:0; }
.item.quiet .plus { flex:0 0 auto; width:7px; text-align:center;
  color:var(--faint); }
.item.dashed { border:1px dashed var(--line); border-radius:2px;
  padding:0 16px; min-height:56px; }

/* A reading, wherever it appears: value, unit, trend, age. */
.reading { display:flex; align-items:baseline; gap:8px; }
.reading .n { font-weight:700; font-size:22px; line-height:1; }
.reading .u { font-family:var(--mono); font-size:11px; color:var(--faint); }
.reading .arrow { font-weight:500; font-size:15px; }
.reading .age { font-family:var(--mono); font-size:11px; color:var(--dim); }

/* The people list: the same reading, given the room to be read across a room. */
.pcard { display:block; text-decoration:none; color:var(--fg); padding:16px;
  background:var(--card); border:1px solid var(--line); border-radius:2px;
  margin-bottom:10px; }
.pcard .head { display:flex; align-items:center; gap:10px; margin-bottom:12px; }
.pcard .who { flex:1 1 auto; min-width:0; font-weight:700; font-size:19px;
  letter-spacing:.02em; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; }
.pcard .chev { flex:0 0 auto; color:var(--faint); font-size:20px; line-height:1; }
.pcard .big { display:flex; align-items:flex-end; gap:10px; }
.pcard .n { font-weight:700; font-size:38px; line-height:.9; }
.pcard .side { display:flex; flex-direction:column; gap:2px; padding-bottom:3px; }
.pcard .side .trend { font-weight:500; font-size:16px; line-height:1; }
.pcard .side .u { font-family:var(--mono); font-size:10px; letter-spacing:.18em;
  text-transform:uppercase; color:var(--faint); }
.pcard .age { font-family:var(--mono); font-size:11px; color:var(--dim);
  padding-bottom:3px; margin-left:auto; }

.pill { flex:0 0 auto; font-family:var(--mono); font-size:9.5px;
  letter-spacing:.18em; text-transform:uppercase; padding:3px 7px;
  border:1px solid var(--line); border-radius:2px; color:var(--dim);
  white-space:nowrap; }
.pill.ok { color:var(--ok); border-color:var(--ok); }
.pill.warn { color:var(--warn); border-color:var(--warn); }
.pill.err { color:var(--danger); border-color:var(--danger); }

/* The last thing on the page, and deliberately the quietest. */
.farewell { margin-top:26px; padding-top:18px; border-top:1px solid var(--hair); }
.farewell form { margin:0; }

/* ---- panels: a fact worth a frame of its own ---- */
.panel { padding:16px; background:var(--band); border-radius:2px;
  margin:0 0 12px; }
.panel.edge { border-left:2px solid var(--accent); }
.panel.quiet { background:var(--card); border:1px solid var(--line); }
.panel .cap { display:flex; align-items:center; gap:10px;
  font-family:var(--mono); font-size:10px; letter-spacing:.22em;
  text-transform:uppercase; color:var(--faint); margin-bottom:8px; }
.panel .cap .grow { flex:1 1 auto; }
.panel .big { display:flex; align-items:center; gap:10px; font-weight:700;
  font-size:21px; }
.panel .clock { font-weight:700; font-size:44px; line-height:.9;
  letter-spacing:-.02em; }
.panel .key { display:block; font-family:var(--mono); font-size:19px;
  color:var(--fg); word-break:break-all; }
.panel .weekday { font-family:var(--mono); font-size:11px; letter-spacing:.18em;
  text-transform:uppercase; color:var(--dim); }
.panel .clockrow { display:flex; align-items:baseline; gap:12px; }
.panel .link { display:block; font-family:var(--mono); font-size:12px;
  color:var(--dim); word-break:break-all; }
.panel .sub { display:block; font-family:var(--mono); font-size:11.5px;
  color:var(--dim); margin-top:8px; }
.panel .note { margin-top:10px; }
.panel .warnnote { display:flex; gap:8px; align-items:flex-start;
  font-size:12.5px; line-height:1.5; color:var(--warn); margin-top:10px; }

/* ---- the live screenshot, which is the device saying hello ---- */
img.screen { display:block; width:100%; border:1px solid var(--line);
             border-radius:2px; }
a.hero { display:block; text-decoration:none; color:inherit; margin:0 0 4px; }
a.hero .cap { display:flex; align-items:center; gap:10px; margin-top:8px;
  min-height:34px;
  font-family:var(--mono); font-size:10.5px; letter-spacing:.2em;
  text-transform:uppercase; color:var(--faint); }
a.hero .cap .fill { flex:1 1 auto; border-top:1px solid var(--line); }
a.hero .cap .go { color:var(--dim); }

/* ---- version diff: what is running, and what could be ---- */
.versions { display:flex; align-items:flex-end; gap:14px; padding-bottom:18px;
  border-bottom:1px solid var(--line); }
.versions .v { display:block; }
.versions .cap { display:block; font-family:var(--mono); font-size:10px;
  letter-spacing:.22em; text-transform:uppercase; color:var(--faint);
  margin-bottom:6px; }
.versions .n { display:block; font-weight:700; font-size:30px; line-height:.95;
  color:var(--dim); }
.versions .to { font-weight:500; font-size:20px; color:var(--faint);
  padding-bottom:3px; }
.versions .new .cap, .versions .new .n { color:var(--accent); }

/* ---- how the screen will colour: the ranges, as the screen reads them ---- */
.rangebar { display:flex; height:12px; border-radius:2px; overflow:hidden; }
.rangebar span { display:block; }
.rangebar .b-urgentlow, .rangebar .b-urgenthigh { background:var(--urgent); }
.rangebar .b-low { background:var(--danger); }
.rangebar .b-inrange { background:var(--ok); }
.rangebar .b-high { background:var(--warn); }
.rangeticks { display:flex; margin-top:6px; font-family:var(--mono);
  font-size:10px; color:var(--faint); }
.rangeticks span { text-align:right; overflow:hidden; }
.rangekeys { display:flex; margin-top:8px; font-family:var(--mono);
  font-size:9.5px; letter-spacing:.14em; text-transform:uppercase; }
.rangekeys .k-low { color:var(--danger); }
.rangekeys .k-inrange { color:var(--ok); }
.rangekeys .k-high { color:var(--warn); }
.rangekeys .k-urgent { color:var(--urgent); text-align:right; }

/* ---- the log: a list, not a table — one line per thing that happened ---- */
.logrow { display:flex; align-items:center; gap:14px; padding:14px 0;
  border-bottom:1px solid var(--hair); }
.logrow:first-child { border-top:1px solid var(--line); }
.logrow .t { flex:0 0 62px; font-family:var(--mono); font-size:11px;
  color:var(--faint); }
.logrow .body { flex:1 1 auto; min-width:0; }
.logrow .who { display:block; font-family:var(--mono); font-size:9.5px;
  letter-spacing:.18em; text-transform:uppercase; color:var(--faint);
  margin-bottom:2px; }
.logrow .msg { display:block; font-weight:500; font-size:14px;
  overflow-wrap:anywhere; }
/* A failure reaches out to the edge of the page, so scrolling past it
   is a decision rather than an accident. */
.logrow.bad { background:var(--band); margin:0 calc(-1 * var(--pad));
  padding:14px var(--pad); box-shadow:inset 2px 0 0 var(--danger); }
.logrow.bad .msg { color:var(--danger); }

/* ---- segmented control: two states, the live one already pressed ---- */
.seg { display:flex; gap:8px; }
.seg form { flex:1 1 0; display:flex; margin:0; }
.seg button, .seg .on { flex:1 1 auto; font-size:13px; letter-spacing:.12em; }
.seg .on { display:inline-flex; align-items:center; justify-content:center;
  min-height:52px; padding:0 18px; border-radius:2px; font-family:var(--sans);
  font-weight:700; text-transform:uppercase; background:var(--band);
  border:1px solid var(--accent); box-shadow:inset 2px 0 0 var(--accent);
  color:var(--fg); cursor:default; }
.seg button { background:transparent; border:1px solid var(--line);
  color:var(--dim); }

/* A caption under something live: the screenshot, saying how fresh it is. */
.livecap { display:flex; align-items:center; gap:8px; margin:8px 0 26px;
  font-family:var(--mono); font-size:10px; letter-spacing:.2em;
  text-transform:uppercase; color:var(--faint); }
.banner .link { flex:0 0 auto; color:var(--dim); text-decoration:none; }
.banner.ok .link, .banner.info .link { color:var(--accent); }
.banner.warn .link { color:var(--warn); }
.banner.err .link { color:var(--danger); }
.inline-field input { flex:0 0 auto; width:96px; }
.optbody .withbtn { margin-bottom:8px; }
.optbody .opts { margin:0; }

/* ---- a labelled field inside a pair: "Low" over the number ---- */
.field { flex:1 1 0; min-width:0; display:block; }
.field .flbl { display:block; font-family:var(--mono); font-size:10px;
  letter-spacing:.18em; text-transform:uppercase; color:var(--dim);
  margin-bottom:6px; }

/* ---- "test this login", and what it answers ---- */
.optbody button.test { width:100%; min-height:46px; font-size:12px;
  background:transparent; border:1px solid var(--accent); color:var(--accent);
  margin:0 0 10px; }
.testresult { display:flex; gap:8px; align-items:flex-start;
  font-family:var(--mono); font-size:11px; line-height:1.5; color:var(--dim);
  margin:0 0 14px; }
.testresult .mark { flex:0 0 auto; }
.testresult.ok { color:var(--ok); } .testresult.err { color:var(--danger); }

/* A value you copy but never edit: shown as text, selected from a field
   the clipboard fallback can actually focus. 16px so focusing it cannot
   make iOS zoom the page. */
input.copysrc { position:absolute; width:1px; height:1px; min-height:0;
  padding:0; margin:0; border:0; opacity:0; overflow:hidden; font-size:16px; }

/* ---- misc ---- */
/* Breathing room between two fields that share no label between them. */
.gap { height:8px; }
/* A footnote that has earned a line above it. */
.note.nudge { margin-top:20px; padding-top:16px; border-top:1px solid var(--hair); }
.sr-only { position:absolute; width:1px; height:1px; padding:0; overflow:hidden;
  clip:rect(0 0 0 0); white-space:nowrap; border:0; }
.stack { display:grid; gap:14px; }
table { width:100%; border-collapse:collapse; font-size:14px; }
td, th { padding:12px 0; border-bottom:1px solid var(--hair); text-align:left;
         vertical-align:top; }
th { font-family:var(--mono); font-size:10px; letter-spacing:.2em;
     text-transform:uppercase; color:var(--faint); font-weight:400; }
td:first-child { font-family:var(--mono); font-size:10px; letter-spacing:.18em;
  text-transform:uppercase; color:var(--faint); width:36%; padding-right:12px; }
td.err { color:var(--danger); }
td.time { white-space:nowrap; color:var(--faint); }
.tablewrap { overflow-x:auto; margin:0 0 8px; }
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
  function paint(){
    var t = document.documentElement.dataset.theme;
    var b = document.getElementById('themebtn');
    // The button names the state you are in, the way the screen's own
    // sun and moon do — not the state you would get by pressing it.
    if (b) b.innerHTML = t === 'dark' ? 'Night &#9790;' : 'Day &#9788;';
  }
  window.toggleTheme = function(){
    var n = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.theme = n; } catch (e) {}
    document.documentElement.dataset.theme = n;
    paint();
  };
  document.addEventListener('DOMContentLoaded', paint);
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
      var card = inputs[i].closest('.optw') || inputs[i].closest('.opt');
      if (card) card.classList.toggle('sel', inputs[i].checked);
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

  // A save that restarts the display should say so, and should not be
  // offered at all until there is something to save. Rendered enabled and
  // captioned with the plain cost, so with JavaScript off the page still
  // works — this only ever makes the button *less* eager.
  function initSaveBars(){
    var forms = d.querySelectorAll('form[data-dirty]');
    for (var f = 0; f < forms.length; f++) (function(form){
      var bar = form.querySelector('.savebar');
      if (!bar || bar.dataset.wired) return;
      bar.dataset.wired = '1';
      var button = bar.querySelector('button[type=submit]');
      var caption = bar.querySelector('.count');
      var fields = form.querySelectorAll('input, select, textarea');
      for (var i = 0; i < fields.length; i++){
        var el = fields[i];
        el.dataset.was = (el.type === 'checkbox' || el.type === 'radio')
          ? (el.checked ? '1' : '') : el.value;
      }
      function count(){
        var n = 0, groups = {};
        for (var i = 0; i < fields.length; i++){
          var el = fields[i];
          if (el.disabled || el.type === 'hidden' || el.readOnly) continue;
          var now = (el.type === 'checkbox' || el.type === 'radio')
            ? (el.checked ? '1' : '') : el.value;
          if (now === el.dataset.was) continue;
          // Picking a different radio changes two of them, once.
          if (el.type === 'radio') {
            if (groups[el.name]) continue;
            groups[el.name] = 1;
          }
          n++;
        }
        return n;
      }
      function sync(){
        var n = count();
        bar.classList.toggle('dirty', n > 0);
        if (button) button.disabled = n === 0;
        if (caption) caption.textContent = n === 0
          ? (bar.dataset.clean || 'Nothing changed yet')
          : n + (n === 1 ? ' change \\u00b7 ' : ' changes \\u00b7 ')
              + (bar.dataset.cost || '');
      }
      form.addEventListener('input', sync);
      form.addEventListener('change', sync);
      sync();
    })(forms[f]);
  }

  // The ranges page, coloured the way the screen colours. The axis is
  // fixed at 40-300 mg/dL so the bands move as you type rather than the
  // scale sliding under them.
  var AXIS_LO = 40, AXIS_HI = 300;
  function initRanges(){
    var box = d.getElementById('rangepreview');
    if (!box) return;
    var bands = box.querySelectorAll('.rangebar span');
    var ticks = box.querySelectorAll('.rangeticks span');
    var keys = box.querySelectorAll('.rangekeys span');
    function at(id, fallback){
      var el = d.getElementById(id);
      var v = el ? parseFloat(el.value) : NaN;
      return isFinite(v) ? v : fallback;
    }
    function pct(v){
      return Math.max(0, Math.min(100,
        (v - AXIS_LO) / (AXIS_HI - AXIS_LO) * 100));
    }
    function draw(){
      var v = [at('urgent_low', 55), at('low', 70),
               at('high', 180), at('urgent_high', 250)];
      var stop = [pct(v[0]), pct(v[1]), pct(v[2]), pct(v[3])];
      // A half-typed number must not turn the bar inside out.
      for (var i = 1; i < 4; i++) if (stop[i] < stop[i - 1]) stop[i] = stop[i - 1];
      var w = [stop[0], stop[1] - stop[0], stop[2] - stop[1], stop[3] - stop[2]];
      for (var i = 0; i < 4; i++){
        if (bands[i]) bands[i].style.flex = '0 0 ' + w[i] + '%';
        if (ticks[i]) {
          ticks[i].style.flex = '0 0 ' + w[i] + '%';
          ticks[i].textContent = String(Math.round(v[i]));
        }
      }
      if (keys.length >= 3){
        keys[0].style.flex = '0 0 ' + (w[0] + w[1]) + '%';
        keys[1].style.flex = '0 0 ' + w[2] + '%';
        keys[2].style.flex = '0 0 ' + w[3] + '%';
      }
    }
    d.addEventListener('input', draw);
    d.addEventListener('change', draw);
    draw();
  }

  // Affordances that only work with JS should not exist without it.
  function enable(){
    var b = d.querySelectorAll('button.reveal[hidden], button.copy[hidden],'
                               + ' [data-needs-js][hidden]');
    for (var i = 0; i < b.length; i++) b[i].hidden = false;
  }

  function sync(){ enable(); syncCards(); syncGroups(); initSaveBars(); }
  d.addEventListener('change', sync);
  d.addEventListener('input', syncGroups);
  sync();
  initRanges();
  window.glucoSync = sync;   // for markup added after load
})();
</script>"""

NAV = """<nav><a href="/">Dashboard</a><a href="/log">Sync log</a>
<span class="grow"></span>
<button type="button" id="themebtn" onclick="toggleTheme()">Theme</button></nav>"""


def nav_html(back: str = "", back_label: str = "Settings") -> str:
    """The page's chrome. A sub-page leads with the way back out of it."""
    if not back:
        return NAV
    return (f'<nav><a href="{esc(back)}">&lsaquo; {esc(back_label)}</a>'
            '<a href="/">Dashboard</a><span class="grow"></span>'
            '<button type="button" id="themebtn"'
            ' onclick="toggleTheme()">Theme</button></nav>')


# ---------------------------------------------------------- components ----

def rule(label: str, trail: str = "") -> str:
    """A group heading drawn as a hairline with a word sitting on it."""
    return (f'<div class="rule"><span>{esc(label)}</span>'
            f'<span class="fill"></span>{trail}</div>')


def eyebrow(text: str) -> str:
    return f'<p class="eyebrow">{esc(text)}</p>'


def dot(kind: str = "") -> str:
    return f'<span class="dot {esc(kind)}" aria-hidden="true"></span>'


def menu(items) -> str:
    """A list of tappable rows — the settings hub, and the people list."""
    return f'<div class="menu">{"".join(items)}</div>'


def menu_item(href: str, label: str, value: str = "", *, value_html: str = "",
              trail: str = "", lead: str = "") -> str:
    """One row of a menu: a whole-row tap target, read value first.

    The label is the small line and the value is the big one. The hub is
    a status report — "Wi-Fi / Sabanis", "Clock / 07:57 · Europe/London" —
    that answers most questions without anyone opening the page that owns
    them, and only then a way in.
    """
    body = value_html or f'<span class="val">{esc(value)}</span>'
    return (
        f'<a class="item" href="{esc(href)}">{lead}'
        f'<span class="body"><span class="lbl">{esc(label)}</span>{body}</span>'
        f'{trail}<span class="chev" aria-hidden="true">&rsaquo;</span></a>'
    )


def menu_errand(href: str, label: str, *, plus: bool = False) -> str:
    """A row that is an errand rather than a setting: no value to report."""
    lead = '<span class="plus" aria-hidden="true">+</span>' if plus else ""
    return (f'<a class="item quiet" href="{esc(href)}">{lead}'
            f'<span class="body">{esc(label)}</span>'
            '<span class="chev" aria-hidden="true">&rsaquo;</span></a>')


def reading(value: str, *, unit: str = "mg/dL", arrow: str = "",
            age: str = "", tone: str = "") -> str:
    """A glucose reading in the colour the screen would paint it."""
    tone_cls = f" {esc(tone)}" if tone else ""
    arrow_html = (f'<span class="arrow{tone_cls}">{arrow}</span>'
                  if arrow else "")
    age_html = f'<span class="age">{esc(age)}</span>' if age else ""
    return (f'<span class="reading"><span class="n{tone_cls}">{esc(value)}</span>'
            f'<span class="u">{esc(unit)}</span>{arrow_html}{age_html}</span>')


def field(label: str, control_html: str) -> str:
    """One labelled control inside a `.pair` — "Low" sitting over its number."""
    return (f'<label class="field"><span class="flbl">{esc(label)}</span>'
            f"{control_html}</label>")


def copy_value(value: str, *, input_id: str, css: str = "key") -> str:
    """A value you read on the page and copy with the button in its caption.

    Shown as text so it can wrap; backed by a real field because the
    clipboard fallback on plain http has to focus and select something.
    """
    return (f'<input class="copysrc" type="text" id="{esc(input_id)}"'
            f' value="{esc(value)}" readonly tabindex="-1">'
            f'<span class="{esc(css)}">{esc(value)}</span>')


def copy_button(input_id: str, *, label: str = "Copy") -> str:
    return (f'<button type="button" class="copy tiny" data-copy="{esc(input_id)}"'
            f" hidden>{esc(label)}</button>")


def person_card(href: str, name: str, *, source: str = "", value: str = "",
                trend: str = "", unit: str = "mg/dL", age: str = "",
                tone: str = "", dot_kind: str = "", note: str = "") -> str:
    """One person on the people list, read the way the screen reads them."""
    tone_cls = f" {esc(tone)}" if tone else ""
    pill = f'<span class="pill">{esc(source)}</span>' if source else ""
    if value:
        big = (f'<span class="big"><span class="n{tone_cls}">{esc(value)}</span>'
               '<span class="side">'
               f'<span class="trend{tone_cls}">{trend}</span>'
               f'<span class="u">{esc(unit)}</span></span>'
               + (f'<span class="age">{esc(age)}</span>' if age else "")
               + "</span>")
    else:
        big = f'<span class="big"><span class="n v-stale">{esc(note)}</span></span>'
    return (f'<a class="pcard" href="{esc(href)}">'
            f'<span class="head">{dot(dot_kind)}'
            f'<span class="who">{esc(name)}</span>{pill}'
            '<span class="chev" aria-hidden="true">&rsaquo;</span></span>'
            f"{big}</a>")


# The ranges preview is drawn on a fixed axis so the bands move as the
# numbers change, instead of the scale sliding underneath them.
RANGE_AXIS = (40.0, 300.0)


def range_preview(low, high, urgent_low, urgent_high) -> str:
    """The five bands the screen paints, at the numbers on this page."""
    lo, hi = RANGE_AXIS
    def at(value):
        try:
            return max(0.0, min(100.0, (float(value) - lo) / (hi - lo) * 100))
        except (TypeError, ValueError):
            return 0.0
    stops, last = [], 0.0
    for value in (urgent_low, low, high, urgent_high):
        last = max(last, at(value))
        stops.append(last)
    widths = [stops[0], stops[1] - stops[0], stops[2] - stops[1],
              stops[3] - stops[2]]
    names = ("urgentlow", "low", "inrange", "high")
    values = (urgent_low, low, high, urgent_high)
    bands = "".join(
        f'<span class="b-{name}" style="flex:0 0 {width:.4f}%"></span>'
        for name, width in zip(names, widths))
    ticks = "".join(
        f'<span style="flex:0 0 {width:.4f}%">{esc(value)}</span>'
        for value, width in zip(values, widths))
    keys = (
        f'<span class="k-low" style="flex:0 0 {widths[0] + widths[1]:.4f}%">'
        "Low</span>"
        f'<span class="k-inrange" style="flex:0 0 {widths[2]:.4f}%">In range</span>'
        f'<span class="k-high" style="flex:0 0 {widths[3]:.4f}%">High</span>'
        '<span class="k-urgent" style="flex:1 1 auto">Urgent</span>'
    )
    return (
        '<div id="rangepreview">'
        + rule("How the screen will colour")
        + f'<div class="rangebar">{bands}'
        '<span class="b-urgenthigh" style="flex:1 1 auto"></span></div>'
        f'<div class="rangeticks">{ticks}<span style="flex:1 1 auto"></span></div>'
        f'<div class="rangekeys">{keys}</div></div>'
    )


def segmented(options) -> str:
    """Two states side by side, the live one already pressed.

    Each option is (is_current, label_html, form_html): the current one is
    inert, the other is a one-button form that switches to it.
    """
    parts = []
    for current, label, form in options:
        parts.append(f'<span class="on" aria-current="true">{label}</span>'
                     if current else form)
    return f'<div class="seg">{"".join(parts)}</div>'


def panel(caption: str, body_html: str, *, edge: bool = False,
          quiet: bool = False, cap_trail: str = "") -> str:
    """A fact worth a frame of its own — what you are connected to, the
    password, the time the device believes it is."""
    cls = "panel" + (" edge" if edge else "") + (" quiet" if quiet else "")
    cap = (f'<span class="cap"><span class="grow">{esc(caption)}</span>'
           f"{cap_trail}</span>" if caption or cap_trail else "")
    return f'<div class="{cls}">{cap}{body_html}</div>'


def save_bar(label: str = "Save &amp; restart display",
             cost: str = "the display restarts, ~5s",
             clean: str = "Nothing changed yet") -> str:
    """The page's primary action, with its price on the line above it.

    Rendered enabled and captioned with the plain cost so the page works
    with JavaScript off; the script then counts the edits and holds the
    button until there is at least one.
    """
    return (
        f'<div class="savebar" data-cost="{esc(cost)}" data-clean="{esc(clean)}">'
        '<p class="cost"><i aria-hidden="true"></i>'
        f'<span class="count">{esc(cost[:1].upper() + cost[1:])}</span></p>'
        f'<button type="submit">{label}</button></div>'
    )


def checklist(steps) -> str:
    """(done, text) lines — what has happened, and what is being waited on."""
    body = "".join(
        f'<span class="{"" if done else "todo"}">'
        f'<span class="mark" aria-hidden="true">'
        f'{"&#10003;" if done else "&#183;"}</span>{esc(text)}</span>'
        for done, text in steps
    )
    return f'<div class="checklist">{body}</div>'


def steps_bar(total: int, done: int) -> str:
    return ('<div class="steps">'
            + "".join(f'<i class="{"done" if i < done else ""}"></i>'
                      for i in range(total))
            + "</div>")


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


def banner(kind: str, body_html: str, *, href: str = "", dot_kind: str = "",
           lead: str = "", trail: str = "") -> str:
    """kind: info | ok | warn | err.

    With `href` it becomes the whole notice you tap: the hub's problems
    are one line each and each line is the way to fix it.
    """
    dot_html = dot(dot_kind) if dot_kind else ""
    if href:
        return (f'<a class="banner {esc(kind)}" href="{esc(href)}">'
                f'{dot_html}{lead}<span class="grow">{body_html}</span>'
                '<span class="chev" aria-hidden="true">&rsaquo;</span></a>')
    return (f'<div class="banner {esc(kind)}">{dot_html}{lead}'
            f'<span class="grow">{body_html}</span>{trail}</div>')


def row(label: str, control_html: str, *, hint: str = "", inline: bool = True,
        for_id: str = "") -> str:
    """Label above field, always. `inline` is kept for callers that pass
    it; the layout is one column at every width now."""
    del inline
    label_html = (
        f'<label{f" for={chr(34)}{esc(for_id)}{chr(34)}" if for_id else ""}>'
        f"{esc(label)}</label>" if label else ""
    )
    hint_html = f'<div class="note">{hint}</div>' if hint else ""
    return f'<div class="row">{label_html}{control_html}{hint_html}</div>'


def text_input(name: str, value: str = "", *, kind: str = "text",
               placeholder: str = "", input_id: str = "", extra: str = "",
               css: str = "") -> str:
    ident = input_id or f"f_{name}"
    ph = f' placeholder="{esc(placeholder)}"' if placeholder else ""
    cls = f' class="{esc(css)}"' if css else ""
    return (f'<input type="{esc(kind)}" id="{esc(ident)}" name="{esc(name)}"'
            f' value="{esc(value)}"{ph}{cls} {extra}>')


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


def copy_input(name: str, value: str, *, input_id: str = "",
               label: str = "") -> str:
    """Read-only value with a copy button — for things typed into another app."""
    ident = input_id or f"f_{name}"
    described = f' aria-label="{esc(label)}"' if label else ""
    return (
        '<div class="withbtn">'
        f'<input type="text" id="{esc(ident)}" name="{esc(name)}"'
        f' value="{esc(value)}" readonly{described}>'
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
                trail: str = "", body_html: str = "",
                wrap_extra: str = "") -> str:
    """A tappable radio card, optionally carrying its own settings.

    `body_html` is what this choice needs to know — credentials, a poll
    interval — and it lives inside the card rather than in a block below
    the list, so the answer to "where do I type the password?" is "in the
    thing you just picked".
    """
    ctl = f' data-controls="{esc(controls)}"' if controls else ""
    inner = (f'<div class="optbody" data-group="{esc(controls or name)}"'
             f' data-when="{esc(value)}"{"" if checked else " hidden"}>'
             f"{body_html}</div>") if body_html else ""
    return (
        f'<div class="optw{" sel" if checked else ""}" {wrap_extra}>'
        f'<label class="opt">'
        f'<input type="radio" name="{esc(name)}" value="{esc(value)}"'
        f'{" checked" if checked else ""}{ctl}>'
        f"{lead}"
        f'<span class="body"><span class="name">{esc(title)}</span>'
        + (f'<span class="sub">{esc(sub)}</span>' if sub else "")
        + f"</span>{trail}"
        '<span class="tick" aria-hidden="true">&#10003;</span></label>'
        f"{inner}</div>"
    )


def choice_cards(name: str, options, selected: str = "", *,
                 controls: str = "", bodies: dict | None = None) -> str:
    """Tappable radio cards. A <select> on a phone hides its options behind
    a picker; these are all visible and each is a 56px target.

    `bodies` maps an option value to the markup that option carries.
    """
    bodies = bodies or {}
    cards = "".join(
        option_card(name, value, title, sub, checked=(value == selected),
                    controls=controls, body_html=bodies.get(value, ""))
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


def disclosure(label: str, body_html: str, *, state: str = "",
               state_set: bool = False, open_: bool = False,
               slim: bool = False, top: bool = False) -> str:
    """A collapsed section whose summary already answers the question.

    "Ranges just for Theo — 80–170" tells you there are overrides without
    opening it; "using shared 70–180" tells you there are not.
    """
    cls = "".join(c for c in (" slim" if slim else "", " top" if top else ""))
    state_html = (f'<span class="state{" set" if state_set else ""}">'
                  f"{esc(state)}</span>") if state else ""
    return (f'<details class="{cls.strip()}"{" open" if open_ else ""}>'
            f'<summary><span class="label">{esc(label)}</span>{state_html}'
            f'</summary><div class="inner">{body_html}</div></details>')


def signal_bars(percent: int, *, live: bool = False) -> str:
    lit = 1 if percent < 30 else 2 if percent < 55 else 3 if percent < 78 else 4
    bars = "".join(f'<i class="{"on" if i < lit else ""}"></i>' for i in range(4))
    return (f'<span class="bars{" live" if live else ""}"'
            f' aria-label="{int(percent)}% signal">{bars}</span>')


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
                '<span class="lock">Open</span>')
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
