"""Process supervisor for the ROS stack.

Each managed process runs in its own session (process group) so stopping it takes down
every child that `ros2 launch` spawned. Output goes to ~/.local/state/lumi/logs/<name>.log.
"""
import os
import re
import shutil
import signal
import subprocess
import threading
import time

from . import config, rviz

# Executables that must not be running twice (serial ports, DDS node names, TCP ports).
CONFLICT_EXES = {
    'core': ('arduino_bridge', 'sllidar_node', 'async_slam_toolbox_node', 'bt_navigator',
             'rf2o_laser_odometry_node'),
    'eyes': ('eyes_node',),
    'voice': ('voice_node',),
    'remote': ('remote_node',),
    'rosbridge': ('rosbridge_websocket',),
    'rviz_bridge': ('zenoh-bridge-ros2dds',),
}
LABELS = {
    'core': 'Robot core', 'eyes': 'Eye displays', 'voice': 'Voice control (Lumi)',
    'remote': 'Browser voice page (/voice/)', 'rosbridge': 'Robot web (tablet dashboards, /robot/)',
    'rviz_bridge': 'Remote RViz2 bridge (Zenoh)',
}
ORDER = ('core', 'eyes', 'voice', 'remote', 'rosbridge', 'rviz_bridge')
STOP_TIMEOUTS = (10.0, 5.0)        # seconds after SIGINT, after SIGTERM, then SIGKILL


def ros_cmd(argv):
    """Wrap argv so it runs with the ROS + workspace environment sourced."""
    script = '. "$0"; [ -f "$1" ] && . "$1"; shift; exec "$@"'
    return ['bash', '-c', script, config.ROS_SETUP, config.WS_SETUP, *argv]


def base_env():
    env = {
        'PATH': os.pathsep.join(['/opt/ros/humble/bin', os.path.join(config.HOME, '.local', 'bin'),
                                 '/usr/local/bin', '/usr/bin', '/bin']),
        'HOME': config.HOME,
        'USER': os.environ.get('USER', ''),
        'LOGNAME': os.environ.get('LOGNAME', os.environ.get('USER', '')),
        'LANG': os.environ.get('LANG', 'C.UTF-8'),
        'PYTHONUNBUFFERED': '1',
    }
    xdg = f'/run/user/{os.getuid()}'
    if os.path.isdir(xdg):
        env['XDG_RUNTIME_DIR'] = xdg           # audio (TTS) needs the user runtime dir
    return env


class Managed:
    def __init__(self, name):
        self.name = name
        self.popen = None
        self.state = 'stopped'     # stopped | running | stopping | crashed | failed | blocked
        self.wanted = False
        self.started_at = None
        self.exit_code = None
        self.message = ''
        self.crashes = []
        self.restart_at = None
        self.digest = None         # config digest captured at start (for "restart needed")


