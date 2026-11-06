"""Paths, persisted settings and the environment/secrets store.

Everything here is plain stdlib so the panel runs without ROS being sourced (it is the
thing that starts ROS). Secrets live in ~/.config/lumi/env (mode 0600) and are only ever
returned to the browser masked.
"""
import copy
import hashlib
import json
import os
import re
import tempfile
import threading

HOME = os.path.expanduser('~')
CONFIG_DIR = os.environ.get('LUMI_CONFIG_DIR') or os.path.join(HOME, '.config', 'lumi')
STATE_DIR = os.environ.get('LUMI_STATE_DIR') or os.path.join(HOME, '.local', 'state', 'lumi')
DATA_DIR = os.environ.get('LUMI_DATA_DIR') or os.path.join(HOME, '.local', 'share', 'lumi')
WS = os.environ.get('LUMI_WS') or os.path.join(HOME, 'ros2_ws')
ROS_SETUP = os.environ.get('LUMI_ROS_SETUP') or '/opt/ros/humble/setup.bash'
WS_SETUP = os.path.join(WS, 'install', 'setup.bash')

SETTINGS_FILE = os.path.join(CONFIG_DIR, 'settings.json')
ENV_FILE = os.path.join(CONFIG_DIR, 'env')
LOG_DIR = os.path.join(STATE_DIR, 'logs')
SAVED_MAPS_DIR = os.path.join(DATA_DIR, 'maps')
REMOTE_SOCKET = os.path.join(STATE_DIR, 'remote.sock')


def ensure_dirs():
    for d in (CONFIG_DIR, STATE_DIR, DATA_DIR, LOG_DIR, SAVED_MAPS_DIR):
        os.makedirs(d, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)


def atomic_write(path, text, mode=0o644):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ════════════════════════════════════════════════════════════
#  SETTINGS
# ════════════════════════════════════════════════════════════
MODES = ('off', 'hardware', 'mapping', 'navigation')
# User-switchable features. (The browser voice page, 'remote', is not one of them: it is
# derived from voice.input == 'browser', which keeps it mutually exclusive with the mic.)
COMPONENTS = ('eyes', 'voice', 'rosbridge', 'rviz_bridge')
VOICE_INPUTS = ('mic', 'browser')

# name -> (type, default, min, max, label)
LAUNCH_ARGS = {
    'lidar_port':       ('dev',   '/dev/rplidar', None, None, 'LiDAR serial port'),
    'arduino_port':     ('dev',   '/dev/arduino', None, None, 'Arduino serial port'),
    'wheel_separation': ('float', 0.35, 0.05, 2.0, 'Wheel separation (m)'),
    'max_speed':        ('float', 0.26, 0.05, 1.5, 'Wheel speed at max PWM (m/s) - keep equal to Nav2 max speed'),
    'max_pwm':          ('int',   30, 15, 100, 'Max motor PWM'),
    'min_pwm':          ('int',   20, 0, 100, 'Min motor PWM (deadband)'),
    'publish_sensors':  ('bool',  False, None, None, 'Publish ultrasonic / IR telemetry'),
}

DEFAULTS = {
    'mode': 'navigation',
    'map': 'package:my_room_map',
    'autostart': False,
    'auto_restart': True,
    'launch': {k: v[1] for k, v in LAUNCH_ARGS.items()},
    'components': {
        'eyes':        {'enabled': True},
        'voice':       {'enabled': False, 'input': 'mic'},   # 'mic' | 'browser' (never both)
        'rosbridge':   {'enabled': False},
        'rviz_bridge': {'enabled': False},
    },
    # hostname = the tunnel's TCP hostname; domain = DDS domain id on the remote machine (0 = same as the robot).
    'rviz': {'hostname': '', 'domain': 0},
    'allowed_hosts': [],
    'listen_lan': False,           # panel listens on all interfaces (needs a panel restart)
}

MAP_ID_RE = re.compile(r'^(package|saved):[A-Za-z0-9_-]{1,40}$')
HOSTNAME_RE = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$')


class ValidationError(ValueError):
    pass


def _validate_launch(key, val):
    kind, _default, lo, hi, _label = LAUNCH_ARGS[key]
    if kind == 'dev':
        if not isinstance(val, str) or not re.fullmatch(r'/dev/[A-Za-z0-9_./-]{1,60}', val) or '..' in val:
            raise ValidationError(f'{key}: must be a /dev/... path')
        return val
    if kind == 'bool':
        if not isinstance(val, bool):
            raise ValidationError(f'{key}: must be true/false')
        return val
    try:
        num = float(val) if kind == 'float' else int(val)
    except (TypeError, ValueError):
        raise ValidationError(f'{key}: must be a number')
    if isinstance(val, bool) or not (lo <= num <= hi):
        raise ValidationError(f'{key}: must be between {lo} and {hi}')
    return num


