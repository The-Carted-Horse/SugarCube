# GlucoCube firmware, for ESP32-S3

The same product as the Raspberry Pi image in this repository, on a board
that costs a fraction of one and boots in under a second. It shows the same
dashboard, applies the same thresholds, runs the same forecast, and is set up
the same way — by scanning what is on its own screen.

It is a **peer of the Pi, not a satellite of one**: it fetches each person's
glucose from GlucoCore, Nightscout or Tidepool itself, over TLS, and needs no
Pi anywhere on the network.

## Boards

| Profile | Board | Panel | Silicon |
|---|---|---|---|
| `esp32-8048s050` | Sunton ESP32-8048S050 (5.0″) | 800×480 ST7262 RGB565, GT911 touch | ESP32-S3-WROOM-1-N16R8 |
| `sugarcube-s3` | SugarCube ESP32-S3 | 800×480 ST7262 RGB565, GT911 touch | ESP32-S3-WROOM-1-N16R8 |

Both are 16 MB flash and 8 MB octal PSRAM, and both draw at the Pi's own
800×480 — which is why the layout ports across exactly rather than
approximately.

> **The pin maps need checking against your board before its first run.**
> `boards/esp32-8048s050/board.h` carries the map the Sunton schematic and the
> community board definitions agree on, but Sunton has shipped revisions of
> neighbouring boards with the touch controller moved between I²C pairs.
> `boards/sugarcube-s3/board.h` is explicitly a **placeholder** copied from the
> Sunton profile so the profile builds while the hardware is in fabrication;
> it logs a warning at boot until the real numbers replace it.
>
> If the panel lights but shows noise, the RGB pins are wrong. If the picture
> is right but taps do nothing, the `GC_TOUCH_*` pins are. Nothing else in the
> firmware needs changing — the board header is the whole hardware surface.

Adding a board is a header in `boards/` and an entry in
`ESP32_BOARDS` in [`glucocube/contract.py`](../glucocube/contract.py). A
profile in one but not the other fails `tests/test_contract.py`, and a
profile the release workflow does not list stops being published — which is
why the matrix in `.github/workflows/` names them explicitly.

## Building

```bash
. $IDF_PATH/export.sh                 # ESP-IDF v5.3.2
cd firmware
idf.py -DGC_BOARD=esp32-8048s050 build
idf.py -DGC_BOARD=esp32-8048s050 -p /dev/ttyACM0 flash monitor
```

LVGL is fetched from git rather than the component registry (see
`main/idf_component.yml`) so a release build depends on one service fewer.
The QR encoder is vendored in `components/gc_ui/qrcodegen/`, for the same
reason: two files that have not needed to change.

## Flashing a finished release

Either from a browser at [`docs/flash/`](../docs/flash/) — Chrome, Edge or
Opera on a desktop, nothing to install — or with `esptool`:

```bash
esptool.py --chip esp32s3 write_flash 0x0 \
    glucocube-esp32-8048s050-<version>-factory.bin
```

Every release carries, per board, an OTA image
(`glucocube-<board>-<version>.bin`, what a device in the field writes into its
spare slot) and a factory image (`…-factory.bin`, bootloader and partition
table included, for a board being flashed for the first time).

## How it is put together

| Component | What it is |
|---|---|
| `gc_contract` | **Generated** from `glucocube/contract.py`. Never edit it — edit the Python and run `python3 firmware/tools/gen_contract.py`. |
| `gc_board` | The only code that knows a GPIO number: RGB panel, backlight, GT911. |
| `gc_store` | Ring buffers in PSRAM standing in for the Pi's SQLite, with `store.py`'s snapshot rules. |
| `gc_oref`, `gc_predict` | The forecast, ported from `oref.py` and `predict.py`. |
| `gc_sources` | GlucoCore, Nightscout and Tidepool clients, and GlucoCore pairing. |
| `gc_config` | The settings, in NVS, validated before they are written. |
| `gc_net` | Wi-Fi, the `GlucoCube-Setup` hotspot, and the clock. |
| `gc_httpd` | The device's own dashboard, wizard and settings, plus the captive-portal answers. |
| `gc_ota` | Self-update from the same GitHub releases the Pi image uses. |
| `gc_ui` | The dashboard, drawn. |

