import os
from glob import glob
from setuptools import find_packages, setup

package_name = 'omni_base'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        # Install package resources so launch files can locate them by share path
        # instead of hardcoded ~/ros2_ws absolute paths.
        (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py')),
        (os.path.join('share', package_name, 'config'), glob('config/*.yaml')),
        (os.path.join('share', package_name, 'urdf'),   glob('urdf/*.urdf')),
        (os.path.join('share', package_name, 'maps'),   glob('maps/*')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='dhanuhka',
    maintainer_email='dilshandhanushka836@gmail.com',
    description='OmniServ / Varys autonomous reception robot: Arduino bridge, voice control, and bringup launch files.',
    license='MIT',
    extras_require={
        'test': [
            'pytest',
        ],
    },
    entry_points={
        'console_scripts': [
            'arduino_bridge  = omni_base.arduino_bridge:main',
            'voice_node      = omni_base.voice_node:main',
            'location_manager = omni_base.location_manager:main',
        ],
    },
)
