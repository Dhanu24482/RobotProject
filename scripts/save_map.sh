#!/bin/bash
# Save the current slam_toolbox map to the omni_base package source maps/ dir.
#
# Usage:
#   ./scripts/save_map.sh [map_name]
#
# Default map_name is "my_room_map" (the map navigation.launch.py loads).
# Rebuild after saving so the installed share copy is updated:
#   colcon build --packages-select omni_base
set -e

MAP_NAME="${1:-my_room_map}"
WS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAPS_DIR="${WS_ROOT}/src/omni_base/maps"

source /opt/ros/humble/setup.bash
[ -f "${WS_ROOT}/install/setup.bash" ] && source "${WS_ROOT}/install/setup.bash"

mkdir -p "${MAPS_DIR}"
echo "Saving map to ${MAPS_DIR}/${MAP_NAME}.{pgm,yaml} ..."
ros2 run nav2_map_server map_saver_cli -f "${MAPS_DIR}/${MAP_NAME}"

echo "Done. Rebuild to install the updated map:"
echo "  colcon build --packages-select omni_base"
