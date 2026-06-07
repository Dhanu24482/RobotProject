"""Robot description: publishes the TF tree from the installed URDF.

Reads omniserv.urdf from the package share dir (installed via setup.py), so it
works after `colcon build` with no absolute paths.
"""
import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    pkg_share = get_package_share_directory('omni_base')
    urdf_file = os.path.join(pkg_share, 'urdf', 'omniserv.urdf')

    with open(urdf_file, 'r') as f:
        robot_desc = f.read()

    return LaunchDescription([
        Node(
            package='robot_state_publisher',
            executable='robot_state_publisher',
            name='robot_state_publisher',
            output='screen',
            parameters=[{
                'robot_description': robot_desc,
                'use_sim_time': False,
            }],
        ),
    ])