class Settings:
    def __init__(self):
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        try:
            with open(SETTINGS_FILE) as f:
                self._merge(self._data, json.load(f))
        except (OSError, ValueError):
            pass

    @staticmethod
    def _merge(base, patch):
        for k, v in patch.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                Settings._merge(base[k], v)
            elif k in base:
                base[k] = v

    def get(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, patch):
        """Validate and apply a partial settings patch; returns the new settings."""
        if not isinstance(patch, dict):
            raise ValidationError('settings must be an object')
        with self._lock:
            new = copy.deepcopy(self._data)
            if 'mode' in patch:
                if patch['mode'] not in MODES:
                    raise ValidationError('mode must be one of ' + ', '.join(MODES))
                new['mode'] = patch['mode']
            if 'map' in patch:
                if not isinstance(patch['map'], str) or not MAP_ID_RE.match(patch['map']):
                    raise ValidationError('invalid map id')
                new['map'] = patch['map']
            for flag in ('autostart', 'auto_restart', 'listen_lan'):
                if flag in patch:
                    if not isinstance(patch[flag], bool):
                        raise ValidationError(f'{flag} must be true/false')
                    new[flag] = patch[flag]
            for key, val in (patch.get('launch') or {}).items():
                if key not in LAUNCH_ARGS:
                    raise ValidationError(f'unknown launch argument {key!r}')
                new['launch'][key] = _validate_launch(key, val)
            for name, cfg in (patch.get('components') or {}).items():
                if name not in COMPONENTS or not isinstance(cfg, dict):
                    raise ValidationError(f'unknown component {name!r}')
                for k, v in cfg.items():
                    if k not in new['components'][name]:
                        raise ValidationError(f'invalid option {name}.{k}')
                    if k == 'input':
                        if v not in VOICE_INPUTS:
                            raise ValidationError('voice.input must be "mic" or "browser"')
                    elif not isinstance(v, bool):
                        raise ValidationError(f'{name}.{k} must be true/false')
                    new['components'][name][k] = v
            if 'rviz' in patch:
                r = patch['rviz']
                if not isinstance(r, dict):
                    raise ValidationError('rviz must be an object')
                if 'hostname' in r:
                    h = r['hostname']
                    if not isinstance(h, str) or (h and not HOSTNAME_RE.match(h)):
                        raise ValidationError('rviz.hostname must be a hostname like zenoh.example.com')
                    new['rviz']['hostname'] = h
                if 'domain' in r:
                    d = r['domain']
                    if isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 232:
                        raise ValidationError('rviz.domain must be a whole number from 0 to 232')
                    new['rviz']['domain'] = d
            if 'allowed_hosts' in patch:
                hosts = patch['allowed_hosts']
                if not isinstance(hosts, list) or not all(isinstance(h, str) and HOSTNAME_RE.match(h.lstrip('*.')) for h in hosts):
                    raise ValidationError('allowed_hosts must be a list of hostnames')
                new['allowed_hosts'] = hosts
            self._data = new
            atomic_write(SETTINGS_FILE, json.dumps(new, indent=2, sort_keys=True))
            return copy.deepcopy(new)


# ════════════════════════════════════════════════════════════
#  ENVIRONMENT / SECRETS
# ════════════════════════════════════════════════════════════
ENV_NAME_RE = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')
# Only names that cannot change what code gets executed (no PATH, LD_PRELOAD, PYTHON*, ...).
ENV_PREFIXES = ('GEMINI_', 'LUMI_', 'ROS_', 'RMW_', 'CYCLONEDDS_', 'FASTRTPS_', 'ROBOT_')
SECRET_RE = re.compile(r'(KEY|TOKEN|SECRET|PASSWORD|PASSCODE)')
DEFAULT_ENV = {'ROS_DOMAIN_ID': '0', 'ROS_LOCALHOST_ONLY': '0', 'RMW_IMPLEMENTATION': 'rmw_cyclonedds_cpp'}
IMPORT_FROM_BASHRC = ('GEMINI_API_KEYS', 'GEMINI_API_KEY', 'ROS_DOMAIN_ID', 'ROS_LOCALHOST_ONLY',
                      'RMW_IMPLEMENTATION')


