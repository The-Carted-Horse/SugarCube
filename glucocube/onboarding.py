"""The guided setup wizard served at /setup.

A fresh device shows a QR code that opens this on a phone. It used to open
the full settings page — the live screen, Wi-Fi, updates, every person,
every threshold and the admin password, all at once, in a form laid out
for a desktop. This asks one thing at a time.

Nothing is written to config.json until the last step. Answers accumulate
in a draft in the store, which means the wizard survives the restart a
save causes and the reboot joining Wi-Fi causes, and abandoning it halfway
leaves the device exactly as it was.
"""

import json
import logging
import time
from urllib.parse import parse_qs, urlparse

from . import config as config_mod
from . import glucocore, network, sync, ui, verify

log = logging.getLogger("glucocube.onboarding")

SETUP_KEY = "__setup"
DRAFT_VERSION = 1
STARTER_NAMES = {"Person A", "Person B"}

TITLES = {
    "welcome": "Welcome",
    "wifi": "Wi-Fi",
    "timezone": "Where is it?",
    "pair": "Pair with GlucoCore",
    "thresholds": "Ranges",
    "password": "Password for this page",
    "review": "Ready",
}


# ------------------------------------------------------------- state ----

def _now_ms() -> int:
    return int(time.time() * 1000)


def load_draft(store) -> dict:
    """The wizard in progress, if there is one.

    A committed draft is left behind as a tombstone rather than deleted,
    so this reports "nothing in progress" for it — otherwise a stale QR
    code would drop a finished device back at step one.
    """
    draft = store.get_params(SETUP_KEY)
    if draft.get("version") != DRAFT_VERSION or draft.get("committed_at"):
        return {}
    return draft


def save_draft(store, draft: dict) -> None:
    # replace, not merge: set_params drops falsy values, so a cleared
    # flag or an emptied list would silently not stick.
    store.replace_params(SETUP_KEY, draft)


def seed_draft(config_path: str) -> dict:
    """A fresh draft, carrying over display settings when re-running setup."""
    display = {}
    try:
        raw = json.loads(open(config_path).read())
        display = dict(raw.get("display") or {})
    except (OSError, ValueError):
        pass
    return {
        "version": DRAFT_VERSION,
        "started_at": _now_ms(),
        "done": [],
        "wifi": {},
        "device_name": "",
        "pairing": {},
        "display": display,
        "admin_password": "",
        "admin_password_off": False,
        "committed_at": None,
    }


def wifi_needed(draft: dict) -> bool:
    """Only ask about Wi-Fi when the device has none.

    Deliberately not network.connectivity(): that shells out to nmcli and
    can take seconds, and this is evaluated on every page of the wizard.
    """
    if (draft.get("wifi") or {}).get("skipped"):
        return False
    return network.hotspot_active_cached()


def steps_for(draft: dict) -> list[str]:
    steps = ["welcome"]
    if wifi_needed(draft):
        steps.append("wifi")
    steps.append("timezone")
    steps.append("pair")
    steps += ["thresholds", "password", "review"]
    return steps


def current_step(draft: dict) -> str:
    done = set(draft.get("done") or [])
    for step in steps_for(draft):
        if step not in done:
            return step
    return "review"


def next_step(draft: dict, step: str) -> str:
    steps = steps_for(draft)
    if step not in steps:
        return current_step(draft)
    index = steps.index(step)
    return steps[index + 1] if index + 1 < len(steps) else "review"


def path_for(step: str) -> str:
    return f"/setup/{step}"


def mark_done(draft: dict, step: str) -> None:
    done = list(draft.get("done") or [])
    if step not in done:
        done.append(step)
    draft["done"] = done


def reconcile_wifi(draft: dict) -> dict:
    """Settle a join that was in flight when the device rebooted.

    The __wifi params key is the authority — the display reads it too —
    so the draft only records that it was waiting on one.
    """
    wifi = dict(draft.get("wifi") or {})
    if not wifi.get("pending"):
        return draft
    state = network.state()
    if state.get("state") == "ok":
        wifi["pending"] = False
        wifi["joined_ssid"] = state.get("ssid")
        mark_done(draft, "wifi")
    elif state.get("state") == "failed":
        wifi["pending"] = False
        wifi["error"] = state.get("error")
    draft["wifi"] = wifi
    return draft


# ------------------------------------------------------------ render ----

