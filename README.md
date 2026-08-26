# GlucoCube

A wall-mounted glucose dashboard for two (or more) people, built for a
Raspberry Pi with the official 7" touchscreen. It boots straight into the
display — no desktop environment — and shows each person's current glucose,
trend, a 3-hour history chart, a 2-hour forecast, insulin on board, carbs on
board, and recent treatments.

![Screenshot](docs/screenshot.png)

## Features

- **Per-person data sources** — each person's data can arrive by:
  - **Push**: point a [Trio](https://github.com/nightscout/Trio) (or any
    Nightscout uploader) at the Pi — it speaks a minimal Nightscout v1 API,
    one port per person
  - **Pull from Tidepool** — for the twiist AID system, which uploads to
    Tidepool automatically; readings, boluses, carbs, IOB/COB, and pump
    settings all come across
  - **Pull from a Nightscout site** — for people with an existing cloud
    Nightscout; API secrets and access tokens both work (auto-detected)
- **Glucose forecast** — a 2-hour forecast curve with a confidence band on
  every chart, plus the projected value 2 hours out: uses the AID system's
  own prediction when available (Trio's `predBGs`, Loop's `predicted`),
  otherwise runs an oref0-style model (exponential insulin activity,
  deviation-based carb absorption) with therapy settings pulled from the
  person's Nightscout profile or Tidepool pump settings. Estimates are
  marked with `~`.
- **Web app** — the same dashboard in any browser, auto-refreshing,
  responsive, with light/dark themes. Settings (people, sources, thresholds)
  and a sync log are managed from the browser too; no SSH needed after
  install. Works great through a Cloudflare tunnel.
- **Touchscreen controls** — tap the sun/moon on the display for light or
  dark, and **SETTINGS** beside it to pop a QR code that opens the
  settings page on a phone, already signed in. The panel is read directly
  from `/dev/input`, so both work on the ready-made image as well as on a
  manual install; the theme switch is on the settings page too.
- **Update channels** — standard releases, or beta with the pre-releases
  as well; switching channels moves the device onto that channel's newest
  release straight away.
- **Per-person thresholds** — low/high/urgent ranges per person, with
  global defaults.
- **Guided setup from a phone** — a fresh device shows a QR code that opens
  a step-by-step wizard: Wi-Fi, where in the world it is so the clock is
  right, and the pairing code from GlucoCore that says who it shows, one
  question per screen. Credentials are
  tested before they're saved, and nothing is written until the last step.
  With no network at all the device opens its own setup hotspot — join it
  and the wizard opens by itself, no second QR code to scan. Once a network
  is chosen the device reboots onto it and shows its new address (also
  reachable as `http://glucocube.local/` on the ready-made image).

## Install

### Option A: flash the ready-made image

