#!/usr/bin/env python3
"""
Lumi Remote Node - the server side of the browser "voice commander" page.

The browser does the speech-to-text AND the command parsing itself (Web Speech API
+ parser in web_remote/app.js). Audio and raw transcripts never reach the robot: the page
sends one small structured action per recognised command, e.g. {"action":"drive",
"dir":"forward"}. Speech that is not a command is discarded in the browser. This node:

  * serves the tiny static page (stdlib only, no extra dependencies),
  * validates each action against a fixed whitelist (the client is not trusted) and
    hands it to voice_node on /robot/voice/remote_action,
  * relays the robot's spoken replies (/robot/voice/reply) back to the page.

Listening: standalone it binds 127.0.0.1:<port>. Under the control panel it is started
with port:=0 and socket_path:=<file>, i.e. NO TCP port at all: the panel reverse-proxies
https://<panel>/voice/ to the private Unix socket, so one tunnel hostname serves everything.
(Browsers need HTTPS for microphone access; Cloudflare provides it.)

There is no login here: access control belongs to the front door (localhost-only, or
Cloudflare Access in front of the tunnel hostname).
"""
import json
import os
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import rclpy
import yaml
from ament_index_python.packages import get_package_share_directory
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile
from std_msgs.msg import String

from omni_base.motion_stop import Halter

MAX_BODY = 2048
MAX_TEXT = 200

# Whitelisted actions -> canonical command text understood by voice_node.
# Anything not in these tables is rejected, whatever the client sends.
DRIVE = {'forward': 'go forward', 'back': 'go backward', 'left': 'turn left', 'right': 'turn right'}
LOOK = {'left': 'look left', 'right': 'look right', 'center': 'look forward'}
GESTURE = {
    'nod': 'nod', 'shake': 'shake', 'blink': 'blink',
    'wave': 'wave', 'wave_left': 'wave left', 'wave_right': 'wave',
    'hands_up': 'hands up', 'hands_down': 'hands down',
    'salute': 'salute', 'goodbye': 'goodbye', 'reset': 'reset',
    'wrist_up': 'wrist up', 'wrist_down': 'wrist down', 'wrist_center': 'wrist',
}

STATIC_FILES = {
    '/':        ('index.html', 'text/html; charset=utf-8'),
    '/app.js':  ('app.js',     'text/javascript; charset=utf-8'),
    '/app.css': ('app.css',    'text/css; charset=utf-8'),
}

