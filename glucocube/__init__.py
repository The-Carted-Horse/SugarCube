"""GlucoCube — a two-person glucose dashboard for Raspberry Pi.

Receives Nightscout-style uploads from Trio (entries, treatments,
devicestatus) and renders both people's current glucose, IOB, and COB
full-screen via pygame/KMS with no desktop environment required.
"""

# Releases stamp _version.py (the image build and the self-updater both
# write it); a plain checkout falls back to the dev version below.
try:
    from ._version import __version__  # noqa: F401
except ImportError:
    __version__ = "1.0.0"
