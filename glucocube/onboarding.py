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
from . import glucocore, network, ui, verify

log = logging.getLogger("glucocube.onboarding")

SETUP_KEY = "__setup"
DRAFT_VERSION = 1
STARTER_NAMES = {"Person A", "Person B"}

SOURCE_CARDS = (
    ("push", "Trio, or another uploader",
     "The pump app sends readings straight to this device"),
    ("tidepool", "twiist",
     "Pulled from the wearer's Tidepool account"),
    ("nightscout", "A Nightscout site",
     "Pulled from an existing cloud Nightscout"),
)

TITLES = {
    "welcome": "Welcome",
    "wifi": "Wi-Fi",
    "timezone": "Where is it?",
    "account": "GlucoCore account",
    "verify_email": "Check your email",
    "device_name": "Name this display",
    "patients": "Who to show",
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
        "account": {},
        "device_name": "",
        "patient_ids": [],
        "patient_names": {},
        "display": display,
        "admin_password": "",
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
    steps.append("account")
    if draft.get("account", {}).get("pending_verification"):
        steps.append("verify_email")
    steps.append("device_name")
    steps.append("patients")
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
    kind, _, index = step.partition(":")
    return f"/setup/{kind}" + (f"?i={index}" if index else "")


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
        f"GlucoCube setup — {TITLES.get(step.partition(':')[0], 'Setup')}",
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


def _person_label(draft: dict, index: int) -> str:
    people = draft.get("people") or []
    if 0 <= index < len(people) and people[index].get("name"):
        return people[index]["name"]
    return f"Person {index + 1}"


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


def render(handler, draft: dict, step: str, *, banner: str = "") -> str:
    kind, _, raw_index = step.partition(":")
    index = int(raw_index) if raw_index else 0
    if kind == "welcome":
        return _render_welcome(handler, draft, step, banner)
    if kind == "wifi":
        return _render_wifi(handler, draft, step, banner)
    if kind == "timezone":
        return _render_timezone(handler, draft, step, banner)
    if kind == "account":
        return _render_account(handler, draft, step, banner)
    if kind == "verify_email":
        return _render_verify_email(handler, draft, step, banner)
    if kind == "device_name":
        return _render_device_name(handler, draft, step, banner)
    if kind == "patients":
        return _render_patients(handler, draft, step, banner)
    if kind == "thresholds":
        return _render_thresholds(handler, draft, step, banner)
    if kind == "password":
        return _render_password(handler, draft, step, banner)
    return _render_review(handler, draft, step, banner)


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
  <li>A GlucoCore account — sign in or create one during setup.</li>
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


def _render_account(handler, draft, step, banner) -> str:
    account = draft.get("account") or {}
    body = f"""{banner}
<p class="lede">Sign in to GlucoCore, or create an account. Your password is
used once — the device keeps a read-only token instead.</p>
<form method="POST" action="/setup/account">
  <label>Full name (optional, for new accounts)</label>
  <input name="name" type="text" value="{ui.esc(account.get('name', ''))}"
         autocomplete="name">
  <label>Email</label>
  <input name="email" type="email" required autocomplete="username"
         value="{ui.esc(account.get('email', ''))}">
  <label>Password</label>
  <input name="password" type="password" required autocomplete="current-password">
  <input type="hidden" name="mode" value="login" id="acct_mode">
  <div class="actions stick">
    <button type="submit" onclick="document.getElementById('acct_mode').value='login'">
      Sign in</button>
    <button type="submit" class="secondary"
            onclick="document.getElementById('acct_mode').value='signup'">
      Create account</button>
  </div>
</form>"""
    return _shell(draft, step, "Your GlucoCore account", body)


def _render_verify_email(handler, draft, step, banner) -> str:
    email = (draft.get("account") or {}).get("email", "")
    body = f"""{banner}
<p class="lede">We sent a six-digit code to <b>{ui.esc(email)}</b>. Enter it
in your email app, then tap continue here once your account is ready.</p>
<form method="POST" action="/setup/verify_email">
  {_actions("I've verified — continue")}
</form>
<p class="note">If you already have an account, try signing in instead from
the previous step.</p>"""
    return _shell(draft, step, "Check your email", body)


def _render_device_name(handler, draft, step, banner) -> str:
    name = draft.get("device_name") or ""
    body = f"""{banner}
<p class="lede">How this display appears in GlucoCore — e.g. Kitchen, Grace's room.</p>
<form method="POST" action="/setup/device_name">
  <label>Display name</label>
  <input name="device_name" type="text" required placeholder="Kitchen display"
         value="{ui.esc(name)}">
  {_actions("Continue")}
</form>"""
    return _shell(draft, step, "Name this display", body)


def _render_patients(handler, draft, step, banner) -> str:
    patients = draft.get("available_patients") or []
    selected = set(draft.get("patient_ids") or [])
    if not patients:
        body = f"""{banner}
<p class="lede">No patients are visible yet. Go back and sign in again once
your account is ready.</p>
<p><a class="btn secondary" href="/setup/account">Back to sign in</a></p>"""
        return _shell(draft, step, "Who to show", body)
    boxes = []
    for patient in patients:
        pid = patient.get("userId") or patient.get("userid") or ""
        label = patient.get("name") or patient.get("email") or pid
        checked = " checked" if pid in selected else ""
        boxes.append(
            f'<label style="display:block;margin:8px 0">'
            f'<input type="checkbox" name="patient_ids" value="{ui.esc(pid)}"'
            f'{checked}> {ui.esc(label)}</label>'
        )
    body = f"""{banner}
<p class="lede">Choose whose glucose appears on this display. You can change
this later from GlucoCore.</p>
<form method="POST" action="/setup/patients">
  {''.join(boxes)}
  {_actions("Continue")}
</form>"""
    return _shell(draft, step, "Who to show", body)


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


def _render_password(handler, draft, step, banner) -> str:
    current = handler.server.config.admin_password
    body = f"""{banner}
<p class="lede">This page and the dashboard are protected by a password.
The username is <b>admin</b>.</p>
{ui.row("Current password", ui.copy_input("current", current,
                                          input_id="current"), inline=False)
 if current else ""}
<form method="POST" action="/setup/password">
  {ui.row("New password", ui.password_input("admin_password", "",
          placeholder="leave blank to keep the current one",
          input_id="admin_password"), inline=False, for_id="admin_password",
          hint="At least 6 characters. The device's own screen always shows"
               " the current one, so you cannot lock yourself out.")}
  {_actions("Continue")}
</form>"""
    return _shell(draft, step, "Password for this page", body)


def _render_review(handler, draft, step, banner) -> str:
    names = draft.get("patient_names") or {}
    patient_rows = "".join(
        f"<tr><td>{ui.esc(names.get(pid, pid))}</td><td>GlucoCore</td></tr>"
        for pid in (draft.get("patient_ids") or [])
    )
    display = draft.get("display") or {}
    body = f"""{banner}
<p class="lede">That is everything. Saving pairs this display with GlucoCore
and restarts it.</p>
<div class="tablewrap"><table><tbody>
<tr><td>Display</td><td>{ui.esc(draft.get('device_name', ''))}</td></tr>
{patient_rows}
<tr><td>In range</td><td>{display.get('low', 70):g}&ndash;{display.get('high', 180):g} mg/dL</td></tr>
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
 if password else ""}
<p><a class="btn" href="/">Open the dashboard</a></p>
<p class="note">Readings appear as soon as the first ones arrive. The
<a href="/log">sync log</a> shows what is coming in.</p>"""
    return ui.page("GlucoCube is set up",
                   f"<h1>All set</h1>{body}", script=DONE_SCRIPT)


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


def _index(handler, draft: dict) -> int:
    try:
        return max(0, min(int(_query(handler.path).get("i", "0")),
                          len(draft.get("people") or []) - 1))
    except ValueError:
        return 0


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
    if step not in ("welcome", "wifi", "timezone", "account", "verify_email",
                    "device_name", "patients", "thresholds", "password", "review"):
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
    if step == "verify":
        _do_verify(handler, draft, form)
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
    elif step == "account":
        email = (form.get("email") or "").strip()
        password = form.get("password") or ""
        name = (form.get("name") or "").strip()
        mode = form.get("mode") or "login"
        draft["account"] = {"email": email, "name": name}
        if mode == "signup":
            draft["account"]["password"] = password
            try:
                glucocore.signup(email, password, name)
                draft["account"]["pending_verification"] = True
                mark_done(draft, "account")
            except Exception as exc:  # noqa: BLE001
                handler._send(render(handler, draft, "account", banner=ui.banner(
                    "err", f"Could not start signup: {ui.esc(exc)}")).encode(),
                    "text/html; charset=utf-8", 400)
                return
        else:
            result = verify.glucocore_login(email, password)
            if not result.ok:
                handler._send(render(handler, draft, "account", banner=ui.banner(
                    "err", result.message)).encode(),
                    "text/html; charset=utf-8", 400)
                return
            try:
                token, userid = glucocore.login(email, password)
            except Exception as exc:  # noqa: BLE001
                handler._send(render(handler, draft, "account", banner=ui.banner(
                    "err", str(exc))).encode(), "text/html; charset=utf-8", 400)
                return
            draft["account"].update({
                "session_token": token,
                "user_id": userid,
                "pending_verification": False,
            })
            draft["available_patients"] = glucocore.list_patients(token, userid)
            mark_done(draft, "account")
            draft["done"] = [d for d in draft.get("done", []) if d != "verify_email"]
    elif step == "verify_email":
        account = draft.get("account") or {}
        email = account.get("email", "")
        password = account.get("password") or ""
        if not password:
            handler._send(render(handler, draft, "verify_email", banner=ui.banner(
                "warn", "Sign in again from the account step after verifying your email.")).encode(),
                "text/html; charset=utf-8", 200)
            draft["done"] = [d for d in draft.get("done", []) if d != "account"]
            save_draft(store, draft)
            _redirect(handler, "/setup/account")
            return
        result = verify.glucocore_login(email, password)
        if not result.ok:
            handler._send(render(handler, draft, "verify_email", banner=ui.banner(
                "err", result.message)).encode(), "text/html; charset=utf-8", 400)
            return
        token, userid = glucocore.login(email, password)
        draft["account"].update({
            "session_token": token,
            "user_id": userid,
            "pending_verification": False,
        })
        draft["account"].pop("password", None)
        draft["available_patients"] = glucocore.list_patients(token, userid)
        mark_done(draft, "verify_email")
        mark_done(draft, "account")
    elif step == "device_name":
        name = (form.get("device_name") or "").strip()
        if not name:
            handler._send(render(handler, draft, "device_name", banner=ui.banner(
                "err", "Enter a name for this display.")).encode(),
                "text/html; charset=utf-8", 400)
            return
        draft["device_name"] = name
        mark_done(draft, "device_name")
    elif step == "patients":
        selected = form.get("patient_ids", [])
        if isinstance(selected, str):
            selected = [selected]
        if not selected:
            handler._send(render(handler, draft, "patients", banner=ui.banner(
                "err", "Choose at least one person.")).encode(),
                "text/html; charset=utf-8", 400)
            return
        names = {}
        for patient in draft.get("available_patients") or []:
            pid = patient.get("userId") or patient.get("userid") or ""
            names[pid] = patient.get("name") or patient.get("email") or pid
        draft["patient_ids"] = selected
        draft["patient_names"] = {pid: names.get(pid, pid) for pid in selected}
        mark_done(draft, "patients")
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
        if password and len(password) < 6:
            handler._send(render(handler, draft, "password", banner=ui.banner(
                "err", "Use at least 6 characters, or leave it blank to keep "
                       "the current password.")).encode(),
                "text/html; charset=utf-8", 400)
            return
        draft["admin_password"] = password
        mark_done(draft, "password")
    else:
        _redirect(handler, "/setup")
        return

    save_draft(store, draft)
    _redirect(handler, path_for(next_step(draft, _step_key(handler, step, draft))))


def _step_key(handler, step: str, draft: dict) -> str:
    if step in ("source", "creds"):
        return f"{step}:{_index(handler, draft)}"
    return step


def _source_from_form(existing: dict, form: dict) -> dict | None:
    kind = existing.get("type") or "push"
    if kind == "push":
        return None
    if kind == "tidepool":
        return {"type": "tidepool",
                "email": (form.get("email") or "").strip(),
                "password": form.get("password") or "",
                "poll_seconds": existing.get("poll_seconds", 60)}
    url = (form.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return {"type": "nightscout", "url": url,
            "api_secret": (form.get("api_secret") or "").strip(),
            "poll_seconds": existing.get("poll_seconds", 60)}


def _missing_credentials(source) -> str:
    if not source:
        return ""
    if source["type"] == "tidepool" and not (source.get("email")
                                             and source.get("password")):
        return "Both the Tidepool email and password are needed."
    if source["type"] == "nightscout" and not source.get("url"):
        return "The Nightscout site address is needed."
    return ""


def _do_verify(handler, draft, form: dict) -> None:
    index = _index(handler, draft)
    person = draft["people"][index]
    source = _source_from_form(person.get("source") or {}, form)
    person["source"] = source
    result = verify.source(source or {"type": "push"})
    person["verified"] = bool(result.ok)
    save_draft(handler.server.store, draft)
    if "application/json" in (handler.headers.get("Accept") or ""):
        handler._send(json.dumps(result.as_dict()).encode(), "application/json")
        return
    handler._send(render(handler, draft, f"creds:{index}",
                         banner=ui.banner("ok" if result.ok else "err",
                                          ui.esc(result.message))).encode(),
                  "text/html; charset=utf-8")


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


def users_from_draft(draft: dict, reserved) -> list[dict]:
    users = []
    for person in draft.get("people") or []:
        source = person.get("source") or None
        user = {
            "name": person["name"],
            "port": person.get("port"),
            "api_secret": person.get("api_secret")
                          or config_mod.readable_secret(16),
        }
        if person.get("thresholds"):
            user["thresholds"] = person["thresholds"]
        if source:
            user["source"] = source
        users.append(user)
    config_mod.assign_ports(users, reserved=reserved)
    return users


def _commit(handler, draft: dict) -> None:
    """Write config.json and register with GlucoCore."""
    from .webadmin import restart_soon

    store = handler.server.store
    account = draft.get("account") or {}
    token = account.get("session_token")
    patient_ids = draft.get("patient_ids") or []
    device_name = (draft.get("device_name") or "").strip()

    if not token or not patient_ids or not device_name:
        handler._send(render(handler, draft, "review", banner=ui.banner(
            "err", "Missing account, patients, or device name — go back and "
                   "complete each step.")).encode(),
            "text/html; charset=utf-8", 400)
        return

    display = draft.get("display") or {}
    remote_config = {
        "version": 1,
        "patientIds": patient_ids,
        "display": {
            "timezone": display.get("timezone", ""),
            "units": "mg/dL",
            "low": display.get("low", 70),
            "high": display.get("high", 180),
            "urgent_low": display.get("urgent_low", 55),
            "urgent_high": display.get("urgent_high", 250),
            "stale_minutes": display.get("stale_minutes", 12),
        },
        "perPatient": {},
    }

    hw_id = network.hardware_id()
    try:
        reg = glucocore.register_device(
            token, device_name, hw_id, patient_ids, config=remote_config,
        )
    except Exception as exc:  # noqa: BLE001
        handler._send(render(handler, draft, "review", banner=ui.banner(
            "err", f"Could not register with GlucoCore: {ui.esc(exc)}")).encode(),
            "text/html; charset=utf-8", 500)
        return

    device_token = reg.get("deviceToken") or ""
    device = reg.get("device") or {}
    device_id = device.get("id") or ""

    if not device_token:
        handler._send(render(handler, draft, "review", banner=ui.banner(
            "err", "GlucoCore did not return a device token.")).encode(),
            "text/html; charset=utf-8", 500)
        return

    import secrets
    names = draft.get("patient_names") or {}
    users = []
    for patient_id in patient_ids:
        users.append({
            "name": names.get(patient_id, patient_id),
            "port": None,
            "api_secret": secrets.token_hex(12),
            "source": {
                "type": "glucocore",
                "patient_id": patient_id,
                "poll_seconds": 60,
            },
        })

    try:
        raw = json.loads(open(handler.server.config_path).read())
    except (OSError, ValueError):
        raw = {}
    config_mod.assign_ports(users, reserved={handler.server.config.admin_port})
    raw["users"] = users
    raw.setdefault("display", {}).update(display)
    raw["glucocore"] = {
        "device_id": device_id,
        "device_token": device_token,
        "hardware_id": hw_id,
    }
    password = (draft.get("admin_password") or "").strip()
    if password:
        raw.setdefault("admin", {})["password"] = password
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
    save_draft(store, {"version": DRAFT_VERSION, "committed_at": _now_ms()})
    log.info("Setup wizard finished; restarting")
    handler._send(_render_done(handler, draft).encode(),
                  "text/html; charset=utf-8")
    restart_soon()
