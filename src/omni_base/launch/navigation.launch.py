"""Autonomous navigation bringup: hardware + Nav2 (+ optional voice + web UI).

    ros2 launch omni_base navigation.launch.py
    ros2 launch omni_base navigation.launch.py use_voice:=true use_web:=true

Uses the installed map (maps/my_room_map.yaml) and Nav2 params
(config/nav2_params.yaml) from the package share dir.
"""
import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import (
    PythonLaunchDescriptionSource,
    AnyLaunchDescriptionSource,
)
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node


def generate_launch_description():
    omni_share = get_package_share_directory('omni_base')
    nav2_share = get_package_share_directory('nav2_bringup')
    rosbridge_share = get_package_share_directory('rosbridge_server')

    default_map = os.path.join(omni_share, 'maps', 'my_room_map.yaml')
    nav2_params = os.path.join(omni_share, 'config', 'nav2_params.yaml')

    lidar_port   = LaunchConfiguration('lidar_port')
    arduino_port = LaunchConfiguration('arduino_port')
    map_yaml     = LaunchConfiguration('map')
    params_file  = LaunchConfiguration('params_file')
    use_voice    = LaunchConfiguration('use_voice')
    use_web      = LaunchConfiguration('use_web')

    return LaunchDescription([
        DeclareLaunchArgument('lidar_port', default_value='/dev/rplidar',
                              description='Serial port for the RPLiDAR A1'),
        DeclareLaunchArgument('arduino_port', default_value='/dev/arduino',
                              description='Serial port for the Arduino Mega'),
        DeclareLaunchArgument('map', default_value=default_map,
                              description='Path to the map yaml to navigate on'),
        DeclareLaunchArgument('params_file', default_value=nav2_params,
                              description='Nav2 parameters file'),
        DeclareLaunchArgument('use_voice', default_value='false',
                              description='Start the voice control node'),
        DeclareLaunchArgument('use_web', default_value='false',
                              description='Start the rosbridge websocket for the web UI'),

        # Shared hardware (description + lidar + rf2o + arduino_bridge)
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(omni_share, 'launch', 'hardware.launch.py')),
            launch_arguments={
                'lidar_port': lidar_port,
                'arduino_port': arduino_port,
            }.items()
        ),

        # Nav2 stack (AMCL localization + planner/controller) on the saved map
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(nav2_share, 'launch', 'bringup_launch.py')),
            launch_arguments={
                'map': map_yaml,
                'params_file': params_file,
                'use_sim_time': 'false',
            }.items()
        ),

        # Optional voice control node
        Node(
            package='omni_base',
            executable='voice_node',
            name='voice_node',
            output='screen',
            condition=IfCondition(use_voice),
        ),

        # Optional rosbridge websocket for the web dashboard
        IncludeLaunchDescription(
            AnyLaunchDescriptionSource(
                os.path.join(rosbridge_share, 'launch', 'rosbridge_websocket_launch.xml')),
            condition=IfCondition(use_web),
        ),

        # Runtime saved locations (user-named points from the web UI).
        # Provides /save_location, /delete_location, and latched /saved_locations.
        # Started when voice or web is enabled so:
        #   - voice_node can resolve "go to abc" against user-saved points
        #   - web UI can persist points and see the live list
        Node(
            package='omni_base',
            executable='location_manager',
            name='location_manager',
            output='screen',
            condition=IfCondition(
                PythonExpression([
                    '"', use_voice, '" == "true" or "', use_web, '" == "true"'
                ])
            ),
        ),
    ])