def _progress(draft: dict, step: str) -> str:
    steps = steps_for(draft)
    here = steps.index(step) if step in steps else 0
    bars = "".join(f'<i class="{"done" if i <= here else ""}"></i>'
                   for i in range(len(steps)))
    return (f'<div class="steps">{bars}</div>'
            f'<p class="stepno">Step {here + 1} of {len(steps)}</p>')


def _shell(draft: dict, step: str, heading: str, body: str, *,
           script: str = "") -> str:
    return ui.page(
        f"GlucoCube setup — {TITLES.get(step, 'Setup')}",
        f"{_progress(draft, step)}<h1>{ui.esc(heading)}</h1>{body}",
        script=script,
    )


def _actions(primary: str, *, back: str = "", extra: str = "",
             note: str = "") -> str:
    back_html = (f'<a class="btn secondary" href="{ui.esc(back)}">Back</a>'
                 if back else "")
    note_html = f'<span class="note">{note}</span>' if note else ""
    return (f'<div class="actions stick">{back_html}'
            f'<button type="submit">{ui.esc(primary)}</button>'
            f'{extra}{note_html}</div>')


TIMEZONE_SCRIPT = """<script>
(function(){
  var zone = '';
  try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; }
  catch (err) {}
  var card = document.getElementById('tzphone');
  var field = document.getElementById('tz_detected');
  var list = document.getElementById('timezone');
  // Browsers report tzdata's retired names — Chromium says Asia/Calcutta,
  // Europe/Kiev, America/Buenos_Aires — so the detected zone is often not
  // one of the options below. Send it anyway and let the server map it:
  // it holds the alias table, and falls back to this picker if it cannot.
  // Filtering here instead would hide the one-tap answer from everyone in
  // India, Ukraine and Argentina.
  if (zone && list) {
    for (var i = 0; i < list.options.length; i++) {
      if (list.options[i].value === zone) { list.value = zone; break; }
    }
  }
  // The phone knows where it is. Offer that first, but only once this has
  // run — with no script there is nothing to detect, and the list below
  // is the whole answer.
  if (zone && card && field) {
    field.value = zone;
    document.getElementById('tzphonename').textContent = zone.replace(/_/g, ' ');
    card.hidden = false;
    card.querySelector('input').checked = true;
    if (window.glucoSync) window.glucoSync();
  }
  function chosen(){
    var mode = document.querySelector('[name=tzmode]:checked');
    if (mode && mode.value === 'phone') return field ? field.value : '';
    var list = document.getElementById('timezone');
    return list ? list.value : '';
  }
  function preview(){
    var out = document.getElementById('tzpreview');
    if (!out) return;
    var z = chosen();
    if (!z) { out.textContent = 'The device keeps the time it has now.'; return; }
    try {
      out.textContent = 'It is ' + new Date().toLocaleString(undefined,
        {timeZone: z, weekday: 'long', hour: 'numeric', minute: '2-digit'})
        + ' there.';
    } catch (err) { out.textContent = ''; }
  }
  document.addEventListener('change', preview);
  preview();
  setInterval(preview, 30000);
})();
</script>"""

PEOPLE_SCRIPT = """<script>
function addPerson(){
  const list = document.getElementById('people');
  const i = list.querySelectorAll('.row').length;
  list.insertAdjacentHTML('beforeend',
    document.getElementById('person-row').innerHTML.replaceAll('__I__', i));
  const added = list.querySelector('.row:last-child input');
  if (added) added.focus();
}
</script>"""

TEST_SCRIPT = """<script>
document.addEventListener('click', async (event) => {
  const button = event.target.closest('button.test');
  if (!button) return;
  event.preventDefault();
  const form = button.closest('form');
  const out = document.getElementById('testresult');
  out.hidden = false;
  out.className = 'banner info';
  out.textContent = 'Testing\\u2026';
  button.disabled = true;
  try {
    const response = await fetch(form.dataset.test, {
      method: 'POST', headers: {'Accept': 'application/json'},
      body: new URLSearchParams(new FormData(form)),
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

DONE_SCRIPT = """<script>
// The old process answers until it exits; once the new one answers, the
// device is running the settings that were just saved.
let gone = false;
setInterval(async () => {
  try {
    await fetch('/api/health.json', {cache: 'no-store'});
    if (gone) location.replace('/');
  } catch (err) { gone = true; }
}, 2000);
</script>"""


RENDERERS = {}          # filled in below, once each renderer exists


def render(handler, draft: dict, step: str, *, banner: str = "") -> str:
    return RENDERERS.get(step, _render_review)(handler, draft, step, banner)


def _render_welcome(handler, draft, step, banner) -> str:
    joined = (draft.get("wifi") or {}).get("joined_ssid")
    note = (ui.banner("ok", f"Connected to <b>{ui.esc(joined)}</b>. Picking up "
                            "where you left off.") if joined else "")
    body = f"""{banner}{note}
