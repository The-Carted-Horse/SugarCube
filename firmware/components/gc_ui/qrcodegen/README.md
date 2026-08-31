# qrcodegen

Project Nayuki's QR Code generator, vendored unmodified from
<https://github.com/nayuki/QR-Code-generator> (`c/qrcodegen.c`,
`c/qrcodegen.h`), under the MIT licence in `LICENSE.txt`.

Every QR code GlucoCube puts on a screen carries a login with it, so it is
read once, at an angle, across a room. `gc_ui` asks this for error
correction level M with a two-module quiet zone — `GC_QR_ERROR_CORRECTION`
and `GC_QR_BORDER_MODULES` in the contract — which is what the Raspberry Pi
asks `python-qrcode` for. The two products print the same code for the same
URL, which matters because the same phone scans both.

It is vendored rather than fetched because it is two files that have not
needed to change, and a release build should not depend on a third service
being up.
