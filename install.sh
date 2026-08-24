#!/usr/bin/env bash
# One-shot installer for GlucoCube on a Raspberry Pi.
#
# Run from a checkout:   ./install.sh
# Or with nothing yet:   curl -sSL https://raw.githubusercontent.com/The-Carted-Horse/GlucoCube/main/install.sh | bash
#
# Installs all dependencies, generates config.json with random API secrets,
# disables console screen blanking, and enables + starts the boot service.
set -euo pipefail

REPO_URL="${GLUCOCUBE_REPO:-https://github.com/The-Carted-Horse/GlucoCube.git}"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

log() { echo "==> $*"; }

# --- Locate (or fetch) the repo -------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd)"
if [ ! -f "$SCRIPT_DIR/glucocube/__main__.py" ]; then
    # Piped from curl or run outside a checkout: clone and continue from there.
    REPO_DIR="$HOME/GlucoCube"
    if [ ! -d "$REPO_DIR/.git" ]; then
        log "Cloning $REPO_URL to $REPO_DIR"
        command -v git >/dev/null || { $SUDO apt-get update; $SUDO apt-get install -y git; }
        git clone "$REPO_URL" "$REPO_DIR"
    fi
else
    REPO_DIR="$SCRIPT_DIR"
fi
cd "$REPO_DIR"
RUN_USER="${SUDO_USER:-$(whoami)}"

# --- Dependencies ----------------------------------------------------------

log "Installing dependencies (python3, pygame, qrcode)"
$SUDO apt-get update
$SUDO apt-get install -y python3 python3-pygame python3-qrcode

if ! python3 -c "import pygame" 2>/dev/null; then
    log "apt pygame unavailable; falling back to pip"
    $SUDO apt-get install -y python3-pip
    python3 -m pip install --user --break-system-packages pygame \
        || python3 -m pip install --user pygame
fi

# --- Config with auto-generated secrets ------------------------------------

if [ ! -f "$REPO_DIR/config.json" ]; then
    log "Creating config.json with random API secrets"
    python3 - "$REPO_DIR" <<'PYEOF'
import json, secrets, sys
from pathlib import Path

repo = Path(sys.argv[1])
config = json.loads((repo / "config.example.json").read_text())
for user in config["users"]:
    user["api_secret"] = secrets.token_hex(12)
# The web admin is exposed on port 80 — never leave it without a password.
alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
config["admin"] = {
    "port": 80,
    "password": "".join(secrets.choice(alphabet) for _ in range(6)),
}
(repo / "config.json").write_text(json.dumps(config, indent=2) + "\n")
PYEOF
else
    log "config.json already exists; keeping it"
fi

# --- Keep the screen awake (wall display) ----------------------------------

for CMDLINE in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [ -f "$CMDLINE" ]; then
        if ! grep -q "consoleblank=0" "$CMDLINE"; then
            log "Disabling console screen blanking in $CMDLINE"
            $SUDO sed -i '1 s/$/ consoleblank=0/' "$CMDLINE"
        fi
        break
    fi
done

# --- polkit: Wi-Fi provisioning + reboot as the service user ----------------

if [ -d /etc/polkit-1/rules.d ]; then
    log "Allowing $RUN_USER to manage Wi-Fi and reboot (polkit rule)"
    $SUDO tee /etc/polkit-1/rules.d/50-glucocube.rules > /dev/null <<POLKITEOF
polkit.addRule(function(action, subject) {
    if (subject.user != "$RUN_USER") {
        return polkit.Result.NOT_HANDLED;
    }
    if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0) {
        return polkit.Result.YES;
    }
    if (action.id == "org.freedesktop.login1.reboot" ||
        action.id == "org.freedesktop.login1.reboot-multiple-sessions") {
        return polkit.Result.YES;
    }
    return polkit.Result.NOT_HANDLED;
});
POLKITEOF
fi

# --- Captive portal for the setup hotspot ----------------------------------
# Resolves every name to the device while the hotspot is up, so a phone
# that joins opens the setup page by itself. NetworkManager applies this
# only to "shared" connections, so it does nothing on a normal network.

if [ -d /etc/NetworkManager ]; then
    log "Installing the setup-hotspot captive portal DNS rule"
    $SUDO install -d /etc/NetworkManager/dnsmasq-shared.d
    $SUDO tee /etc/NetworkManager/dnsmasq-shared.d/glucocube-captive.conf > /dev/null <<'CAPTIVEEOF'
# Captive portal for the GlucoCube setup hotspot; see the project README.
address=/#/10.42.0.1
CAPTIVEEOF
fi

# --- systemd service -------------------------------------------------------

log "Installing systemd service (user: $RUN_USER, path: $REPO_DIR)"
sed -e "s|^User=.*|User=$RUN_USER|" \
    -e "s|^Group=.*|Group=$RUN_USER|" \
    -e "s|/home/pi/GlucoCube|$REPO_DIR|g" \
    "$REPO_DIR/systemd/glucocube.service" \
    | $SUDO tee /etc/systemd/system/glucocube.service > /dev/null

$SUDO systemctl daemon-reload
$SUDO systemctl enable glucocube.service

if [ -d /dev/dri ]; then
    log "Starting glucocube"
    $SUDO systemctl restart glucocube.service || true
else
    log "No display hardware detected; service will start on next boot"
fi

# --- Summary ---------------------------------------------------------------

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "============================================================"
echo " GlucoCube installed."
echo
echo " Enter these in each Trio under Settings -> Services -> Nightscout:"
python3 - "$REPO_DIR" "${IP:-<pi-ip>}" <<'PYEOF'
import json, sys
from pathlib import Path

config = json.loads((Path(sys.argv[1]) / "config.json").read_text())
ip = sys.argv[2]
for user in config["users"]:
    print(f"   {user['name']}:")
    print(f"     URL:        http://{ip}:{user['port']}")
    print(f"     API secret: {user['api_secret']}")
PYEOF
echo
echo " Web dashboard + settings:  http://${IP:-<pi-ip>}/"
python3 - "$REPO_DIR" <<'PYEOF'
import json, sys
from pathlib import Path

admin = json.loads(
    (Path(sys.argv[1]) / "config.json").read_text()).get("admin", {})
if admin.get("password"):
    print(f"   login: admin / {admin['password']}")
PYEOF
echo
echo " Edit the names in $REPO_DIR/config.json, then:"
echo "   sudo systemctl restart glucocube"
echo
echo " Logs: journalctl -u glucocube -f"
echo " If screen blanking was just disabled, reboot once for it to apply."
echo "============================================================"