class Supervisor:
    def __init__(self, settings, env_store):
        self.settings = settings
        self.env = env_store
        self.lock = threading.RLock()
        self.procs = {n: Managed(n) for n in ORDER}
        self.stack_up = False
        self._stop_event = threading.Event()
        threading.Thread(target=self._monitor, daemon=True, name='supervisor').start()

    # ── command construction ────────────────────────────────────────────────
    def _launch_args(self, s):
        out = []
        for k, v in s['launch'].items():
            out.append(f'{k}:={str(v).lower() if isinstance(v, bool) else v}')
        return out

    def map_yaml(self, s):
        from . import maps          # local import: maps needs nothing from here
        path = maps.resolve(s['map'])
        return path

    def build(self, name, s):
        """Return (argv, error). argv is None when the process cannot be started."""
        if name == 'core':
            if s['mode'] == 'off':
                return None, 'Mode is "off" - choose hardware, mapping or navigation.'
            args = self._launch_args(s) + ['use_eyes:=false']
            if s['mode'] == 'hardware':
                return ros_cmd(['ros2', 'launch', 'omni_base', 'hardware.launch.py', *args]), None
            if s['mode'] == 'mapping':
                return ros_cmd(['ros2', 'launch', 'omni_base', 'mapping.launch.py', *args]), None
            yaml = self.map_yaml(s)
            if not yaml:
                return None, 'The selected map no longer exists - pick another one.'
            return ros_cmd(['ros2', 'launch', 'omni_base', 'navigation.launch.py', f'map:={yaml}', *args,
                            'use_voice:=false', 'use_web:=false', 'use_remote:=false']), None
        if name == 'eyes':
            return ros_cmd(['ros2', 'run', 'omni_base', 'eyes_node']), None
        if name == 'voice':
            # The local mic and the browser voice page are mutually exclusive inputs.
            mic = str(s['components']['voice']['input'] == 'mic').lower()
            return ros_cmd(['ros2', 'run', 'omni_base', 'voice_node', '--ros-args', '-p', f'enable_mic:={mic}']), None
        if name == 'remote':
            return ros_cmd(['ros2', 'run', 'omni_base', 'remote_node', '--ros-args', '-p', 'port:=0',
                            '-p', f'socket_path:={config.REMOTE_SOCKET}']), None
        if name == 'rosbridge':
            # rosbridge has no authentication, so it only listens on loopback; the panel
            # exposes it to dashboards through its own /rosbridge path (same-origin check).
            return ros_cmd(['ros2', 'launch', 'rosbridge_server', 'rosbridge_websocket_launch.xml',
                            'address:=127.0.0.1']), None
        if name == 'rviz_bridge':
            exe = rviz.bridge_binary()
            if not exe:
                return None, 'zenoh-bridge-ros2dds is not installed - use "Install bridge" on the RViz2 tab.'
            conf = rviz.write_bridge_config(s, self.env.as_environ())
            # Run it WITH the ROS environment: without ROS_DISTRO the bridge assumes 'iron' and mis-reads
            # ros_discovery_info (log spam, and remote tools may see no topics).
            return ros_cmd([exe, '-c', conf]), None
        return None, 'unknown component'

    def config_digest(self, name, s):
        """Fingerprint of everything that only takes effect at process start."""
        parts = [self.env.digest()]
        if name == 'core':
            parts += [s['mode'], s['map'] if s['mode'] == 'navigation' else '', repr(sorted(s['launch'].items()))]
        elif name == 'voice':
            parts.append(s['components']['voice']['input'])
        elif name == 'rviz_bridge':
            parts.append(repr(sorted(s['rviz'].items())))
        return '|'.join(parts)

    # ── conflict detection ──────────────────────────────────────────────────
    def _our_sids(self):
        sids = {os.getsid(0)}
        for m in self.procs.values():
            if m.popen is not None and m.popen.poll() is None:
                sids.add(m.popen.pid)
        return sids

    def external_processes(self, name):
        exes = CONFLICT_EXES.get(name, ())
        if not exes:
            return []
        pat = re.compile(r'(?:^|[ /])(?:' + '|'.join(re.escape(e) for e in exes) + r')(?: |$)')
        try:
            out = subprocess.run(['ps', '-eo', 'pid=,sid=,args='], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return []
        ours, found = self._our_sids(), []
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pid, sid, args = int(parts[0]), int(parts[1]), parts[2]
            if sid in ours or pid == os.getpid():
                continue
            if pat.search(args):
                found.append({'pid': pid, 'cmd': args[:100]})
        return found

    # ── start / stop ────────────────────────────────────────────────────────
    def start(self, name):
        with self.lock:
            m = self.procs[name]
            if m.popen is not None and m.popen.poll() is None:
                return
            s = self.settings.get()
            argv, err = self.build(name, s)
            m.wanted = True
            m.restart_at = None
            if argv is None:
                m.state, m.message = 'blocked', err
                return
            clash = self.external_processes(name)
            if clash:
                m.state = 'blocked'
                m.message = ('Already running outside the panel (pid '
                             + ', '.join(str(c['pid']) for c in clash) + '). Stop it first, e.g. a manual `ros2 launch`.')
                return
            os.makedirs(config.LOG_DIR, exist_ok=True)
            log_path = os.path.join(config.LOG_DIR, f'{name}.log')
            if os.path.exists(log_path):
                try:
                    os.replace(log_path, log_path + '.prev')
                except OSError:
                    pass
            env = {**base_env(), **self.env.as_environ()}
            try:
                logf = open(log_path, 'ab', buffering=0)
                m.popen = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                           env=env, cwd=config.WS if os.path.isdir(config.WS) else config.HOME,
                                           start_new_session=True)
                logf.close()
            except Exception as e:
                m.state, m.message = 'blocked', f'Could not start: {e}'
                return
            m.state, m.message = 'running', ''
            m.started_at, m.exit_code = time.time(), None
            m.digest = self.config_digest(name, s)

    def stop(self, name, wait=False):
        with self.lock:
            m = self.procs[name]
            m.wanted = False
            m.restart_at = None
            p = m.popen
            if p is None or p.poll() is not None:
                if m.state not in ('stopped',):
                    m.state, m.message = 'stopped', ''
                return None
            m.state = 'stopping'
        t = threading.Thread(target=self._terminate, args=(m, p), daemon=True)
        t.start()
        if wait:
            t.join()
        return t

    def _terminate(self, m, p):
        for sig, timeout in ((signal.SIGINT, STOP_TIMEOUTS[0]), (signal.SIGTERM, STOP_TIMEOUTS[1]), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                break
            try:
                p.wait(timeout=timeout)
                break
            except subprocess.TimeoutExpired:
                continue
        # Sweep any stragglers that outlived the leader but are still in the group.
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if m.name == 'remote':
            try:
                os.unlink(config.REMOTE_SOCKET)       # never leave a stale socket behind
            except OSError:
                pass
        with self.lock:
            if m.popen is p:
                m.state, m.exit_code, m.message = 'stopped', p.returncode, ''

    def restart(self, name):
        def go():
            self.stop(name, wait=True)
            self.start(name)
        threading.Thread(target=go, daemon=True).start()

    # ── stack ───────────────────────────────────────────────────────────────
    @staticmethod
    def desired(s):
        """Which non-core processes the settings ask for."""
        want = {n: s['components'][n]['enabled'] for n in ('eyes', 'voice', 'rosbridge', 'rviz_bridge')}
        # Browser voice page exists only when voice runs with the browser as its input.
        want['remote'] = s['components']['voice']['enabled'] and s['components']['voice']['input'] == 'browser'
        return want

    def start_stack(self):
        with self.lock:
            self.stack_up = True
            s = self.settings.get()
        if s['mode'] != 'off':
            self.start('core')
        for name, wanted in self.desired(s).items():
            if wanted:
                self.start(name)

    def stop_stack(self):
        with self.lock:
            self.stack_up = False
        threads = [self.stop(n) for n in reversed(ORDER)]
        for t in threads:
            if t is not None:
                t.join(timeout=30)

    def restart_stack(self):
        def go():
            self.stop_stack()
            self.start_stack()
        threading.Thread(target=go, daemon=True).start()

    def reconcile(self, before, after):
        """Apply a settings change to running processes (only when the stack is up)."""
        with self.lock:
            if not self.stack_up:
                return
        was, now = self.desired(before), self.desired(after)
        # Switching voice input must not leave the old input holding on: restart voice so
        # the microphone is released (browser) or taken back (mic) without a manual step.
        input_changed = before['components']['voice']['input'] != after['components']['voice']['input']
        if input_changed and now['voice']:
            if now['remote']:
                self.start('remote')              # browser takes over
            else:
                self.stop('remote')               # mic takes over: close the browser page first
            self.restart('voice')
        for name in ('eyes', 'voice', 'rosbridge', 'rviz_bridge', 'remote'):
            if was[name] != now[name] and not (name == 'voice' and input_changed):
                (self.start if now[name] else self.stop)(name)

    def shutdown(self):
        self._stop_event.set()
        threads = [self.stop(n) for n in reversed(ORDER)]      # all in parallel
        for t in threads:
            if t is not None:
                t.join(timeout=30)

    # ── monitor ─────────────────────────────────────────────────────────────
    def _enforce_switches(self):
        """A switched-off feature must not run. Checked every tick (not only when a switch is
        flipped), so drift from any cause - a manual start, a toggle while the system was down,
        a race - heals itself instead of leaving a feature running behind a switch that says off."""
        want = self.desired(self.settings.get())
        with self.lock:
            for name, wanted in want.items():
                m = self.procs[name]
                if not wanted and m.state == 'running':
                    self.stop(name)

    def _monitor(self):
        while not self._stop_event.wait(1.0):
            now = time.time()
            try:
                self._enforce_switches()
            except Exception:
                pass
            for m in list(self.procs.values()):
                with self.lock:
                    p = m.popen
                    if m.state == 'running' and p is not None and p.poll() is not None:
                        m.exit_code = p.returncode
                        self._on_exit(m, now)
                    elif m.state == 'crashed' and m.restart_at and now >= m.restart_at and m.wanted:
                        m.restart_at = None
                        threading.Thread(target=self.start, args=(m.name,), daemon=True).start()

    def _on_exit(self, m, now):
        m.crashes = [t for t in m.crashes if now - t < 120] + [now]
        if not (m.wanted and self.settings.get()['auto_restart']):
            m.state, m.message = 'crashed', f'Exited with code {m.exit_code}.'
            return
        if len(m.crashes) >= 5:
            m.state, m.wanted = 'failed', False
            m.message = f'Crashed {len(m.crashes)} times in 2 minutes (last exit code {m.exit_code}); gave up. Check the log.'
            return
        delay = min(30, 2 ** (len(m.crashes) - 1) * 2)
        m.state, m.restart_at = 'crashed', now + delay
        m.message = f'Exited with code {m.exit_code}; restarting in {delay}s.'

    # ── reporting ───────────────────────────────────────────────────────────
    def status(self):
        s = self.settings.get()
        now = time.time()
        out = {}
        with self.lock:
            for name, m in self.procs.items():
                alive = m.popen is not None and m.popen.poll() is None
                needs = bool(alive and m.state == 'running' and m.digest != self.config_digest(name, s))
                out[name] = {
                    'label': LABELS[name], 'state': m.state, 'message': m.message,
                    'pid': m.popen.pid if alive else None,
                    'uptime': int(now - m.started_at) if alive and m.started_at else None,
                    'exit_code': m.exit_code, 'needs_restart': needs,
                    'enabled': True if name == 'core' else self.desired(s)[name],
                }
            return {'stack_up': self.stack_up, 'processes': out}

    def tail(self, name, lines=200, previous=False):
        path = os.path.join(config.LOG_DIR, f'{name}.log' + ('.prev' if previous else ''))
        try:
            with open(path, 'rb') as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 96 * 1024))
                data = f.read().decode('utf-8', errors='replace')
        except OSError:
            return ''
        return '\n'.join(data.splitlines()[-lines:])


