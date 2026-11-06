"""HTTP API + static UI + /voice/ reverse proxy for the Lumi control panel (stdlib only).

Access control is deliberately NOT done here: the panel listens on 127.0.0.1 only, so it is
reachable from the Pi itself and, if you add a Cloudflare Tunnel hostname, through Cloudflare
- put Cloudflare Access in front of that hostname. What this file does enforce:
  * state-changing requests must be same-origin JSON (blocks cross-site form/fetch CSRF),
  * secrets are never sent to the browser unmasked,
  * only whitelisted actions - there is no endpoint that runs arbitrary commands,
  * optional Host allow-list (settings.allowed_hosts) against DNS rebinding.
"""
import http.client
import ipaddress
import json
import os
import re
import select
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, config, maps, rviz, supervisor
from .config import ValidationError

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
    '/app.css': ('app.css', 'text/css; charset=utf-8'),
    '/fonts/inter.woff2': ('fonts/inter.woff2', 'font/woff2'),
    '/fonts/mono.woff2': ('fonts/mono.woff2', 'font/woff2'),
}
SECURITY_HEADERS = {
    'Content-Security-Policy': ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; "
                                "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
    'Cache-Control': 'no-store',
}
# The existing tablet dashboards (web_interface/) are served read-only under /robot/.
ROBOT_ROOT = os.path.join(config.WS, 'src', 'omni_base', 'web_interface')
ROBOT_MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
              '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml',
              '.ico': 'image/x-icon', '.json': 'application/json', '.md': 'text/plain; charset=utf-8',
              '.woff2': 'font/woff2'}
HOST_RE = re.compile(r'^[A-Za-z0-9.\-:\[\]]{1,260}$')
ROSBRIDGE_ADDR = ('127.0.0.1', 9090)


def robot_csp(host):
    # The dashboards are fully self-contained: roslib and the fonts are served from web_interface/lib, and the pages carry
    # no inline scripts, handlers or <style>/style="" (they would work with no internet at all), so nothing external or
    # inline is allowed. connect-src adds the page's own host for the rosbridge WebSocket (/rosbridge).
    ws = f' ws://{host} wss://{host}' if HOST_RE.match(host or '') else ''
    return ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data: blob:; "
            f"connect-src 'self'{ws}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


HOP_BY_HOP = {'connection', 'keep-alive', 'transfer-encoding', 'server', 'date', 'proxy-authenticate',
              'proxy-authorization', 'te', 'trailers', 'upgrade'}
