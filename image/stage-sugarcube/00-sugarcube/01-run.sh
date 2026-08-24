#!/bin/bash -e
# Copy the application into the image. pi-gen only mounts the stage
# directory into its build container, so the workflow copies the
# sugarcube package into files/ before the build starts.
FILES="${STAGE_DIR}/00-sugarcube/files"

install -d "${ROOTFS_DIR}/opt/sugarcube"
cp -r "${FILES}/sugarcube" "${ROOTFS_DIR}/opt/sugarcube/"
find "${ROOTFS_DIR}/opt/sugarcube" -name __pycache__ -type d -exec rm -rf {} + || true

install -m 644 "${FILES}/sugarcube.service" \
	"${ROOTFS_DIR}/etc/systemd/system/sugarcube.service"

install -D -m 644 "${FILES}/50-sugarcube.rules" \
	"${ROOTFS_DIR}/etc/polkit-1/rules.d/50-sugarcube.rules"

# Captive portal: resolve every name to the device while the setup
# hotspot is up, so a phone that joins opens the setup page by itself.
install -D -m 644 "${FILES}/sugarcube-captive.conf" \
	"${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d/sugarcube-captive.conf"
