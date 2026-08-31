# The web installer

`index.html` writes GlucoCube firmware to an ESP32-S3 over USB, from a
browser, using [ESP Web Tools](https://esphome.github.io/esp-web-tools/).
Nothing to download and nothing to install first.

It reads the current release from the GitHub API, then flashes the factory
image named in that release's `manifest-<board>.json` — both of which the
`Build and release` workflow publishes. Adding a board profile to
`glucocube.contract.ESP32_BOARDS` and to the workflow's matrix is enough;
this page picks it up, and `tests/test_contract.py` fails if it does not.

**This page needs GitHub Pages turned on to be reachable.** In the
repository's Settings → Pages, set the source to the `main` branch and the
`/docs` folder. It then serves from

    https://the-carted-horse.github.io/SugarCube/flash/

which is the URL the README links to. Serving over HTTPS is not optional:
a browser will not hand a page a serial port otherwise.