<p class="lede">A few questions and this screen is done. It takes about two
minutes.</p>
<h2>You will need</h2>
<ul>
  <li>Your Wi-Fi password, if the device is not on a network yet.</li>
  <li>A pairing code from GlucoCore — open <b>Devices</b> there, add this
      display and choose who it shows.</li>
</ul>
<form method="POST" action="/setup/welcome">
  {_actions("Start")}
</form>
<p class="note"><a href="/settings">Skip — take me to settings</a></p>"""
    return _shell(draft, step, "Set up GlucoCube", body)


def _render_wifi(draft_handler, draft, step, banner) -> str:
    wifi = draft.get("wifi") or {}
    notices = [banner]
    state = network.state()
    if wifi.get("error") or state.get("state") == "failed":
        reason = wifi.get("error") or state.get("error") or "unknown error"
        notices.append(ui.banner(
            "err", f"Last attempt failed: {ui.esc(reason)}"))
    networks = network.cached_networks()
    if network.scan_in_progress():
        hint = "Scanning&hellip;"
    elif networks:
        hint = f"{len(networks)} networks found."
    else:
        hint = ("No networks in the list — choose <b>Other network</b> and "
                "type the name exactly.")
    body = f"""{''.join(notices)}
<p class="lede">Pick your home network so the device can reach the internet.</p>
<form method="POST" action="/setup/wifi">
  {ui.network_picker(networks, selected=wifi.get("attempted_ssid", ""))}
  {ui.row("Password", ui.password_input("wifi_password", "",
          input_id="wifi_password"), inline=False, for_id="wifi_password")}
  <p class="note">{hint}</p>
  {_actions("Join this network")}
</form>
<form method="POST" action="/setup/wifi/skip">
  <button type="submit" class="quiet">This device is already online — skip</button>
</form>"""
    return _shell(draft, step, "Connect to Wi-Fi", body)


def _render_timezone(handler, draft, step, banner) -> str:
    from .webadmin import timezone_options

    current = (draft.get("display") or {}).get(
        "timezone", handler.server.config.display.timezone)
    phone_card = ui.option_card(
        "tzmode", "phone", "Use this phone's setting", "",
        controls="tzmode",
        trail='<span class="sub" id="tzphonename"></span>')
    # Hidden until the script fills in the zone it detected.
    phone_card = phone_card.replace('<label class="opt"',
                                    '<label class="opt" id="tzphone"'
                                    ' data-needs-js hidden', 1)
    list_card = ui.option_card("tzmode", "list", "Choose from a list", "",
                               checked=True, controls="tzmode")
    picker = ui.group(
        "tzmode", "list",
        ui.row("Time zone", ui.select("timezone", timezone_options(), current,
                                      input_id="timezone"),
               inline=False, for_id="timezone"),
        current="list")
    body = f"""{banner}
<p class="lede">So the clock and the times on the chart are right. A device
straight off the image has no time zone set, and reads UTC.</p>
<form method="POST" action="/setup/timezone">
  <input type="hidden" name="tz_detected" id="tz_detected" value="">
  <div class="opts">{phone_card}{list_card}</div>
  {picker}
  <p class="note" id="tzpreview"></p>
  {_actions("Continue")}
</form>"""
    return _shell(draft, step, "Where is it?", body, script=TIMEZONE_SCRIPT)


def _render_pair(handler, draft, step, banner) -> str:
    host = glucocore.GLUCOCORE_BASE.split("//")[-1]
    name = draft.get("device_name") or ""
    body = f"""{banner}