Grab `glucocube-<version>.img.xz` from the
[releases page](https://github.com/The-Carted-Horse/SugarCube/releases),
flash it with Raspberry Pi Imager (or `dd`), boot the Pi, and follow the
QR codes on screen. That's the whole install.

(Images are built by the `Build and release` GitHub Actions workflow, which
runs on every push to `main` — see [Cutting a release](#cutting-a-release).)

### Option B: install on an existing Raspberry Pi OS

Use Raspberry Pi OS **Lite** (no desktop needed):

```bash
curl -sSL https://raw.githubusercontent.com/The-Carted-Horse/SugarCube/main/install.sh | bash
```

The installer handles everything: dependencies, config with random secrets,
console screen-blanking, and a systemd service that starts on boot. It
finishes by printing the URL and API secret for each person's uploader.

## Connecting the data

- **Trio (push)**: in Trio, Settings → Services → Nightscout, set URL
  `http://<pi-ip>:<port>` and the person's API secret (both shown by the
  installer and on the web settings page).
- **twiist**: the wearer links their My twiist Portal account to a free
  [Tidepool](https://www.tidepool.org) account (one-time), then enter the
  Tidepool login in the web settings under their data source.
- **Nightscout**: enter the site URL and its API secret or access token.
- **GlucoCore**: in GlucoCore, open **Devices**, add this display and choose
  who it shows; it gives you a six-digit code. Enter that under **Settings →
  GlucoCore** on the display (or during guided setup) and it pairs. The code
  lasts ten minutes and works once, and the display never handles the account
  password — it holds a token scoped to those people, revocable from
  GlucoCore. Pairing adds them to the display: anyone already fed by Trio,
  twiist or Nightscout keeps the source they have. Who appears, what they are
  called and their ranges then follow what GlucoCore says. Unpairing on the
  same page turns them back into uploader-fed people, each with their own
  port and API secret.

## The web app

Everything is served on plain HTTP port 80 — open `http://glucocube.local/`
(or the IP shown on the device's screen; HTTP Basic auth, login shown
there too). Every QR code the device puts on its screen carries the login
with it, so scanning one opens the page signed in — tap **SETTINGS** in
the footer of the display to get one for the settings page. The password
is printed under each code for anyone typing the address by hand.

The installer sets a random password, and **Access** can turn it off
again: on a home network you trust, the device is only reachable from
that network, and no password means nothing to look up on a phone. Then
the settings hub stops asking you to set one. On a network guests,
flatmates or an office share, keep it.

The `.local` name needs mDNS — iPhones, Macs, Windows, and Android 12+
all resolve it; on older Android type the IP instead. When port 80 isn't
available (e.g. running by hand on a dev machine) it falls back to 8080.

| Path | What |
|---|---|
| `/` | Live dashboard (auto-refreshes every 30s) |
| `/setup` | Guided setup, one question per screen |
| `/settings` | Everything else, one page per thing |
| `/log` | Sync activity from every data source |
| `/screen.png` | What the physical screen shows right now |
| `/api/dashboard.json` | The dashboard's data, as JSON |

Settings is a hub rather than one long form: each row says what it is set
to now — who is configured and when their last reading arrived, the
ranges, the network, the time zone, the version and channel — and opens a
short page for that one thing. Every person has their own page, with only
the fields their data source actually needs. Saving restarts the display
(a few seconds) and the page waits for it to come back rather than
guessing.

| Page | What is on it |
|---|---|
| `/settings/screen` | Live view of the physical screen, and Day/Night |
| `/settings/people` | One row per person, then a page each |
| `/settings/glucocore` | Pair this display with a GlucoCore code, or unpair it |
| `/settings/ranges` | In-range and urgent thresholds, staleness |
| `/settings/network` | Wi-Fi: what it is on, and what else is nearby |
| `/settings/clock` | Time zone (with what your phone thinks it is) |
| `/settings/updates` | Version, release channel, install |
| `/settings/access` | Password (or none at all), and a link that opens settings without logging in |

## Wi-Fi setup

A device with no network opens its own `GlucoCube-Setup` hotspot and shows
a QR code that joins a phone to it. Once the phone is on that hotspot the
setup page opens by itself — the device answers the connectivity check
every phone makes on joining a network, which is what makes the "sign in
to network" sheet appear. The page lists the networks the device saw
before the hotspot came up: tap one, or choose **Other network** for a
hidden or out-of-range one, and enter the password (with a Show button,
so you can check it before committing).

The hotspot drops while the device tries to connect, so the phone loses
that page; that is expected. If the join succeeds the device reboots, its
screen shows the new address, and reopening setup picks up where you left
off. If it fails, the hotspot comes back within a minute or two and **the
reason appears both on the device's own screen and at the top of the page**
(wrong password, network not found, and so on) — no SSH needed to find out
what went wrong.

### From GlucoCore's devices screen

A paired display collects the commands queued for it — on its realtime
channel when it has one, otherwise within the minute — and says what it
did with each, so the devices screen shows the outcome rather than a
silence:

| Command | On this display |
|---|---|
| Identify | Flashes a band across the screen for 30 seconds |
| Restart | Restarts the display; it comes back in a few seconds |
| Refresh now | Polls every pull source immediately |
| Clear cache | Drops stored readings and fetches again (therapy settings stay) |
| Check for updates | Runs the release check, and installs a forced release |

The heartbeat carries the version it is running and the config version it
has applied, which is what lets that screen tell a display that is behind
from one that is simply offline.

## Updates

### Upgrading from 1.x (SugarCube)

**Re-flash the card.** Version 2 renames the Python package, the service and
the paths, and 1.x's updater cannot install it: it looks for a package by the
old name. What happens when you press Install depends on how 1.x was put on:

- **From the ready-made image** — the update fails before anything is
  touched. The device carries on running 1.x, and simply never updates again.
- **From `install.sh` (a git checkout)** — do not press Install. The checkout
  succeeds, the service is left pointing at a module that no longer exists,
  and it restart-loops with no automatic way back. Recovering needs SSH.

Flashing a version 2 image is the supported route, and settings are entered
again through the setup wizard. To keep a 1.x device exactly as it is, leave
it alone — it will offer the update but cannot complete it.

The device checks GitHub for new [releases](https://github.com/The-Carted-Horse/SugarCube/releases)
every 6 hours. When one is available it shows up on the display footer, the
web dashboard, and the settings page — install it from **Settings →
Updates** (the display restarts, data is untouched). A release whose notes
contain `[force-update]` installs itself automatically at the device's next
check — use that for fixes every device should have.

### Release channels

**Settings → Updates** chooses which releases a device follows:

- **Standard** — full releases only, the ones `main` publishes. The default.
- **Beta** — the pre-releases `dev` publishes as well, for anyone happy to
  find the rough edges first.

Changing the channel installs that channel's newest release immediately,
rather than waiting for the next thing to be published. Leaving Beta
therefore steps *back* onto the last full release, which is the point: the
channel decides which releases the device runs, not just which ones it is
told about. A device that ends up on a pre-release with Standard selected
says so on the settings page, and offers the way back.

The channel is stored in `config.json`:

```json
{ "updates": { "channel": "beta" } }
```

### Cutting a release

Releases are cut by pushing, not by tagging:

- **Push to `dev`** — builds an image and publishes `vX.Y.Z-rc.N` as a
  GitHub *pre-release*. Devices on the Standard channel never see it;
  devices set to Beta are offered it at their next check, and the attached
  image can be flashed to try it. Each further push to `dev` bumps `N`.
- **Push to `main`** — builds an image and publishes `vX.Y.Z` as a full
  release. This is the one existing devices offer under Settings → Updates.
  It takes the version the `dev` rcs were rehearsing (`2.0.1-rc.3` →
  `2.0.1`), or bumps the patch number if `dev` never rehearsed one.

Both create their tag at the commit that was pushed, so there is no tag to
push by hand.

For a minor or major bump — or to run either channel off another branch —
run the workflow manually and pick the part to increment and the channel.

A push that changes nothing a device runs publishes nothing: prose, the
enclosure, the test suite and tool config are excluded on both channels,
because a release whose code is identical to the last one would still be
offered to every device on it. Everything else still cuts one, `install.sh`
and the systemd units included. To publish anyway, run the workflow by
hand — the exclusions apply to pushes only.

## Enclosure

[`enclosure/`](enclosure/) has a printable OpenSCAD enclosure for the 7"
display module — see `enclosure.scad` and the ready-to-slice STLs in
`enclosure/build/`. It was designed by Sarah Sabanis and is licensed
separately, under [CC BY-NC-SA 4.0](enclosure/LICENSE).

## Development

Runs on a Mac/PC in a window with fake data:

```bash
pip install pygame qrcode
python -m glucocube --demo --windowed
```

`--no-display` runs the servers headless — useful for working on the web UI,
since nothing it serves needs pygame. `--screenshot out.png` renders one
frame and exits.

If the touchscreen is mounted rotated relative to the panel, correct it
with `GLUCOCUBE_TOUCH_TRANSFORM` — a comma-separated list of `swap`,
`invx` and `invy` — in the systemd unit. `GLUCOCUBE_TOUCH=off` disables
reading the panel altogether. The runtime dependencies are `pygame` and
`qrcode`, plus `websocket-client` for GlucoCore's realtime channel — a
config change then reaches the display in seconds rather than at the next
poll, and without the package it long-polls instead. All three come from
apt on the Pi; everything else is the Python standard library.
The display and web app are typeset in
[Space Grotesk](https://github.com/floriankarsten/space-grotesk) and
[JetBrains Mono](https://github.com/JetBrains/JetBrainsMono), bundled under
the SIL Open Font License in `glucocube/fonts/`.

### Tests

The suite is standard `pytest`, and needs no hardware — the display is
rendered through SDL's dummy driver (the same one the shipped image uses),
NetworkManager and every outbound HTTP call are stubbed out, and the
end-to-end tests start `python -m glucocube` in a subprocess and talk to
it over sockets:

```bash
pip install -r requirements-dev.txt
pytest                       # the whole suite, about fifteen seconds
pytest tests/test_oref.py    # one module
pytest --cov=glucocube       # with coverage
ruff check glucocube tests   # the same lint CI runs
```

The `Tests` workflow runs all of it on Python 3.11, 3.12 and 3.13,
alongside `shellcheck` over `install.sh` and the image stage scripts and
`systemd-analyze verify` over the service units. The release build calls
that same workflow and waits for it, so nothing is published from a red
tree.

## Safety note

This is a convenience display, not a medical device. Forecasts are estimates
— even the pump-provided ones. Don't rely on it for alarms or treatment
decisions; use the CGM app's own alerts for that. The same, in operative
terms, is in [`LICENSE`](LICENSE), so that it travels with every copy.

## License

GlucoCube is free to use for personal and other noncommercial purposes,
under the [PolyForm Noncommercial License 1.0.0](LICENSE) — that covers
personal and hobby use, study and research, and use by charities, schools,
public health bodies and government, whatever their funding. Commercial use
needs written permission: open an issue to ask.

Three things in this repository are **not** under those terms:

- The **enclosure** is Sarah Sabanis's design, under
  [CC BY-NC-SA 4.0](enclosure/LICENSE).
- The **bundled fonts** in `glucocube/fonts/` stay solely under the SIL Open
  Font License 1.1. Your rights in them, commercial use included, are
  untouched by the license above — the OFL requires exactly that.
- The **SD card image** is Raspberry Pi OS, and every package in it keeps its
  own license. GlucoCube's terms reach only the GlucoCube layer.

The glucose forecast reimplements the exponential insulin-activity model
from [oref0](https://github.com/openaps/oref0) in Python. Raspberry Pi,
Nightscout, Trio, Tidepool, twiist, Loop and OpenAPS are their owners'
names, used here only to say what works with what.