MAX_BODY = 8192
_ip_cache = {'t': 0, 'v': []}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=10):
        super().__init__('localhost', timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _read_first(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def pi_health():
    out = {'hostname': socket.gethostname()}
    temp = _read_first('/sys/class/thermal/thermal_zone0/temp')
    if temp and temp.isdigit():
        out['cpu_temp_c'] = round(int(temp) / 1000, 1)
    try:
        out['load'] = [round(x, 2) for x in os.getloadavg()]
        mem = {}
        with open('/proc/meminfo') as f:
            for line in f:
                k, v = line.split(':', 1)
                mem[k] = int(v.split()[0])
        out['mem_used_pct'] = round(100 * (1 - mem['MemAvailable'] / mem['MemTotal']))
        st = os.statvfs(config.HOME)
        out['disk_free_gb'] = round(st.f_bavail * st.f_frsize / 1e9, 1)
        out['uptime_s'] = int(float(_read_first('/proc/uptime', '0').split()[0]))
    except Exception:
        pass
    if time.time() - _ip_cache['t'] > 30:
        try:
            ips = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=3).stdout.split()
            _ip_cache.update(t=time.time(), v=[i for i in ips if ':' not in i])
        except Exception:
            pass
    out['ips'] = _ip_cache['v']
    return out


class Context:
    def __init__(self):
        config.ensure_dirs()
        self.settings = config.Settings()
        self.env = config.EnvStore()
        self.sup = supervisor.Supervisor(self.settings, self.env)
        self.busy = threading.Lock()          # serialises slow one-shot actions (save map, install, ...)


def make_handler(ctx):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'LumiPanel/' + __version__
        sys_version = ''
        timeout = 30

        def log_message(self, fmt, *args):
            pass

        # ── plumbing ────────────────────────────────────────────────────────
        def _send(self, code, body=b'', ctype='application/json', extra=None, secure_headers=True):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            if secure_headers:
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode())

        def _err(self, code, msg):
            self._json(code, {'error': msg})

        def _host_ok(self):
            allowed = ctx.settings.get()['allowed_hosts']
            if not allowed:
                return True
            host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]').lower()
            try:
                ipaddress.ip_address(host)
                return True                   # IP literals cannot be DNS-rebinding targets
            except ValueError:
                pass
            if host in ('localhost',):
                return True
            for a in allowed:
                a = a.lower()
                if host == a or (a.startswith('*.') and host.endswith(a[1:])):
                    return True
            return False

        def _mutation_ok(self):
            """Same-origin JSON only: a cross-site page cannot satisfy this."""
            if self.headers.get('Sec-Fetch-Site', 'same-origin') not in ('same-origin', 'none'):
                return False
            ctype = self.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if ctype != 'application/json':
                return False
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != (self.headers.get('X-Forwarded-Host') or self.headers.get('Host')):
                return False
            return True

        def _body(self):
            try:
                n = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                return None
            if not 0 <= n <= MAX_BODY:
                return None
            if n == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(n))
            except ValueError:
                return None
            return data if isinstance(data, dict) else None

        def _access_info(self):
            h = self.headers
            via_cf = bool(h.get('Cf-Ray') or h.get('Cf-Connecting-Ip'))
            user = h.get('Cf-Access-Authenticated-User-Email')
            protected = bool(user or h.get('Cf-Access-Jwt-Assertion'))
            return {'via_cloudflare': via_cf, 'access_user': user,
                    'unprotected_public': via_cf and not protected}

        # ── dispatch ────────────────────────────────────────────────────────
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._host_ok():
                return self._err(421, 'Host not allowed (see settings.allowed_hosts)')
            url = urlparse(self.path)
            path = url.path
            if path in STATIC_FILES:
                name, ctype = STATIC_FILES[path]
                try:
                    with open(os.path.join(STATIC_DIR, name), 'rb') as f:
                        return self._send(200, f.read(), ctype)
                except OSError:
                    return self._send(404)
            if path == '/voice':
                return self._send(301, extra={'Location': '/voice/'})
            if path.startswith('/voice/'):
                return self._proxy_voice(None)
            if path == '/robot':
                return self._send(301, extra={'Location': '/robot/'})
            if path.startswith('/robot/'):
                return self._serve_robot(path)
            if path == '/rosbridge':
                return self._proxy_rosbridge()
            q = parse_qs(url.query)
            try:
                return self._get_api(path, q)
            except ValueError as e:
                return self._err(400, str(e))
            except Exception as e:                       # never leak a traceback to the browser
                return self._err(500, f'{type(e).__name__}: {e}')

        def do_POST(self):
            if not self._host_ok():
                return self._err(421, 'Host not allowed (see settings.allowed_hosts)')
            if not self._mutation_ok():
                return self._err(403, 'cross-origin or non-JSON request refused')
            path = urlparse(self.path).path
            if path.startswith('/voice/'):
                return self._proxy_voice(self._raw_body())
            data = self._body()
            if data is None:
                return self._err(400, 'bad request body')
            try:
                return self._post_api(path, data)
            except ValueError as e:
                return self._err(400, str(e))
            except Exception as e:
                return self._err(500, f'{type(e).__name__}: {e}')

        def _raw_body(self):
            try:
                n = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                n = 0
            return self.rfile.read(n) if 0 < n <= MAX_BODY else b''

        # ── /robot/ : the existing dashboards ─────────────────────────────────
        def _off_page(self, title, body):
            # No inline style: the panel's policy forbids it. Reuse the panel's own stylesheet instead.
            page = ('<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">'
                    '<title>Lumi</title><link rel=stylesheet href=/app.css>'
                    '<main><div class=card><div class=brand><i class=brand-eyes></i><h1>Lumi</h1></div>'
                    f'<h2>{title}</h2><p class=muted>{body}</p>'
                    '<div class=row><a href=/><button class=primary type=button>Back to the control panel</button></a></div></div></main>')
            return self._send(503, page.encode(), 'text/html; charset=utf-8')

        def _serve_robot(self, path):
            if ctx.sup.procs['rosbridge'].state != 'running':
                return self._off_page('Robot web is off',
                                      'Turn on <b>Robot web</b> in the control panel (Components) and start the system.')
            rel = path[len('/robot/'):]
            if rel == '':
                return self._send(302, extra={'Location': '/robot/layouts/'})
            if rel.endswith('/'):
                rel += 'index.html'
            root = os.path.realpath(ROBOT_ROOT)
            full = os.path.realpath(os.path.join(root, rel))
            ext = os.path.splitext(full)[1].lower()
            if os.path.commonpath([root, full]) != root or ext not in ROBOT_MIME or not os.path.isfile(full):
                return self._send(404)
            with open(full, 'rb') as f:
                body = f.read()
            self._send(200, body, ROBOT_MIME[ext], secure_headers=False, extra={
                'Content-Security-Policy': robot_csp(self.headers.get('X-Forwarded-Host') or self.headers.get('Host')),
                'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-cache'})

        # ── /rosbridge : WebSocket pass-through to the loopback-only rosbridge ──
        def _proxy_rosbridge(self):
            if self.headers.get('Upgrade', '').lower() != 'websocket':
                return self._err(400, 'WebSocket connections only')
            host = self.headers.get('X-Forwarded-Host') or self.headers.get('Host')
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != host:      # blocks cross-site WebSocket hijacking
                return self._err(403, 'cross-origin WebSocket refused')
            if ctx.sup.procs['rosbridge'].state != 'running':
                return self._err(503, 'Robot web (rosbridge) is not running')
            try:
                up = socket.create_connection(ROSBRIDGE_ADDR, timeout=5)
            except OSError:
                return self._err(502, 'rosbridge is not reachable yet - try again in a few seconds')
            try:
                head = 'GET / HTTP/1.1\r\n' + ''.join(
                    f'{k}: {v}\r\n' for k, v in self.headers.items() if k.lower() != 'x-forwarded-host') + '\r\n'
                up.sendall(head.encode('latin-1'))
                client = self.connection
                client.settimeout(None)
                up.settimeout(None)
                self.close_connection = True
                socks = (client, up)
                while True:
                    ready, _, _ = select.select(socks, [], [], 3600)
                    if not ready:
                        return
                    for sk in ready:
                        data = sk.recv(65536)
                        if not data:
                            return
                        (up if sk is client else client).sendall(data)
            except OSError:
                return
            finally:
                up.close()

        # ── /voice/ reverse proxy (private unix socket to remote_node) ─────────
        def _proxy_voice(self, body):
            upstream = self.path[len('/voice'):] or '/'
            if ctx.sup.procs['remote'].state != 'running' or not os.path.exists(config.REMOTE_SOCKET):
                page = (b'<!doctype html><meta name=viewport content="width=device-width">'
                        b'<body style="font:16px system-ui;padding:2em;background:#0f1420;color:#e8ecf5">'
                        b'<h2>Browser voice is off</h2>'
                        b'<p>In the control panel (Components) turn on <b>Voice control</b> and choose '
                        b'<b>Browser</b> as the voice input, then start the system.</p><p><a style="color:#4f8cff" href="/">Back to panel</a></p>')
                return self._send(503, page, 'text/html; charset=utf-8')
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() in ('content-type', 'accept', 'origin', 'host', 'cf-connecting-ip')}
            headers['X-Forwarded-Host'] = self.headers.get('Host', '')
            if body is not None:
                headers['Content-Length'] = str(len(body))
            try:
                conn = UnixHTTPConnection(config.REMOTE_SOCKET)
                conn.request(self.command, upstream, body=body, headers=headers)
                resp = conn.getresponse()
                payload = resp.read(1024 * 1024)
            except Exception as e:
                return self._send(502, f'voice remote unreachable: {e}'.encode(), 'text/plain; charset=utf-8')
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in HOP_BY_HOP or k.lower() == 'content-length':
                    continue
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(payload)

        # ── GET api ─────────────────────────────────────────────────────────
        def _get_api(self, path, q):
            if path == '/api/status':
                s = ctx.settings.get()
                st = ctx.sup.status()
                return self._json(200, {
                    'version': __version__, 'settings': s, **st,
                    'health': pi_health(), 'access': self._access_info(),
                    'maps_dir': config.SAVED_MAPS_DIR,
                    'launch_schema': {k: {'type': v[0], 'min': v[2], 'max': v[3], 'label': v[4]}
                                      for k, v in config.LAUNCH_ARGS.items()},
                    'env_imported': ctx.env.imported,
                })
            if path == '/api/maps':
                return self._json(200, {'maps': maps.list_maps(), 'selected': ctx.settings.get()['map']})
            if path == '/api/maps/preview':
                png = maps.preview_png((q.get('id') or [''])[0])
                if png is None:
                    return self._send(404)
                return self._send(200, png, 'image/png', extra={'Cache-Control': 'private, max-age=60'})
            if path == '/api/logs':
                name = (q.get('name') or [''])[0]
                if name not in supervisor.ORDER:
                    raise ValidationError('unknown process')
                lines = max(10, min(1000, int((q.get('lines') or ['200'])[0])))
                prev = (q.get('previous') or ['0'])[0] == '1'
                return self._json(200, {'name': name, 'text': ctx.sup.tail(name, lines, prev)})
            if path == '/api/env':
                return self._json(200, {'vars': ctx.env.listing(), 'gemini': ctx.env.gemini_listing(),
                                        'prefixes': list(config.ENV_PREFIXES)})
            if path == '/api/rviz':
                s = ctx.settings.get()
                return self._json(200, {
                    'installed': bool(rviz.bridge_binary()), 'version': rviz.BRIDGE_VERSION, 'port': rviz.BRIDGE_PORT,
                    'hostname': s['rviz']['hostname'], 'domain': s['rviz']['domain'],
                })
            if path == '/rviz/lumi-rviz.sh':
                host = (q.get('host') or [''])[0].strip()
                if host and not config.HOSTNAME_RE.match(host):
                    raise ValidationError('host must be a hostname like zenoh.example.com')
                dom = (q.get('domain') or [''])[0].strip()
                if dom and not (dom.isdigit() and 0 <= int(dom) <= 232):
                    raise ValidationError('domain must be a whole number from 0 to 232')
                body = rviz.connect_script(ctx.settings.get(), ctx.env.as_environ(),
                                           host=host or None, domain=int(dom) if dom else None).encode()
                return self._send(200, body, 'text/x-shellscript; charset=utf-8',
                                  extra={'Content-Disposition': 'attachment; filename="lumi-rviz.sh"'})
            return self._send(404)

        # ── POST api ────────────────────────────────────────────────────────
        def _post_api(self, path, d):
            sup = ctx.sup
            if path == '/api/stack':
                action = d.get('action')
                if action == 'start':
                    threading.Thread(target=sup.start_stack, daemon=True).start()
                elif action == 'stop':
                    threading.Thread(target=sup.stop_stack, daemon=True).start()
                elif action == 'restart':
                    sup.restart_stack()
                else:
                    raise ValidationError('action must be start, stop or restart')
                return self._json(200, {'ok': True})

            if path == '/api/component':
                name, action = d.get('name'), d.get('action')
                if name not in supervisor.ORDER:
                    raise ValidationError('unknown process')
                if action in ('start', 'restart') and name != 'core' and not sup.desired(ctx.settings.get())[name]:
                    raise ValidationError('That feature is switched off - turn its switch on first'
                                          + (' (the voice page follows Voice control set to Browser input).' if name == 'remote' else '.'))
                if action == 'start':
                    threading.Thread(target=sup.start, args=(name,), daemon=True).start()
                elif action == 'stop':
                    sup.stop(name)
                elif action == 'restart':
                    sup.restart(name)
                elif action in ('enable', 'disable') and name in config.COMPONENTS:
                    before = ctx.settings.get()
                    after = ctx.settings.update({'components': {name: {'enabled': action == 'enable'}}})
                    sup.reconcile(before, after)
                else:
                    raise ValidationError('invalid action (the browser voice page follows the voice input setting)')
                return self._json(200, {'ok': True})

            if path == '/api/settings':
                before = ctx.settings.get()
                new = ctx.settings.update(d)
                sup.reconcile(before, new)
                return self._json(200, {'ok': True, 'settings': new})

            if path == '/api/panel/restart':
                # Exit cleanly; systemd (Restart=always) brings the panel back, which also
                # applies the network setting. The robot stack stops with it.
                threading.Thread(target=lambda: (time.sleep(0.5), os.kill(os.getpid(), signal.SIGTERM)),
                                 daemon=True).start()
                return self._json(200, {'ok': True})

            if path == '/api/stop':                      # emergency stop - always allowed, always fast
                self._emergency_stop()
                return self._json(200, {'ok': True})

            if path == '/api/maps/select':
                mid = d.get('id')
                if not maps.resolve(mid):
                    raise ValidationError('no such map')
                ctx.settings.update({'map': mid})
                if d.get('apply') and sup.status()['processes']['core']['state'] == 'running':
                    sup.restart('core')
                return self._json(200, {'ok': True})

            if path == '/api/maps/delete':
                maps.delete_saved(d.get('id'))
                if ctx.settings.get()['map'] == d.get('id'):
                    ctx.settings.update({'map': 'package:my_room_map'})
                return self._json(200, {'ok': True})

            if path == '/api/maps/save':
                name = d.get('name', '')
                argv = maps.save_argv(name)
                core = sup.status()['processes']['core']
                if core['state'] != 'running' or ctx.settings.get()['mode'] != 'mapping':
                    raise ValidationError('Start the system in mapping mode first - there is no live map to save.')
                if maps.resolve(f'saved:{name}') and not d.get('overwrite'):
                    return self._json(409, {'error': 'a saved map with that name exists', 'exists': True})
                if not ctx.busy.acquire(blocking=False):
                    raise ValidationError('another long action is running')
                try:
                    rc, out = supervisor.run_ros(argv, timeout=60, env_store=ctx.env)
                finally:
                    ctx.busy.release()
                if rc != 0 or not maps.resolve(f'saved:{name}'):
                    return self._json(500, {'error': 'map_saver failed', 'output': out[-1500:]})
                return self._json(200, {'ok': True, 'id': f'saved:{name}', 'output': out[-500:]})

            if path == '/api/nav/initial_pose':
                try:
                    x, y, yaw = (float(d[k]) for k in ('x', 'y', 'yaw'))
                except (KeyError, TypeError, ValueError):
                    raise ValidationError('x, y and yaw (radians) are required numbers')
                if not all(abs(v) < 1000 for v in (x, y, yaw)):
                    raise ValidationError('values out of range')
                rc, out = supervisor.run_ros(['python3', '-c', supervisor.POSE_SCRIPT, str(x), str(y), str(yaw)],
                                             timeout=20, env_store=ctx.env)
                if rc != 0:
                    return self._json(500, {'error': 'could not publish', 'output': out[-500:]})
                return self._json(200, {'ok': True, 'output': out[-200:]})

            if path == '/api/env':
                ctx.env.set(str(d.get('name', '')).strip(), d.get('value'))
                return self._json(200, {'ok': True})
            if path == '/api/env/delete':
                ctx.env.delete(str(d.get('name', '')))
                return self._json(200, {'ok': True})
            if path == '/api/gemini/add':
                ctx.env.add_gemini(d.get('key'))
                return self._json(200, {'ok': True})
            if path == '/api/gemini/remove':
                ctx.env.remove_gemini(str(d.get('id', '')))
                return self._json(200, {'ok': True})
            if path == '/api/gemini/test':
                key = ctx.env.gemini_by_id(str(d.get('id', '')))
                if not key:
                    raise ValidationError('no such key')
                return self._json(200, self._test_gemini(key))

            if path == '/api/rviz/install':
                if not ctx.busy.acquire(blocking=False):
                    raise ValidationError('another long action is running')
                try:
                    msg = rviz.install_bridge()
                finally:
                    ctx.busy.release()
                return self._json(200, {'ok': True, 'message': msg})

            return self._send(404)

        # ── helpers ─────────────────────────────────────────────────────────
        def _emergency_stop(self):
            """Fast path via the voice remote if it is up, plus a one-shot Halter in any case."""
            def fast():
                try:
                    c = UnixHTTPConnection(config.REMOTE_SOCKET, timeout=2)
                    c.request('POST', '/api/stop', body=b'{}', headers={'Content-Type': 'application/json'})
                    c.getresponse().read()
                    c.close()
                except Exception:
                    pass
            if os.path.exists(config.REMOTE_SOCKET):
                threading.Thread(target=fast, daemon=True).start()
            threading.Thread(target=supervisor.run_ros, args=(['python3', '-c', supervisor.HALT_SCRIPT],),
                             kwargs={'timeout': 20, 'env_store': ctx.env}, daemon=True).start()

        @staticmethod
        def _test_gemini(key):
            req = urllib.request.Request('https://generativelanguage.googleapis.com/v1beta/models?pageSize=1',
                                         headers={'x-goog-api-key': key})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return {'ok': True, 'status': r.status, 'detail': 'Key accepted.'}
            except urllib.error.HTTPError as e:
                detail = {400: 'Rejected (invalid key).', 401: 'Rejected (unauthorised).', 403: 'Rejected (forbidden or API not enabled).',
                          429: 'Valid, but currently rate-limited.'}.get(e.code, f'HTTP {e.code}')
                return {'ok': e.code == 429, 'status': e.code, 'detail': detail}
            except Exception as e:
                return {'ok': False, 'status': None, 'detail': f'Could not reach Google: {type(e).__name__}'}

    return Handler


def serve(host=None, port=8090):
    ctx = Context()
    if not host:
        host = os.environ.get('LUMI_PANEL_HOST') or ('0.0.0.0' if ctx.settings.get()['listen_lan'] else '127.0.0.1')
    if host not in ('127.0.0.1', 'localhost', '::1'):
        print(f'WARNING: listening on {host}: the panel has no login of its own - anyone who can reach '
              f'this address controls the robot.', flush=True)
    httpd = ThreadingHTTPServer((host, port), make_handler(ctx))
    httpd.daemon_threads = True
    if ctx.settings.get()['autostart']:
        threading.Thread(target=ctx.sup.start_stack, daemon=True).start()
    print(f'Lumi panel {__version__} on http://{host}:{port}', flush=True)
    return ctx, httpd
