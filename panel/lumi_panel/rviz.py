"""Remote RViz2 over a Cloudflare Tunnel.

DDS (what ROS 2 speaks) needs UDP multicast + random UDP ports, which a tunnel cannot carry.
Zenoh can: `zenoh-bridge-ros2dds` runs on both ends and tunnels the DDS traffic over ONE TCP
connection, which `cloudflared access tcp` can forward.

    Pi:      zenoh-bridge-ros2dds (listens on 127.0.0.1:7447 and [::1]:7447) <- tunnel public hostname tcp://127.0.0.1:7447
    Remote:  cloudflared access tcp -> 127.0.0.1:7447 <- zenoh-bridge-ros2dds client <- rviz2

The bridge is a transparent pipe: it filters, renames and throttles nothing (see bridge_config()).
"""
import json
import os
import platform
import shutil
import stat
import tempfile
import urllib.request
import zipfile

from . import config

BRIDGE_VERSION = '1.10.2'
BRIDGE_PORT = 7447
_ASSET = 'zenoh-plugin-ros2dds-{v}-{arch}-unknown-linux-gnu-standalone.zip'
_URL = 'https://github.com/eclipse-zenoh/zenoh-plugin-ros2dds/releases/download/{v}/' + _ASSET
_ARCH = {'aarch64': 'aarch64', 'arm64': 'aarch64', 'x86_64': 'x86_64', 'amd64': 'x86_64'}

def ros_distro():
    """The robot's ROS distro, from the setup file the panel sources (e.g. /opt/ros/humble/setup.bash)."""
    import re
    m = re.search(r'/opt/ros/([a-z]+)/', config.ROS_SETUP)
    return m.group(1) if m else 'humble'


def bridge_binary():
    for d in (os.path.join(config.HOME, '.local', 'bin'), '/usr/local/bin', '/usr/bin'):
        p = os.path.join(d, 'zenoh-bridge-ros2dds')
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def install_bridge():
    """Download the official standalone bridge for this CPU into ~/.local/bin."""
    arch = _ARCH.get(platform.machine().lower())
    if not arch:
        raise RuntimeError(f'No prebuilt bridge for CPU {platform.machine()!r}')
    url = _URL.format(v=BRIDGE_VERSION, arch=arch)
    dest_dir = os.path.join(config.HOME, '.local', 'bin')
    os.makedirs(dest_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        zpath = os.path.join(tmp, 'bridge.zip')
        with urllib.request.urlopen(url, timeout=120) as r, open(zpath, 'wb') as f:
            shutil.copyfileobj(r, f)
        with zipfile.ZipFile(zpath) as z:
            if 'zenoh-bridge-ros2dds' not in z.namelist():
                raise RuntimeError('unexpected archive layout')
            z.extract('zenoh-bridge-ros2dds', tmp)
        exe = os.path.join(tmp, 'zenoh-bridge-ros2dds')
        if os.path.getsize(exe) < 1_000_000:
            raise RuntimeError('downloaded file is too small to be the bridge')
        os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR)
        final = os.path.join(dest_dir, 'zenoh-bridge-ros2dds')
        shutil.move(exe, final + '.new')
        os.replace(final + '.new', final)
    return f'Installed zenoh-bridge-ros2dds {BRIDGE_VERSION} to {final}'


def _loopback_endpoints():
    """cloudflared dials `localhost`, which on many systems resolves to ::1 first. A bridge that only
    listens on 127.0.0.1 then answers "connection refused" and the remote side sees a bare
    'websocket: bad handshake'. So listen on both loopbacks whenever IPv6 loopback exists."""
    eps = [f'tcp/127.0.0.1:{BRIDGE_PORT}']
    try:
        with open('/proc/sys/net/ipv6/conf/lo/disable_ipv6') as f:
            if f.read().strip() == '0':
                eps.append(f'tcp/[::1]:{BRIDGE_PORT}')
    except OSError:
        pass
    return eps


def bridge_config(settings, env):
    """A transparent pipe: no allow-list, no deny-list, no rate limits, no access rules. Whatever exists on
    the robot's ROS graph (any topic, service or action, present or added later, under any name) is bridged
    both ways. Who may use the pipe is decided in front of it (Cloudflare Access), not here."""
    return {
        'mode': 'router',
        'listen': {'endpoints': _loopback_endpoints()},
        'scouting': {'multicast': {'enabled': False}},
        'plugins': {'ros2dds': {'domain': int(env.get('ROS_DOMAIN_ID', '0') or 0)}},
    }


def write_bridge_config(settings, env):
    path = os.path.join(config.STATE_DIR, 'zenoh-bridge.json5')
    config.atomic_write(path, json.dumps(bridge_config(settings, env), indent=2))
    return path


def connect_script(settings, env, host=None, domain=None):
    """The client installer/launcher for the remote machine (templates/lumi-rviz.sh), built at request time:
    `host` and `domain` override the saved values, so the panel's input fields can be used without saving."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'lumi-rviz.sh')) as f:
        tpl = f.read()
    host = host or settings['rviz']['hostname'] or 'zenoh.example.com'
    domain = settings['rviz'].get('domain', 0) if domain is None else domain
    return (tpl.replace('__HOST__', host).replace('__PORT__', str(BRIDGE_PORT))
            .replace('__DOMAIN__', str(int(domain))).replace('__ZVER__', BRIDGE_VERSION)
            .replace('__DISTRO__', ros_distro()))