SECURITY_HEADERS = {
    'Content-Security-Policy': (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
    'Cache-Control': 'no-store',
    # Allow the microphone for this origin only (Web Speech API needs it).
    'Permissions-Policy': 'microphone=(self), camera=(), geolocation=()',
}


class UnixThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer on a Unix domain socket (mode 0600, owner only)."""
    address_family = socket.AF_UNIX

    def server_bind(self):
        path = self.server_address
        if os.path.exists(path):
            os.unlink(path)                       # stale socket from a previous run
        old = os.umask(0o177)
        try:
            self.socket.bind(path)
        finally:
            os.umask(old)
        self.server_name, self.server_port = 'unix', 0

    def get_request(self):
        conn, _ = self.socket.accept()
        return conn, ('unix', 0)

    def server_close(self):
        super().server_close()
        try:
            os.unlink(self.server_address)
        except OSError:
            pass


# ════════════════════════════════════════════════════════════
#  ROS NODE
# ════════════════════════════════════════════════════════════
class RemoteNode(Node):
    def __init__(self):
        super().__init__('remote_node')
        self.port = int(self.declare_parameter('port', 8088).value)
        # If set, listen on this Unix socket instead of TCP (used by the control panel).
        self.socket_path = self.declare_parameter('socket_path', '').value
        remote_topic = self.declare_parameter('remote_topic', '/robot/voice/remote_action').value
        reply_topic = self.declare_parameter('reply_topic', '/robot/voice/reply').value
        body_topic = self.declare_parameter('body_topic', '/robot/body/command').value

        share = get_package_share_directory('omni_base')
        self.static_dir = self._find_static_dir(share)

        self.remote_pub = self.create_publisher(String, remote_topic, 10)
        # STOP must work even if voice_node is busy or down, so it is done here directly:
        # bridge STOP + cancel Nav2 goals + zero /cmd_vel (see motion_stop.py).
        self.halter = Halter(self, body_topic=body_topic)
        self.create_subscription(String, reply_topic, self._on_reply, 10)

        # Room names the page needs to resolve "go to <room>" (static + user-saved).
        self.static_rooms = self._load_static_rooms(os.path.join(share, 'config', 'rooms.yaml'))
        self.rooms = list(self.static_rooms)
        latched = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(String, '/saved_locations', self._on_locations, latched)

        self.replies = deque(maxlen=50)
        self.reply_id = 0
        self.reply_lock = threading.Lock()

        # Simple global command throttle: at most 20 commands per 10 s.
        self._cmd_times = deque()
        self._cmd_lock = threading.Lock()

        if self.socket_path:
            self.server = UnixThreadingHTTPServer(self.socket_path, self._make_handler())
            where = f'unix socket {self.socket_path} (served by the control panel at /voice/)'
        else:
            self.server = ThreadingHTTPServer(('127.0.0.1', self.port), self._make_handler())
            where = f'http://127.0.0.1:{self.port}'
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.get_logger().info(f'Voice page server listening on {where}')

    # -- helpers ------------------------------------------------------------
    def _find_static_dir(self, share):
        installed = os.path.join(share, 'web_remote')
        source = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'web_remote')
        for d in (installed, source):
            if os.path.isfile(os.path.join(d, 'index.html')):
                return d
        raise RuntimeError('web_remote/index.html not found (rebuild with colcon build)')

    def _load_static_rooms(self, path):
        try:
            with open(path) as f:
                return sorted((yaml.safe_load(f) or {}).get('rooms', {}))
        except Exception as e:
            self.get_logger().warn(f'Could not read rooms file {path}: {e}')
            return []

    def _on_locations(self, msg):
        try:
            names = list((json.loads(msg.data).get('locations') or {}).keys())
            self.rooms = sorted(set(self.static_rooms) | {str(n) for n in names})
        except Exception as e:
            self.get_logger().warn(f'Bad /saved_locations payload: {e}')

    def _on_reply(self, msg):
        with self.reply_lock:
            self.reply_id += 1
            self.replies.append({'id': self.reply_id, 'text': msg.data[:400]})

    def replies_after(self, after):
        with self.reply_lock:
            if after is None:
                return [], self.reply_id
            return [r for r in self.replies if r['id'] > after], self.reply_id

    def throttle_ok(self):
        now = time.time()
        with self._cmd_lock:
            while self._cmd_times and self._cmd_times[0] < now - 10:
                self._cmd_times.popleft()
            if len(self._cmd_times) >= 20:
                return False
            self._cmd_times.append(now)
            return True

    def _publish_action(self, kind, text):
        msg = String(); msg.data = json.dumps({'type': kind, 'text': text})
        self.remote_pub.publish(msg)

    def translate(self, data):
        """Validate a client action; return (kind, text) for voice_node or None."""
        action = data.get('action')
        if action == 'stop':
            return 'command', 'stop'
        if action == 'drive' and data.get('dir') in DRIVE:
            return 'command', DRIVE[data['dir']]
        if action == 'look' and data.get('dir') in LOOK:
            return 'command', LOOK[data['dir']]
        if action == 'gesture' and data.get('name') in GESTURE:
            return 'command', GESTURE[data['name']]
        if action == 'goto':
            room = data.get('room')
            if isinstance(room, str):
                known = {r.lower(): r for r in self.rooms}
                if room.strip().lower() in known:
                    return 'command', f'go to {known[room.strip().lower()]}'
        if action == 'ask':
            text = data.get('text')
            if isinstance(text, str) and text.strip():
                return 'ask', ' '.join(text.split())[:MAX_TEXT]
        return None

    def send_action(self, kind, text):
        self._publish_action(kind, text)

    def emergency_stop(self):
        self.halter.halt()
        self._publish_action('command', 'stop')      # lets voice_node announce it

    # -- HTTP ---------------------------------------------------------------
    def _make_handler(self):
        node = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'LumiRemote'
            sys_version = ''
            timeout = 10

            def log_message(self, fmt, *args):      # keep the ROS log quiet
                pass

            # -- plumbing --
            def _ip(self):
                return self.headers.get('CF-Connecting-IP') or self.client_address[0]

            def _send(self, code, body=b'', ctype='application/json', extra=None):
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)

            def _json(self, code, obj):
                self._send(code, json.dumps(obj).encode())

            def _body(self):
                ctype = self.headers.get('Content-Type', '').split(';')[0].strip().lower()
                if ctype != 'application/json':
                    return None
                origin = self.headers.get('Origin')
                if origin and urlparse(origin).netloc != (self.headers.get('X-Forwarded-Host')
                                                           or self.headers.get('Host')):
                    return None
                try:
                    n = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    return None
                if not 0 < n <= MAX_BODY:
                    return None
                try:
                    data = json.loads(self.rfile.read(n))
                except ValueError:
                    return None
                return data if isinstance(data, dict) else None

            # -- routes --
            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                url = urlparse(self.path)
                if url.path in STATIC_FILES:
                    name, ctype = STATIC_FILES[url.path]
                    try:
                        with open(os.path.join(node.static_dir, name), 'rb') as f:
                            return self._send(200, f.read(), ctype)
                    except OSError:
                        return self._send(404)
                if url.path == '/api/rooms':
                    return self._json(200, {'rooms': node.rooms})
                if url.path == '/api/replies':
                    after = parse_qs(url.query).get('after', [None])[0]
                    try:
                        after = int(after) if after is not None else None
                    except ValueError:
                        after = None
                    replies, last = node.replies_after(after)
                    return self._json(200, {'replies': replies, 'last': last})
                return self._send(404)

            def do_POST(self):
                url = urlparse(self.path)
                data = self._body()
                if data is None:
                    return self._json(400, {'error': 'bad request'})

                if url.path == '/api/stop':
                    node.emergency_stop()
                    node.get_logger().warn(f'STOP from {self._ip()}')
                    return self._json(200, {'ok': True})

                if url.path == '/api/action':
                    parsed = node.translate(data)
                    if parsed is None:
                        return self._json(400, {'error': 'unknown action'})
                    if node.remote_pub.get_subscription_count() == 0:
                        # voice_node is not running (or still starting): say so instead of
                        # reporting success for a command nobody received.
                        return self._json(503, {'error': 'Robot voice is not running'})
                    if not node.throttle_ok():
                        return self._json(429, {'error': 'slow down'})
                    kind, text = parsed
                    node.get_logger().info(
                        f'Action from {self._ip()}: {kind} '
                        f'{text if kind == "command" else "<question>"}')
                    node.send_action(kind, text)
                    return self._json(200, {'ok': True})

                return self._send(404)

        return Handler

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()


def main(args=None):
    rclpy.init(args=args)
    node = RemoteNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