### Where the memory goes

An 800×480 RGB565 frame is 750 KB, and there are two of them so a frame is
never scanned out half-drawn — 1.5 MB of the 8 MB of PSRAM, before anything
else. The store is about 25 KB a person. The four bundled typefaces are
embedded in the binary (≈770 KB of the 6 MB slot) and rasterised at runtime,
because the type sizes come from the panel's height and the number of people
sharing it, so a pre-generated bitmap font would have to guess both.

TLS is the largest live consumer after the framebuffers: mbedTLS's handshake
buffers are configured to be freed between requests, which is what keeps four
pollers, an HTTP server and an OTA download from colliding.

## Is it really the same dashboard?

Every number that decides what a person sees — the palette, the layout
fractions, the thresholds, the forecast model's coefficients, the trend
arrows, the chart's window and its confidence cone — lives in
[`glucocube/contract.py`](../glucocube/contract.py) and is generated into
`gc_contract.h`. Neither product carries its own copy.

The forecast is the part where "close enough" would be wrong, so it is
pinned rather than trusted:

```bash
make -C firmware/host_test        # builds the C with a host compiler and runs it
```

One wrinkle worth knowing about: Python 3.12 changed `sum()` over floats to
compensated summation, and `oref.predict` sums in four places, so the Pi's
own forecast differs in its last digit depending on which Python it runs —
about 1e-14 mg/dL. Nothing a display could show, but enough that a
byte-for-byte golden file is impossible, so the vectors are quantised to
1e-6 and compared to 1e-2. CI runs the generator on 3.11, 3.12 and 3.13,
which is what catches it if that ever stops being true.

`host_test/gen_vectors.py` runs `glucocube/oref.py` and `predict.py` over a
spread of deliberately awkward inputs — pump IOB with no visible boluses,
negative IOB, carbs on board, readings clamping at both ends, an implausible
profile, a peak sitting exactly at half the insulin duration — and writes the
answers out as C arrays. `host_test/test_parity.c` runs the firmware's own
code over the same inputs and compares, to a hundredth of a mg/dL. The whole
thing runs inside `pytest` too, so it gates every push.

## What the Pi does that this does not

The list is kept in `tests/test_contract.py` rather than only here —
`PI_ONLY_ROUTES` names every path the Pi answers and this does not, with
the reason, and a test fails if one of them quietly starts working. Closing
a gap means deleting a line from a test.

The ones worth knowing about:

- **Ambient mode** — one person at a time over a photograph, with the
  wallpapers and the weather that go with it. The firmware draws the split
  layout, which is what a device upgrading from an earlier version keeps.
- **`/screen.png`** — the settings page's live view of the panel. Encoding
  750 KB of RGB565 to PNG every few seconds costs more than the page is
  worth on this hardware, so it answers 503 and says so.
- **Push sources** — a Trio instance uploading straight to the device. The
  Pi opens a Nightscout-compatible listener per person; here every source is
  pulled.
- **Scan-to-pair** — an unpaired display showing a request as a QR code for
  a signed-in phone to approve. Pairing here is by six-digit code. The
  request and collect calls are implemented (`gc_glucocore_request_pairing`,
  `gc_glucocore_collect_pairing`); what is missing is the screen and the
  waiter that drive them.
- **GlucoCore's command queue** — the five buttons on its devices screen
  (identify, restart, refresh, clear cache, check for updates).

## Safety note

This is a convenience display, not a medical device. Forecasts are estimates
— even the pump-provided ones. Don't rely on it for alarms or treatment
decisions; use the CGM app's own alerts for that.
