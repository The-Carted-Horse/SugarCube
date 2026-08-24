# SugarCube

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
- **Touchscreen light/dark mode** — tap the sun/moon on the display.
- **Per-person thresholds** — low/high/urgent ranges per person, with
  global defaults.
- **QR-code onboarding** — a fresh device shows a QR code that takes a phone
  to the setup page; with no network at all it opens its own setup hotspot
  first so you can connect the Pi to Wi-Fi from your phone. Once a network
  is chosen the device reboots onto it and shows its new address (also
  reachable as `http://sugarcube.local/` on the ready-made image).

## Install

### Option A: flash the ready-made image

Grab `sugarcube-<version>.img.xz` from the
[releases page](https://github.com/The-Carted-Horse/SugarCube/releases),
flash it with Raspberry Pi Imager (or `dd`), boot the Pi, and follow the
QR codes on screen. That's the whole install.

(Images are built by the `Build SD card image` GitHub Actions workflow —
push a `v*` tag or run it manually.)

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

## The web app

Everything is served on plain HTTP port 80 — open `http://sugarcube.local/`
(or the IP shown on the device's screen; HTTP Basic auth, login shown
there too). The `.local` name needs mDNS — iPhones, Macs, Windows, and
Android 12+ all resolve it; on older Android type the IP instead. When
port 80 isn't available (e.g. running by hand on a dev machine) it falls
back to 8080.

| Path | What |
|---|---|
| `/` | Live dashboard (auto-refreshes every 30s) |
| `/settings` | People, data sources, thresholds, Wi-Fi |
| `/log` | Sync activity from every data source |
| `/screen.png` | What the physical screen shows right now |
| `/api/dashboard.json` | The dashboard's data, as JSON |

## Wi-Fi setup

A device with no network opens its own `SugarCube-Setup` hotspot and shows
a QR code that joins a phone to it. From there the settings page lists the
networks it saw before the hotspot came up — pick one, or type a name for
a hidden or missing network — and enter the password.

The hotspot drops while the device tries to connect, so the phone loses
that page; that is expected. If the join succeeds the device reboots and
its screen shows the new address. If it fails, the hotspot comes back
within a minute or two and **the reason appears both on the device's own
screen and at the top of the settings page** (wrong password, network not
found, and so on) — no SSH needed to find out what went wrong.

## Updates

The device checks GitHub for new [releases](https://github.com/The-Carted-Horse/SugarCube/releases)
every 6 hours. When one is available it shows up on the display footer, the
web dashboard, and the settings page — install it from **Settings →
Updates** (the display restarts, data is untouched). A release whose notes
contain `[force-update]` installs itself automatically at the device's next
check — use that for fixes every device should have.

Cutting a release: push a `v*` tag (or run the `Build SD card image`
workflow). The tag both builds the flashable image and becomes the update
that existing devices see.

## Enclosure

[`enclosure/`](enclosure/) has a printable OpenSCAD enclosure for the 7"
display module — see `enclosure.scad` and the ready-to-slice STLs in
`enclosure/build/`.

## Development

Runs on a Mac/PC in a window with fake data:

```bash
pip install pygame qrcode
python -m sugarcube --demo --windowed
```

`--no-display` runs the servers headless; `--screenshot out.png` renders one
frame and exits. The only runtime dependencies are `pygame` and `qrcode`
(both from apt on the Pi); everything else is the Python standard library.
The display and web app are typeset in
[Space Grotesk](https://github.com/floriankarsten/space-grotesk) and
[JetBrains Mono](https://github.com/JetBrains/JetBrainsMono), bundled under
the SIL Open Font License in `sugarcube/fonts/`.

## Safety note

This is a convenience display, not a medical device. Forecasts are estimates
— even the pump-provided ones. Don't rely on it for alarms or treatment
decisions; use the CGM app's own alerts for that.
