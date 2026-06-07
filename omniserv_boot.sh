#!/bin/bash
# OmniServ mapping entrypoint (kept for backwards compatibility).
#
# This now delegates to the unified mapping launch file, which brings up the
# robot description, LiDAR, laser odometry, the Arduino bridge, and slam_toolbox.
# Drive with the BT remote to map, then save with scripts/save_map.sh.
#
# Equivalent to:
#   ros2 launch omni_base mapping.launch.py

source /opt/ros/humble/setup.bash
source /home/user/ros2_ws/install/setup.bash

ros2 launch omni_base mapping.launch.py "$@"
