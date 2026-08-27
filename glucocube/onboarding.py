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
from . import network, ui, verify

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
    "people": "Who is this for?",
    "source": "Where the data comes from",
    "creds": "Connect the source",
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
    """A fresh draft, carrying over anything already configured.

    Re-running setup on a working device must not quietly drop the second
    person or regenerate their API secret.
    """
    people = []
    display = {}
    try:
        raw = json.loads(open(config_path).read())
        display = dict(raw.get("display") or {})
        for user in raw.get("users") or []:
            people.append({
                "name": "" if user.get("name") in STARTER_NAMES
                        else user.get("name", ""),
                "port": user.get("port"),
                "api_secret": user.get("api_secret", ""),
                "source": dict(user.get("source") or {}) or None,
                "thresholds": dict(user.get("thresholds") or {}),
            })
    except (OSError, ValueError):
        pass
    # A brand new device ships with two placeholder people; asking for one
    # name and offering "add another" is a better first question than
    # presenting two blanks.
    if people and all(not person["name"] and not person["source"]
                      for person in people):
        people = people[:1]
    if not people:
        people = [{"name": "", "port": None, "api_secret": "",
                   "source": None, "thresholds": {}}]
    return {
        "version": DRAFT_VERSION,
        "started_at": _now_ms(),
        "done": [],
        "wifi": {},
        "people": people,
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
    # Before the people: everything after this shows times, and a device
    # fresh off the image has no time zone at all, so it reads UTC.
    steps.append("timezone")
    steps.append("people")
    for index in range(len(draft.get("people") or [])):
        steps += [f"source:{index}", f"creds:{index}"]
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
    if kind == "people":
        return _render_people(handler, draft, step, banner)
    if kind == "source":
        return _render_source(handler, draft, step, index, banner)
    if kind == "creds":
        return _render_creds(handler, draft, step, index, banner)
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
  <li>For twiist: the wearer's Tidepool email and password.</li>
  <li>For an existing Nightscout site: its address and API secret.</li>
  <li>For Trio: nothing — this device will show you what to type in.</li>
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
    # Hidden until the script fills in the zone it detected — on the card
    # itself, not the label inside it, or an empty box is left behind.
    phone_card = ui.option_card(
        "tzmode", "phone", "Use this phone's setting", "",
        controls="tzmode",
        trail='<span class="sub" id="tzphonename"></span>',
        wrap_extra='id="tzphone" data-needs-js hidden')
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


def _render_people(handler, draft, step, banner) -> str:
    people = draft.get("people") or []
    # One spare slot beyond whoever is already here, so a second person can
    # be named without the JavaScript-only "add another" button.
    slots = [person.get("name", "") for person in people] + [""]
    rows = "".join(
        ui.row(f"Person {i + 1}" if i < len(people) else "Another person",
               ui.text_input(f"name{i}", name, input_id=f"name{i}",
                             placeholder="their name"
                             if i < len(people) else "leave blank if not needed"),
               inline=False, for_id=f"name{i}")
        for i, name in enumerate(slots)
    )
    template = ui.row("Another person",
                      ui.text_input("name__I__", "", input_id="name__I__",
                                    placeholder="their name"),
                      inline=False, for_id="name__I__")
    body = f"""{banner}
<p class="lede">One panel per person. Leave a name blank to drop that person.</p>
<form method="POST" action="/setup/people">
  <div id="people">{rows}</div>
  <template id="person-row">{template}</template>
  <button type="button" class="secondary" onclick="addPerson()"
          data-needs-js hidden>Add another person</button>
  {_actions("Continue")}
</form>"""
    return _shell(draft, step, "Who is this for?", body, script=PEOPLE_SCRIPT)


def _render_source(handler, draft, step, index, banner) -> str:
    person = (draft.get("people") or [{}])[index]
    selected = (person.get("source") or {}).get("type") or "push"
    body = f"""{banner}
<p class="lede">How does {ui.esc(_person_label(draft, index))}'s glucose data
reach this device?</p>
<form method="POST" action="/setup/source?i={index}">
  {ui.choice_cards("source", SOURCE_CARDS, selected)}
  {_actions("Continue")}
</form>"""
    return _shell(draft, step, _person_label(draft, index), body)


def _render_creds(handler, draft, step, index, banner) -> str:
    person = (draft.get("people") or [{}])[index]
    source = person.get("source") or {}
    kind = source.get("type") or "push"
    who = _person_label(draft, index)
    action = f"/setup/creds?i={index}"
    if kind == "push":
        port = person.get("port") or config_mod.FIRST_USER_PORT + index
        url = f"http://{network.get_lan_ip()}:{port}"
        secret = person.get("api_secret", "")
        body = f"""{banner}
<p class="lede">In Trio, open <b>Settings &rarr; Services &rarr;
Nightscout</b> and enter these two. Nothing else is needed here.</p>
<form method="POST" action="{action}">
  <input type="hidden" name="api_secret" value="{ui.esc(secret)}">
  {ui.row("URL", ui.copy_input("push_url", url, input_id="push_url"),
          inline=False)}
  {ui.row("API secret", ui.copy_input("push_secret", secret,
                                      input_id="push_secret"), inline=False)}
  <p class="note">This device's address can change if your router
  reassigns it; the settings page always shows the current one.</p>
  {_actions("Done — continue")}
</form>"""
        return _shell(draft, step, f"{who} — Trio", body)

    if kind == "tidepool":
        fields = (
            ui.row("Tidepool email",
                   ui.text_input("email", source.get("email", ""), kind="email",
                                 input_id="email",
                                 extra='autocapitalize="none" autocorrect="off"'
                                       ' spellcheck="false" required'),
                   inline=False, for_id="email")
            + ui.row("Tidepool password",
                     ui.password_input("password", source.get("password", ""),
                                       input_id="password"),
                     inline=False, for_id="password")
        )
        lede = ("The wearer links their My twiist Portal account to a free "
                "Tidepool account once, then signs in here.")
    else:
        fields = (
            ui.row("Site address",
                   ui.text_input("url", source.get("url", ""), kind="url",
                                 placeholder="mysite.example.com",
                                 input_id="url",
                                 extra='autocapitalize="none" autocorrect="off"'
                                       ' spellcheck="false" required'),
                   inline=False, for_id="url")
            + ui.row("API secret or access token",
                     ui.password_input("api_secret",
                                       source.get("api_secret", ""),
                                       input_id="api_secret"),
                     inline=False, for_id="api_secret",
                     hint="Either works — GlucoCube works out which.")
        )
        lede = "Where your existing Nightscout site lives."
    test_button = ('<button type="button" class="test secondary" '
                   'data-needs-js hidden>Test connection</button>')
    body = f"""{banner}
<p class="lede">{lede}</p>
<form method="POST" action="{action}" data-test="/setup/verify?i={index}">
  {fields}
  <div class="banner" id="testresult" hidden></div>
  <noscript><button type="submit" formaction="/setup/verify?i={index}"
    class="secondary">Test connection</button></noscript>
  {_actions("Continue", extra=test_button)}
</form>"""
    return _shell(draft, step, f"{who} — {'twiist' if kind == 'tidepool' else 'Nightscout'}",
                  body, script=TEST_SCRIPT)


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
    rows = []
    for index, person in enumerate(draft.get("people") or []):
        source = person.get("source") or {}
        kind = source.get("type") or "push"
        detail = {"push": "Trio or another uploader",
                  "tidepool": f"Tidepool — {source.get('email', '')}",
                  "nightscout": f"Nightscout — {source.get('url', '')}"}[kind]
        verified = person.get("verified")
        mark = (" &check; tested" if verified else
                " &mdash; not tested" if kind != "push" else "")
        rows.append(f"<tr><td>{ui.esc(person.get('name', ''))}</td>"
                    f"<td>{ui.esc(detail)}{mark}</td></tr>")
    display = draft.get("display") or {}
    body = f"""{banner}
<p class="lede">That is everything. Saving restarts the display, which takes
a few seconds.</p>
<div class="tablewrap"><table><tbody>{''.join(rows)}
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
    if step not in ("welcome", "wifi", "timezone", "people", "source",
                    "creds", "thresholds", "password", "review"):
        _redirect(handler, "/setup")
        return
    if step in ("source", "creds"):
        step = f"{step}:{_index(handler, draft)}"
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
    elif step == "people":
        # Keep the original position with each name: a blank first slot
        # must not shift the second person onto the first one's port and
        # credentials.
        named = []
        index = 0
        while f"name{index}" in form:
            name = form[f"name{index}"].strip()
            if name:
                named.append((index, name))
            index += 1
        if not named:
            handler._send(render(handler, draft, "people", banner=ui.banner(
                "err", "At least one person needs a name.")).encode(),
                "text/html; charset=utf-8", 400)
            return
        people = draft.get("people") or []
        merged = []
        for position, name in named:
            existing = people[position] if position < len(people) else {}
            merged.append({**{"port": None, "api_secret": "", "source": None,
                              "thresholds": {}}, **existing, "name": name})
        config_mod.assign_ports(
            merged, reserved={handler.server.config.admin_port})
        draft["people"] = merged
        mark_done(draft, "people")
    elif step == "source":
        index = _index(handler, draft)
        kind = form.get("source", "push")
        person = draft["people"][index]
        source = person.get("source") or {}
        if source.get("type") != kind:
            # Switching source discards the other kind's credentials
            # rather than carrying them along invisibly.
            person["source"] = {"type": kind}
        if kind == "push" and not person.get("api_secret"):
            person["api_secret"] = config_mod.readable_secret(16)
        person["verified"] = False
        mark_done(draft, f"source:{index}")
        # The credentials step belongs to the source just chosen.
        draft["done"] = [d for d in draft["done"] if d != f"creds:{index}"]
    elif step == "creds":
        index = _index(handler, draft)
        person = draft["people"][index]
        kind = (person.get("source") or {}).get("type") or "push"
        if kind == "push":
            # A push person has no source block; the secret the page just
            # showed them is what has to be saved.
            if form.get("api_secret"):
                person["api_secret"] = form["api_secret"].strip()
            person["source"] = None
        else:
            person["source"] = _source_from_form(person.get("source") or {},
                                                 form)
        missing = _missing_credentials(person["source"])
        if missing:
            save_draft(store, draft)
            handler._send(render(handler, draft, f"creds:{index}",
                                 banner=ui.banner("err", missing)).encode(),
                          "text/html; charset=utf-8", 400)
            return
        mark_done(draft, f"creds:{index}")
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
    """Write config.json — the first and only time the wizard touches it."""
    from .webadmin import restart_soon

    store = handler.server.store
    try:
        raw = json.loads(open(handler.server.config_path).read())
    except (OSError, ValueError):
        raw = {}
    raw["users"] = users_from_draft(
        draft, reserved={handler.server.config.admin_port})
    if not raw["users"]:
        handler._send(render(handler, draft, "review", banner=ui.banner(
            "err", "At least one person is needed.")).encode(),
            "text/html; charset=utf-8", 400)
        return
    raw.setdefault("display", {}).update(draft.get("display") or {})
    password = (draft.get("admin_password") or "").strip()
    if password:
        raw.setdefault("admin", {})["password"] = password
    try:
        config_mod.write_atomic(raw, handler.server.config_path)
    except Exception as exc:  # noqa: BLE001 - shown, never a restart loop
        handler._send(render(handler, draft, "review", banner=ui.banner(
            "err", f"Could not save: {ui.esc(exc)}")).encode(),
            "text/html; charset=utf-8", 500)
        return
    if password:
        # Otherwise the browser is locked out the moment the new process
        # starts, holding a cookie for the old password.
        handler._cookie_value = password
        handler.server.config.admin_password = password
    # The draft holds Tidepool and Nightscout credentials; keep only a
    # tombstone so a later /setup knows setup already happened.
    save_draft(store, {"version": DRAFT_VERSION, "committed_at": _now_ms()})
    log.info("Setup wizard finished; restarting")
    handler._send(_render_done(handler, draft).encode(),
                  "text/html; charset=utf-8")
    restart_soon()
