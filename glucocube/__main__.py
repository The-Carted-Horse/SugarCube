"""Entry point: python -m glucocube [--config PATH] [--windowed] [--demo]"""

import argparse
import logging
import math
import os
import random
import sys
import time
from pathlib import Path

from . import config as config_mod
from .network import NetworkWatcher
from .server import start_servers, stop_servers
from .sources import start_pollers
from .store import Store


def seed_demo_data(store: Store, users) -> None:
    """Fill the store with plausible fake data so the layout can be previewed."""
    now = int(time.time() * 1000)
    rng = random.Random(42)
    for i, user in enumerate(users):
        base = 120 + i * 45
        entries = []
        for m in range(180, -1, -5):
            wobble = 35 * math.sin((m + i * 40) / 38) + rng.uniform(-6, 6)
            entries.append(
                {
                    "type": "sgv",
                    "sgv": round(base + wobble),
                    "date": now - m * 60 * 1000,
                    "direction": ["Flat", "FortyFiveUp", "SingleDown"][i % 3],
                    "device": "demo",
                }
            )
        store.add_entries(user.name, entries)
        store.add_treatments(
            user.name,
            [
                {"eventType": "Carb Correction", "carbs": 24 + i * 10,
                 "created_at": now - (47 + i * 20) * 60 * 1000},
                {"eventType": "Bolus", "insulin": 2.5 + i,
                 "created_at": now - (31 + i * 15) * 60 * 1000},
            ],
        )
        # First demo user gets a device forecast curve (like Trio uploads);
        # the second exercises the fallback estimator instead.
        last_sgv = entries[-1]["sgv"]
        suggested = {"COB": 12 + i * 6, "IOB": 1.4 + i * 0.8}
        if i == 0:
            suggested["timestamp"] = now - 4 * 60 * 1000
            suggested["predBGs"] = {
                "COB": [
                    round(last_sgv + 40 * math.sin(step / 7) + step)
                    for step in range(30)
                ]
            }
        store.add_devicestatus(
            user.name,
            [{
                "created_at": now - 4 * 60 * 1000,
                "openaps": {"iob": {"iob": 1.4 + i * 0.8}, "suggested": suggested},
            }],
        )


def wait_for_connected_display(timeout: float = 45.0) -> bool:
    """Block until a DRM connector reports 'connected' (or timeout)."""
    import glob

    deadline = time.time() + timeout
    while time.time() < deadline:
        for status_path in glob.glob("/sys/class/drm/card*-*/status"):
            try:
                if open(status_path).read().strip() == "connected":
                    return True
            except OSError:
                continue
        time.sleep(1)
    logging.warning("No connected display after %.0fs; trying anyway", timeout)
    return False


# Long enough to walk into the other room and look at the wall.
IDENTIFY_SECONDS = 30


def command_actions(config, store, pollers) -> dict:
    """What GlucoCore's buttons do to this display.

    Built here because this is where the running pieces are — the pollers
    to poke, the store to clear, the process to restart. Each returns the
    line that ends up on the devices screen beside the command, so "done"
    is never the whole answer.
    """
    from .webadmin import restart_soon

    def identify() -> str:
        store.replace_params(
            config_mod.IDENTIFY_KEY,
            {"until": int(time.time() * 1000) + IDENTIFY_SECONDS * 1000})
        return f"flashing for {IDENTIFY_SECONDS} seconds"

    def restart() -> str:
        # Acknowledged first, then gone: the delay is what lets the
        # acknowledgement leave before the process does.
        restart_soon(2.0)
        return "restarting"

    def refresh() -> str:
        if not pollers:
            # Everyone here is fed by an uploader — there is nothing to
            # fetch, and saying so beats a tick that means nothing.
            return "nothing to fetch: every person here is fed by an uploader"
        for poller in pollers:
            poller.poke()
        return (f"polling {len(pollers)} source"
                f"{'s' if len(pollers) != 1 else ''} now")

    def clear_cache() -> str:
        removed = store.clear_readings([user.name for user in config.users])
        for poller in pollers:
            poller.poke()
        return f"dropped {removed} stored rows; fetching again"

    def check_update() -> str:
        from .updater import check_and_maybe_force
        state = check_and_maybe_force(store, config.update_channel)
        if state.get("forcing"):
            return f"installing {state.get('latest', '')}"
        if state.get("available"):
            return f"{state.get('latest', '')} is available"
        channel = config_mod.CHANNEL_LABELS[
            config_mod.normalize_channel(config.update_channel)]
        return f"up to date on the {channel.lower()} channel"

    return {"identify": identify, "restart": restart, "refresh": refresh,
            "clear_cache": clear_cache, "check_update": check_update}


