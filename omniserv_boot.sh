#!/bin/bash

# 1. Source the ROS 2 environments
source /opt/ros/humble/setup.bash
source /home/user/ros2_ws/install/setup.bash

# 2. Launch the Hardware
ros2 launch sllidar_ros2 sllidar_a1_launch.py serial_port:=/dev/rplidar &
ros2 run omni_base arduino_bridge &

# 3. Launch the Robot's Body (URDF)
ros2 run robot_state_publisher robot_state_publisher ~/ros2_ws/omniserv.urdf &

# 4. Launch Odometry (Redirected to the Judge)
ros2 launch rf2o_laser_odometry rf2o_laser_odometry.launch.py laser_scan_topic:=/scan odom_topic:=/laser/odom publish_tf:=false &

# 4.5 Launch the Judge (EKF)
# ros2 run robot_localization ekf_node --ros-args --params-file /home/user/ros2_ws/src/omni_base/config/ekf.yaml &

# 5. Launch the Brain (SLAM)
ros2 run slam_toolbox async_slam_toolbox_node --ros-args -p odom_frame:=odom -p base_frame:=base_link -p map_frame:=map -p scan_topic:=/scan &

wait
