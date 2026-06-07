#!/bin/bash
#============================================================================
# flash_arduino.sh — Compile & upload the OmniServ firmware from the Pi
#
# Flashes firmware/omniserv_firmware over the existing USB cable using
# arduino-cli. No PC and no Arduino IDE required.
#
# A serial port has a single owner, so this script stops whatever is holding
# /dev/arduino (the arduino_bridge node) before flashing, then restarts it.
#
# Usage:
#   ./scripts/flash_arduino.sh              # compile + upload to /dev/arduino
#   ./scripts/flash_arduino.sh --port /dev/ttyACM0
#   ./scripts/flash_arduino.sh --compile-only
#============================================================================

set -euo pipefail

# ─── Config ────────────────────────────────────────────────────────────────
PORT="/dev/arduino"
FQBN="arduino:avr:mega"
SERVICE="omniserv.service"
COMPILE_ONLY=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_DIR="$(dirname "$SCRIPT_DIR")"
SKETCH_DIR="$WS_DIR/firmware/omniserv_firmware"

# ─── Parse args ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)         PORT="$2"; shift 2 ;;
    --fqbn)         FQBN="$2"; shift 2 ;;
    --compile-only) COMPILE_ONLY=1; shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^#//'
      exit 0 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# ─── Sanity checks ───────────────────────────────────────────────────────────
if ! command -v arduino-cli >/dev/null 2>&1; then
  echo "ERROR: arduino-cli not found. Install it (see ENVIRONMENT_SETUP.md):"
  echo "  curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh"
  echo "  arduino-cli core update-index && arduino-cli core install arduino:avr"
  exit 1
fi

if [[ ! -f "$SKETCH_DIR/omniserv_firmware.ino" ]]; then
  echo "ERROR: sketch not found at $SKETCH_DIR/omniserv_firmware.ino"
  exit 1
fi

# ─── 1. Compile ──────────────────────────────────────────────────────────────
echo ">> Compiling $SKETCH_DIR (fqbn=$FQBN)"
arduino-cli compile --fqbn "$FQBN" "$SKETCH_DIR"

if [[ "$COMPILE_ONLY" -eq 1 ]]; then
  echo ">> Compile-only requested. Done."
  exit 0
fi

# ─── 2. Free the serial port (stop the node holding it) ──────────────────────
STOPPED_SERVICE=0
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  echo ">> Stopping $SERVICE to free $PORT"
  sudo systemctl stop "$SERVICE"
  STOPPED_SERVICE=1
else
  # Fall back to killing a bare arduino_bridge process if it is running
  if pgrep -f "arduino_bridge" >/dev/null 2>&1; then
    echo ">> Stopping running arduino_bridge process to free $PORT"
    pkill -f "arduino_bridge" || true
    sleep 2
  fi
fi

# ─── 3. Upload ───────────────────────────────────────────────────────────────
echo ">> Uploading to $PORT"
arduino-cli upload -p "$PORT" --fqbn "$FQBN" "$SKETCH_DIR"
echo ">> Upload complete."

# ─── 4. Restart the node ─────────────────────────────────────────────────────
if [[ "$STOPPED_SERVICE" -eq 1 ]]; then
  echo ">> Restarting $SERVICE"
  sudo systemctl start "$SERVICE"
fi

echo ">> Done."