def env_name_allowed(name):
    return bool(ENV_NAME_RE.match(name)) and name.startswith(ENV_PREFIXES)


def mask(value):
    if len(value) <= 12:
        return '\u2022' * 6
    return value[:4] + '\u2026' + value[-4:]


def key_id(key):
    return hashlib.sha256(key.encode()).hexdigest()[:10]


def _read_bashrc_exports():
    found = {}
    try:
        with open(os.path.join(HOME, '.bashrc')) as f:
            for line in f:
                m = re.match(r'^\s*export\s+([A-Z0-9_]+)=(?:"([^"]*)"|\'([^\']*)\'|(\S*))', line)
                if m and m.group(1) in IMPORT_FROM_BASHRC:
                    found[m.group(1)] = next(g for g in m.groups()[1:] if g is not None)
    except OSError:
        pass
    return found


class EnvStore:
    def __init__(self):
        self._lock = threading.RLock()
        self.imported = []
        if os.path.exists(ENV_FILE):
            self._vars = self._load()
        else:
            # First run: adopt the ROS/Gemini settings the user already had in ~/.bashrc,
            # because a system service never reads it.
            self._vars = dict(DEFAULT_ENV)
            for name, val in _read_bashrc_exports().items():
                if val.strip():
                    self._vars[name] = val.strip()
                    self.imported.append(name)
            self._save()

    def _load(self):
        out = {}
        try:
            with open(ENV_FILE) as f:
                for line in f:
                    line = line.rstrip('\n')
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        out[k] = v
        except OSError:
            pass
        return out

    def _save(self):
        body = '# Managed by the Lumi control panel. Mode 0600 - contains secrets.\n'
        body += ''.join(f'{k}={v}\n' for k, v in sorted(self._vars.items()))
        atomic_write(ENV_FILE, body, 0o600)

    def as_environ(self):
        with self._lock:
            return dict(self._vars)

    def digest(self):
        with self._lock:
            return hashlib.sha256(json.dumps(self._vars, sort_keys=True).encode()).hexdigest()[:12]

    # -- generic variables ------------------------------------------------
    def listing(self):
        with self._lock:
            rows = []
            for k, v in sorted(self._vars.items()):
                if k in ('GEMINI_API_KEYS', 'GEMINI_API_KEY'):
                    continue            # managed through the Gemini key endpoints
                secret = bool(SECRET_RE.search(k))
                rows.append({'name': k, 'value': mask(v) if secret else v, 'secret': secret})
            return rows

    def set(self, name, value):
        if not env_name_allowed(name):
            raise ValidationError('variable names must be UPPER_CASE and start with one of: ' + ', '.join(ENV_PREFIXES))
        if name in ('GEMINI_API_KEYS',):
            raise ValidationError('use the Gemini keys section for GEMINI_API_KEYS')
        if not isinstance(value, str) or '\n' in value or '\r' in value or len(value) > 2000:
            raise ValidationError('value must be a single line (max 2000 chars)')
        with self._lock:
            self._vars[name] = value
            self._save()

    def delete(self, name):
        with self._lock:
            if name in DEFAULT_ENV:
                raise ValidationError(f'{name} is required; change its value instead')
            if self._vars.pop(name, None) is None:
                raise ValidationError('no such variable')
            self._save()

    # -- gemini keys ------------------------------------------------------
    def gemini_keys(self):
        with self._lock:
            raw = self._vars.get('GEMINI_API_KEYS', '') or self._vars.get('GEMINI_API_KEY', '')
        return [k.strip() for k in raw.split(',') if k.strip()]

    def gemini_listing(self):
        return [{'id': key_id(k), 'masked': mask(k)} for k in self.gemini_keys()]

    def _set_gemini(self, keys):
        with self._lock:
            self._vars.pop('GEMINI_API_KEY', None)
            if keys:
                self._vars['GEMINI_API_KEYS'] = ','.join(keys)
            else:
                self._vars.pop('GEMINI_API_KEYS', None)
            self._save()

    def add_gemini(self, key):
        key = (key or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9._-]{16,200}', key):
            raise ValidationError('that does not look like an API key')
        keys = self.gemini_keys()
        if key in keys:
            raise ValidationError('that key is already added')
        self._set_gemini(keys + [key])

    def remove_gemini(self, kid):
        keys = self.gemini_keys()
        left = [k for k in keys if key_id(k) != kid]
        if len(left) == len(keys):
            raise ValidationError('no such key')
        self._set_gemini(left)

    def gemini_by_id(self, kid):
        return next((k for k in self.gemini_keys() if key_id(k) == kid), None)
