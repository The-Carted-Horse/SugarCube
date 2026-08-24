#!/bin/bash -e
# Copy the application into the image. pi-gen only mounts the stage
# directory into its build container, so the workflow copies the
# glucocube package into files/ before the build starts.
FILES="${STAGE_DIR}/00-glucocube/files"

install -d "${ROOTFS_DIR}/opt/glucocube"
cp -r "${FILES}/glucocube" "${ROOTFS_DIR}/opt/glucocube/"
find "${ROOTFS_DIR}/opt/glucocube" -name __pycache__ -type d -exec rm -rf {} + || true

install -m 644 "${FILES}/glucocube.service" \
	"${ROOTFS_DIR}/etc/systemd/system/glucocube.service"

install -D -m 644 "${FILES}/50-glucocube.rules" \
	"${ROOTFS_DIR}/etc/polkit-1/rules.d/50-glucocube.rules"

# Captive portal: resolve every name to the device while the setup
# hotspot is up, so a phone that joins opens the setup page by itself.
install -D -m 644 "${FILES}/glucocube-captive.conf" \
	"${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d/glucocube-captive.conf"