# ── one-shot ROS helpers (run in a throwaway process with the ROS env) ───────────
def run_ros(argv, timeout=30, env_store=None):
    env = {**base_env(), **(env_store.as_environ() if env_store else {})}
    try:
        r = subprocess.run(ros_cmd(argv), capture_output=True, text=True, timeout=timeout, env=env,
                           cwd=config.WS if os.path.isdir(config.WS) else config.HOME)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, 'timed out'


HALT_SCRIPT = r'''
import time, rclpy
from rclpy.node import Node
from omni_base.motion_stop import Halter
rclpy.init(); n = Node('panel_halt'); h = Halter(n)
t = time.time()
while time.time() - t < 2.0 and h.body_pub.get_subscription_count() == 0:
    rclpy.spin_once(n, timeout_sec=0.05)
for _ in range(2):
    h.halt(); end = time.time() + 0.6
    while time.time() < end: rclpy.spin_once(n, timeout_sec=0.05)
rclpy.shutdown()
'''

POSE_SCRIPT = r'''
import sys, math, time, rclpy
from rclpy.node import Node
from geometry_msgs.msg import PoseWithCovarianceStamped
x, y, yaw = (float(v) for v in sys.argv[1:4])
rclpy.init(); n = Node('panel_initial_pose')
pub = n.create_publisher(PoseWithCovarianceStamped, '/initialpose', 10)
t = time.time()
while time.time() - t < 3.0 and pub.get_subscription_count() == 0:
    rclpy.spin_once(n, timeout_sec=0.05)
m = PoseWithCovarianceStamped(); m.header.frame_id = 'map'
m.pose.pose.position.x, m.pose.pose.position.y = x, y
m.pose.pose.orientation.z, m.pose.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
m.pose.covariance[0] = m.pose.covariance[7] = 0.25; m.pose.covariance[35] = 0.0685
for _ in range(5):
    m.header.stamp = n.get_clock().now().to_msg(); pub.publish(m); rclpy.spin_once(n, timeout_sec=0.2)
print('published', pub.get_subscription_count(), 'subscriber(s)')
rclpy.shutdown()
'''


def which(exe):
    return shutil.which(exe, path=base_env()['PATH'])