<p class="lede">In GlucoCore, open <b>Devices</b>, add this display and
choose who it shows. It gives you a six-digit code — type it in here.</p>
<form method="POST" action="/setup/pair">
  {ui.row("Pairing code",
          ui.text_input("code", "", placeholder="123456", input_id="code",
                        extra='inputmode="numeric" autocomplete="one-time-code"'
                              ' pattern="[0-9 ]*" maxlength="9"'
                              ' autocapitalize="off" spellcheck="false"'),
          inline=False, for_id="code",
          hint="It lasts ten minutes and works once. This display never"
               " needs your GlucoCore password.")}
  {ui.row("Name this display",
          ui.text_input("device_name", name, placeholder="Kitchen display",
                        input_id="device_name"),
          inline=False, for_id="device_name",
          hint="Optional — blank keeps the name you gave it in GlucoCore.")}
  {_actions("Pair this display")}
</form>
<p class="note">No account yet? Create one at <b>{ui.esc(host)}</b> on your
phone, then come back for the code.</p>"""
    return _shell(draft, step, "Pair with GlucoCore", body)


def _render_thresholds(handler, draft, step, banner) -> str:
    display = draft.get("display") or {}
    value = lambda key, default: display.get(key, default)
    body = f"""{banner}
<p class="lede">Readings are coloured against these. The defaults suit most
people — you can change them per person later.</p>
<form method="POST" action="/setup/thresholds">
  {ui.row("In range", '<div class="pair">'
          + ui.text_input("low", value("low", 70), kind="number")
          + ui.text_input("high", value("high", 180), kind="number")
          + "</div>", inline=False, hint="low and high, mg/dL")}
  {ui.row("Urgent", '<div class="pair">'
          + ui.text_input("urgent_low", value("urgent_low", 55), kind="number")
          + ui.text_input("urgent_high", value("urgent_high", 250), kind="number")
          + "</div>", inline=False,
          hint="below and above these, the panel turns red")}
  {_actions("Continue")}
</form>"""
    return _shell(draft, step, "Ranges", body)


PASSWORD_CARDS = (
    ("on", "Ask for a password",
     "Log in as admin — needed if anyone else can reach this network"),
    ("off", "No password",
     "Anyone on this network opens the dashboard and settings"),
)


def _render_password(handler, draft, step, banner) -> str:
    current = handler.server.config.admin_password
    mode = "off" if draft.get("admin_password_off") else "on"
    field = ui.row(
        "New password" if current else "Password",
        ui.password_input("admin_password", "",
                          placeholder="leave blank to keep the current one"
                                      if current else "",
                          input_id="admin_password"),
        inline=False, for_id="admin_password",
        hint="At least 6 characters. The device's own screen always shows"
             " the current one, so you cannot lock yourself out."
             + (" Leave it blank to keep the one in use." if current else ""))
    open_note = ('<p class="note">Fine on a home network you trust — the'
                 " device is only reachable from it. Not fine on a network"
                 " guests, flatmates or an office share. You can turn a"
                 " password on later under Access.</p>")
    body = f"""{banner}
<p class="lede">This page and the dashboard can be protected by a password.
The username is <b>admin</b>.</p>
{ui.row("Current password", ui.copy_input("current", current,
                                          input_id="current"), inline=False)
 if current else ""}
<form method="POST" action="/setup/password">
  {ui.choice_cards("mode", PASSWORD_CARDS, mode, controls="access")}
  {ui.group("access", "on", field, current=mode)}
  {ui.group("access", "off", open_note, current=mode)}
  {_actions("Continue")}
</form>"""
    return _shell(draft, step, "Password for this page", body)


def _render_review(handler, draft, step, banner) -> str:
    remote = (draft.get("pairing") or {}).get("config") or {}
    patient_rows = "".join(
        f"<tr><td>{ui.esc(sync.patient_label(remote, pid))}</td>"
        "<td>GlucoCore</td></tr>"
        for pid in (remote.get("patientIds") or [])
    )
    display = draft.get("display") or {}
    if (draft.get("admin_password") or "").strip():
        access = "a password you set"
    elif not draft.get("admin_password_off") \
            and handler.server.config.admin_password:
        access = "the password already in use"
    else:
        access = "no password — open to this network"
    body = f"""{banner}
<p class="lede">That is everything. This display is paired already — saving
writes it down and restarts on the new settings.</p>
<div class="tablewrap"><table><tbody>
<tr><td>Display</td><td>{ui.esc(draft.get('device_name', ''))}</td></tr>
{patient_rows}
<tr><td>In range</td><td>{display.get('low', 70):g}&ndash;{display.get('high', 180):g} mg/dL</td></tr>
<tr><td>Settings page</td><td>{ui.esc(access)}</td></tr>
</tbody></table></div>
<form method="POST" action="/setup/review">
  {_actions("Save and finish", note="Nothing has been saved until now.")}