def main() -> int:
    parser = argparse.ArgumentParser(prog="glucocube")
    parser.add_argument(
        "--config", default=None,
        help="path to config.json (default: config.json next to the package)",
    )
    parser.add_argument("--windowed", action="store_true",
                        help="run in a window instead of fullscreen (for development)")
    parser.add_argument("--demo", action="store_true",
                        help="use an in-memory database seeded with fake data")
    parser.add_argument("--no-display", action="store_true",
                        help="run only the HTTP servers (for testing)")
    parser.add_argument("--screenshot", metavar="PATH",
                        help="render one frame to PATH and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = args.config or Path(__file__).resolve().parent.parent / "config.json"
    if not Path(config_path).exists():
        logging.info("No config at %s — creating a starter config; the display "
                     "will show a setup QR code.", config_path)
        config_mod.create_default(config_path)
    config = config_mod.load(config_path)
    # Before anything renders a clock: a fresh image has no time zone set,
    # so without this every timestamp on the wall reads UTC.
    config_mod.apply_timezone(config.display.timezone)

    store = Store(":memory:" if args.demo else config.database)
    if args.demo:
        seed_demo_data(store, config.users)

    servers = start_servers(config.users, store)
    gc_token = config.glucocore.device_token if config.glucocore else ""
    pollers = start_pollers(config.users, store, glucocore_token=gc_token)

    push_listener = None
    if config.glucocore and config.glucocore.device_token:
        from .push import start_push_listener
        from .webadmin import restart_soon

        def _on_remote_config(_new_config):
            restart_soon()

        push_listener = start_push_listener(
            config_path,
            {
                "device_id": config.glucocore.device_id,
                "device_token": config.glucocore.device_token,
                "hardware_id": config.glucocore.hardware_id,
            },
            store,
            _on_remote_config,
            actions=command_actions(config, store, pollers),
        )

    from .webadmin import start_admin
    start_admin(config, config_path, store)

    # Release checks (and forced-release self-installs) run only under
    # systemd — a dev checkout must never have its working tree replaced
    # underneath the developer. The settings page's "Check now" still
    # works everywhere.
    if (os.environ.get("INVOCATION_ID")
            and not args.demo and not args.screenshot):
        from .updater import UpdateChecker, mark_boot_ok_later
        mark_boot_ok_later()
        # The live config, not a copy of the channel: the settings page
        # edits it in place when someone switches channel.
        UpdateChecker(store, config).start()

    # Wi-Fi provisioning: with no network at all, a setup hotspot comes up
    # and the screen switches to a join-QR. Password persists across boots.
    import secrets as secrets_mod
    from . import network as network_mod
    network_mod.init(store)   # scan cache + join results live in the store
    net_params = store.get_params("__network")
    if not net_params.get("hotspot_password"):
        net_params["hotspot_password"] = secrets_mod.token_hex(4)
        store.set_params("__network", net_params)
    watcher = NetworkWatcher(net_params["hotspot_password"])
    watcher.start()
    try:
        if args.no_display:
            print("Servers running; Ctrl-C to stop.")
            while True:
                time.sleep(3600)
        else:
            if os.environ.get("GLUCOCUBE_DISPLAY") == "fbdev":
                # Render into a dummy SDL surface; frames go to /dev/fb0.
                # Force dummy: a unit-level SDL_VIDEODRIVER=kmsdrm would
                # otherwise make SDL seize the display with a blank buffer
                # while our fb0 frames render into the void behind it.
                os.environ["SDL_VIDEODRIVER"] = "dummy"
            elif (
                # Default SDL to kmsdrm on Linux consoles (no desktop session).
                sys.platform.startswith("linux")
                and "SDL_VIDEODRIVER" not in os.environ
                and not os.environ.get("DISPLAY")
                and not os.environ.get("WAYLAND_DISPLAY")
            ):
                os.environ["SDL_VIDEODRIVER"] = "kmsdrm"
                # Early in boot the panel may still be probing; grabbing DRM
                # before a connector is up can leave the screen undriven.
                wait_for_connected_display()
            from .display import Display

            display = Display(config, store, windowed=args.windowed)
            if args.screenshot:
                display.screenshot(args.screenshot)
            else:
                display.run()
    except KeyboardInterrupt:
        pass
    finally:
        if push_listener:
            push_listener.stop()
        stop_servers(servers)
        for poller in pollers:
            poller.stop()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
