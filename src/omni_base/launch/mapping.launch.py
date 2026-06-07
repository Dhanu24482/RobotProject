"""Mapping bringup: hardware + slam_toolbox (async).

Drive the robot with the BT remote to build the map, then save it with
`scripts/save_map.sh` (or `ros2 run nav2_map_server map_saver_cli`).

    ros2 launch omni_base mapping.launch.py
"""
import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    omni_share = get_package_share_directory('omni_base')
    slam_params = os.path.join(omni_share, 'config', 'slam_params.yaml')

    lidar_port   = LaunchConfiguration('lidar_port')
    arduino_port = LaunchConfiguration('arduino_port')

    return LaunchDescription([
        DeclareLaunchArgument('lidar_port', default_value='/dev/rplidar',
                              description='Serial port for the RPLiDAR A1'),
        DeclareLaunchArgument('arduino_port', default_value='/dev/arduino',
                              description='Serial port for the Arduino Mega'),

        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(omni_share, 'launch', 'hardware.launch.py')),
            launch_arguments={
                'lidar_port': lidar_port,
                'arduino_port': arduino_port,
            }.items()
        ),

        Node(
            package='slam_toolbox',
            executable='async_slam_toolbox_node',
            name='slam_toolbox',
            output='screen',
            parameters=[slam_params],
        ),
    ])