</form>
<p class="note"><a href="/setup/welcome">Start again</a></p>"""
    return _shell(draft, step, "Ready", body)


def _render_done(handler, draft) -> str:
    password = handler.server.config.admin_password
    body = f"""
<p class="lede">Saved. The display is restarting on the new settings.</p>
{ui.row("Password for this page", ui.copy_input("pw", password,
        input_id="pw"), inline=False,
        hint="Write this down. The device's own screen shows it too.")
 if password else
 '<p class="note">There is no password: anyone on this network can open'
 ' the dashboard and the settings. Add one any time under'
 ' <a href="/settings/access">Access</a>.</p>'}
<p><a class="btn" href="/">Open the dashboard</a></p>
<p class="note">Readings appear as soon as the first ones arrive. The
<a href="/log">sync log</a> shows what is coming in.</p>"""
    return ui.page("GlucoCube is set up",
                   f"<h1>All set</h1>{body}", script=DONE_SCRIPT)


RENDERERS.update({
    "welcome": _render_welcome,
    "wifi": _render_wifi,
    "timezone": _render_timezone,
    "pair": _render_pair,
    "thresholds": _render_thresholds,
    "password": _render_password,
    "review": _render_review,
})


# ------------------------------------------------------------ routing ----

def handles(path: str) -> bool:
    return path == "/setup" or path.startswith("/setup/")


def open_without_login(path: str) -> bool:
    """Setup is reachable unauthenticated while the setup hotspot is up.

    On the hotspot, WPA2 is the boundary: the phone already had to know
    the hotspot password shown on the device's screen to be here at all.
    It matters because a phone's captive-portal browser carries neither
    the ?key= link nor a cookie.
    """
    return handles(path) and network.hotspot_active_cached()


def _query(path: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}


def _redirect(handler, target: str) -> None:
    handler._send(b"", "text/html", 303, {"Location": target})


def _already_configured(handler) -> bool:
    """Same test the display uses to decide it is past first-boot."""
    if handler.server.config.glucocore and handler.server.config.glucocore.device_token:
        return True
    users = handler.server.config.users
    if any((user.source or {}).get("type") for user in users):
        return True
    return any(handler.server.store.snapshot(user.name).sgv_date
               for user in users)


def do_get(handler, path: str) -> None:
    store = handler.server.store
    if path == "/setup/done":
        # Never seeds a draft: this is served after the commit wrote the
        # tombstone, and starting a new one here would undo that.
        handler._send(_render_done(handler, {}).encode(),
                      "text/html; charset=utf-8")
        return
    draft = load_draft(store)
    if path == "/setup":
        query = _query(handler.path)
        if not draft:
            if _already_configured(handler) and not query.get("again"):
                # A stale QR or bookmark on a working device should not
                # drop someone back into setup.
                _redirect(handler, "/settings")
                return
            draft = seed_draft(handler.server.config_path)
            save_draft(store, draft)
        draft = reconcile_wifi(draft)
        save_draft(store, draft)
        _redirect(handler, path_for(current_step(draft)))
        return
    if not draft:
        draft = seed_draft(handler.server.config_path)
        save_draft(store, draft)
    step = path[len("/setup/"):]
    if step not in RENDERERS:
        # A stale bookmark or a typed URL. One list of steps, so a new one
        # cannot be reachable in the wizard but a 303 from the address bar.
        _redirect(handler, "/setup")
        return
    handler._send(render(handler, draft, step).encode(),
                  "text/html; charset=utf-8")


def do_post(handler, path: str, form: dict) -> None:
    store = handler.server.store
    draft = load_draft(store) or seed_draft(handler.server.config_path)
    step = path[len("/setup/"):]

    if step == "wifi/skip":
        draft["wifi"] = {**(draft.get("wifi") or {}), "skipped": True}
        mark_done(draft, "wifi")
        save_draft(store, draft)
        _redirect(handler, path_for(current_step(draft)))
        return
    if step == "wifi/rescan":
        network.refresh_scan_async(force=True)
        _redirect(handler, "/setup/wifi")
        return
    if step == "review":
        _commit(handler, draft)
        return

    if step == "welcome":
        mark_done(draft, "welcome")
    elif step == "timezone":
        mode = form.get("tzmode", "list")
        asked = ((form.get("tz_detected") or "").strip() if mode == "phone"
                 else (form.get("timezone") or "").strip())
        chosen = config_mod.canonical_timezone(asked)
        if asked and not chosen:
            # The phone reported a name this device's tzdata has never
            # heard of. That is the browser's doing, not the user's, so
            # show the picker rather than an error page.
            handler._send(render(handler, draft, "timezone", banner=ui.banner(
                "warn", f"This device does not know a zone called "
                        f"<b>{ui.esc(asked)}</b> — please pick the closest "
                        "one below.")).encode(),
                "text/html; charset=utf-8", 200)
            return
        draft["display"] = {**(draft.get("display") or {}),
                            "timezone": chosen}
        # Applied now rather than at the end, so every later step — and the
        # device's own screen — is already telling the right time.
        config_mod.apply_timezone(chosen)
        mark_done(draft, "timezone")
    elif step == "wifi":
        _start_join(handler, draft, form)
        return
    elif step == "pair":
        # The claim is the only step that talks to GlucoCore, and it is the
        # one that cannot be undone by going back: a code is spent once. So
        # what it returns is kept in the draft and the commit below reads
        # it, rather than the code being redeemed a second time at the end.
        device_name = (form.get("device_name") or "").strip()
        result, claimed = verify.glucocore_claim(
            form.get("code") or "", network.hardware_id(), device_name)
        if not result.ok:
            handler._send(render(handler, draft, "pair",
                                 banner=ui.failure(result.message,
                                                   result.detail)).encode(),
                          "text/html; charset=utf-8", 400)
            return
        device = claimed.get("device") or {}
        remote = device.get("config") or {}
        if not (remote.get("patientIds") or []):
            handler._send(render(handler, draft, "pair", banner=ui.banner(
                "err", "That pairing has nobody on it yet. Choose who this "
                       "display shows in GlucoCore, then pair again.")).encode(),
                "text/html; charset=utf-8", 400)
            return
        draft["device_name"] = device.get("name") or device_name
        draft["pairing"] = {
            "device_id": device.get("id") or "",
            "device_token": claimed["deviceToken"],
            "hardware_id": network.hardware_id(),
            "config": remote,
        }
        # The bands GlucoCore holds for this display are the answer to the
        # next step's question, so it is asked with them already filled in.
        draft["display"] = sync.display_from_remote(draft.get("display") or {},
                                                    remote)
        mark_done(draft, "pair")
    elif step == "thresholds":
        display = dict(draft.get("display") or {})
        for key in ("low", "high", "urgent_low", "urgent_high"):
            value = (form.get(key) or "").strip()
            if value:
                try:
                    display[key] = float(value)
                except ValueError:
                    pass
        draft["display"] = display
        mark_done(draft, "thresholds")
    elif step == "password":
        password = (form.get("admin_password") or "").strip()
        off = (form.get("mode") or "on").strip() == "off"
        current = handler.server.config.admin_password
        # Set before validating so a re-render comes back on the card the
        # form was submitted from; nothing is saved until the step passes.
        draft["admin_password_off"] = off
        problem = ""
        if off:
            password = ""
        elif password and len(password) < 6:
            problem = ("Use at least 6 characters, or choose No password."
                       if not current else
                       "Use at least 6 characters, or leave it blank to keep "
                       "the current password.")
        elif not password and not current:
            problem = "Type a password, or choose No password."
        if problem:
            handler._send(render(handler, draft, "password",
                                 banner=ui.banner("err", problem)).encode(),
                          "text/html; charset=utf-8", 400)
            return
        draft["admin_password"] = password
        mark_done(draft, "password")
    else:
        _redirect(handler, "/setup")
        return

    save_draft(store, draft)
    _redirect(handler, path_for(next_step(draft, step)))


def _start_join(handler, draft, form: dict) -> None:
    import threading

    ssid = ((form.get("wifi_other_ssid") or "").strip()
            or (form.get("wifi_ssid") or "").strip())
    if ssid == "__other__":
        ssid = ""
    if not ssid:
        handler._send(render(handler, draft, "wifi", banner=ui.banner(
            "err", "Tap a network, or choose <b>Other network</b> and type "
                   "its name.")).encode(), "text/html; charset=utf-8", 400)
        return
    password = form.get("wifi_password", "")
    hidden = bool(form.get("wifi_hidden"))
    # Recorded before the join starts: a successful join reboots the
    # device, and that can happen at any moment afterwards.
    draft["wifi"] = {"attempted_ssid": ssid, "pending": True, "error": ""}
    save_draft(handler.server.store, draft)
    hotspot_pw = handler.server.store.get_params("__network").get(
        "hotspot_password", "")

    def join_then_reboot():
        # The join tears the hotspot down, killing the phone's
        # connection — let the response reach it first.
        time.sleep(2)
        ok, _ = network.connect_wifi(ssid, password, hidden)
        if ok:
            network.reboot()
        elif hotspot_pw:
            network.start_hotspot(hotspot_pw, prescan=False)

    threading.Thread(target=join_then_reboot, daemon=True).start()
    handler._send(handler._joining_page(ssid), "text/html; charset=utf-8")


def keep_local_users(users: list[dict]) -> list[dict]:
    """Everyone a GlucoCore pairing does not replace.

    People fed by an uploader, Tidepool or a Nightscout site are none of
    GlucoCore's business and stay exactly as they are — pairing adds to a
    display rather than clearing it. The two placeholders a fresh image
    ships with are not people at all: if nobody has touched them they go,
    rather than leaving two empty panels beside the real ones.
    """
    kept = []
    for user in users:
        if (user.get("source") or {}).get("type") == "glucocore":
            continue
        if user.get("name") in STARTER_NAMES and not user.get("source"):
            continue
        kept.append(user)
    return kept


def _commit(handler, draft: dict) -> None:
    """Write config.json for the pairing the code already earned."""
    from .webadmin import restart_soon

    store = handler.server.store
    pairing = draft.get("pairing") or {}
    remote = pairing.get("config") or {}
    patient_ids = [str(pid) for pid in (remote.get("patientIds") or []) if pid]

    if not pairing.get("device_token") or not patient_ids:
        handler._send(render(handler, draft, "review", banner=ui.banner(
            "err", "This display is not paired yet — go back and enter the "
                   "code from GlucoCore.")).encode(),
            "text/html; charset=utf-8", 400)
        return

    display = draft.get("display") or {}
    import secrets
    try:
        raw = json.loads(open(handler.server.config_path).read())
    except (OSError, ValueError):
        raw = {}
    users = keep_local_users(raw.get("users") or [])
    taken = {(user.get("name") or "").casefold() for user in users}
    for patient_id in patient_ids:
        name = sync.patient_label(remote, patient_id)
        while name.casefold() in taken:
            name = f"{name} 2"
        taken.add(name.casefold())
        users.append({
            "name": name,
            "port": None,
            "api_secret": secrets.token_hex(12),
            "source": {
                "type": "glucocore",
                "patient_id": patient_id,
                "poll_seconds": 60,
            },
        })

    config_mod.assign_ports(users, reserved={handler.server.config.admin_port})
    raw["users"] = users
    raw.setdefault("display", {}).update(display)
    raw["glucocore"] = {
        "device_id": pairing.get("device_id", ""),
        "device_token": pairing["device_token"],
        "hardware_id": pairing.get("hardware_id", ""),
        "name": draft.get("device_name") or "",
    }
    password = (draft.get("admin_password") or "").strip()
    password_off = bool(draft.get("admin_password_off")) and not password
    if password:
        admin = raw.setdefault("admin", {})
        admin["password"] = password
        admin.pop("password_off", None)
    elif password_off:
        # Recorded as a choice, so the settings hub stops offering to set
        # one every time it is opened.
        admin = raw.setdefault("admin", {})
        admin["password"] = ""
        admin["password_off"] = True
    try:
        config_mod.write_atomic(raw, handler.server.config_path)
    except Exception as exc:  # noqa: BLE001
        handler._send(render(handler, draft, "review", banner=ui.banner(
            "err", f"Could not save: {ui.esc(exc)}")).encode(),
            "text/html; charset=utf-8", 500)
        return
    if password:
        handler._cookie_value = password
        handler.server.config.admin_password = password
    elif password_off:
        handler.server.config.admin_password = ""
        handler.server.config.admin_password_off = True
    save_draft(store, {"version": DRAFT_VERSION, "committed_at": _now_ms()})
    log.info("Setup wizard finished; restarting")
    handler._send(_render_done(handler, draft).encode(),
                  "text/html; charset=utf-8")
    restart_soon()
