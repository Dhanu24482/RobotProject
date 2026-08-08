"""Shared hardware bringup: robot description + LiDAR + laser odometry + Arduino bridge.

Included by both mapping.launch.py and navigation.launch.py. Mirrors the working
manual workflow: sllidar A1 on /dev/rplidar, rf2o with its defaults (publishes
/odom and the odom->base_link TF), and the arduino_bridge serial driver.
"""
import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    omni_share    = get_package_share_directory('omni_base')
    sllidar_share = get_package_share_directory('sllidar_ros2')
    rf2o_share    = get_package_share_directory('rf2o_laser_odometry')

    lidar_port   = LaunchConfiguration('lidar_port')
    arduino_port = LaunchConfiguration('arduino_port')
    wheel_separation = LaunchConfiguration('wheel_separation')
    max_speed        = LaunchConfiguration('max_speed')
    max_pwm          = LaunchConfiguration('max_pwm')
    min_pwm          = LaunchConfiguration('min_pwm')
    publish_sensors  = LaunchConfiguration('publish_sensors')

    return LaunchDescription([
        DeclareLaunchArgument('lidar_port', default_value='/dev/rplidar',
                              description='Serial port for the RPLiDAR A1'),
        DeclareLaunchArgument('arduino_port', default_value='/dev/arduino',
                              description='Serial port for the Arduino Mega'),
        DeclareLaunchArgument('wheel_separation', default_value='0.35',
                              description='Wheel separation in meters (for differential drive mixing)'),
        DeclareLaunchArgument('max_speed', default_value='0.26',
                              description='Wheel speed (m/s) that maps to max_pwm in arduino_bridge. '
                                          'Keep it equal to max_speed_xy in nav2_params.yaml'),
        DeclareLaunchArgument('max_pwm', default_value='30',
                              description='Maximum PWM magnitude sent to motors (clamped in bridge)'),
        DeclareLaunchArgument('min_pwm', default_value='20',
                              description='Minimum PWM magnitude for non-zero motor commands (deadband)'),
        DeclareLaunchArgument('publish_sensors', default_value='false',
                              description='Publish ultrasonic /pit telemetry. Off by default: the '
                                          'readings are unreliable indoors and Nav2 uses the LiDAR only'),

        # Robot description / TF tree
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(omni_share, 'launch', 'description.launch.py'))
        ),

        # RPLiDAR A1 -> /scan
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(sllidar_share, 'launch', 'sllidar_a1_launch.py')),
            launch_arguments={'serial_port': lidar_port}.items()
        ),

        # rf2o laser odometry -> /odom (+ odom->base_link TF), package defaults
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(rf2o_share, 'launch', 'rf2o_laser_odometry.launch.py'))
        ),

        # Arduino bridge (ROS <-> Mega serial)
        Node(
            package='omni_base',
            executable='arduino_bridge',
            name='arduino_bridge',
            output='screen',
            parameters=[{
                'serial_port': arduino_port,
                'wheel_separation': wheel_separation,
                'max_speed': max_speed,
                'max_pwm': max_pwm,
                'min_pwm': min_pwm,
                'publish_sensors': publish_sensors,
            }],
        ),

        # Dual ILI9341 TFT eye displays (Pi SPI0, CE0=left CE1=right)
        # Import-guarded: starts headless on non-Pi systems with no SPI hardware.
        Node(
            package='omni_base',
            executable='eyes_node',
            name='eyes_node',
            output='screen',
        ),
    ])
