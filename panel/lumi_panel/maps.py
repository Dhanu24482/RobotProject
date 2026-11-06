"""Map discovery, previews and housekeeping (stdlib only: PGM -> PNG via zlib)."""
import math
import os
import re
import struct
import threading
import zlib

from . import config

NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
_preview_cache = {}
_cache_lock = threading.Lock()


def _map_dirs():
    return [
        ('package', os.path.join(config.WS, 'install', 'omni_base', 'share', 'omni_base', 'maps')),
        ('package', os.path.join(config.WS, 'src', 'omni_base', 'maps')),
        ('saved', config.SAVED_MAPS_DIR),
    ]


def _parse_yaml(path):
    """Minimal map_server yaml reader (flat key: value lines)."""
    info = {}
    with open(path) as f:
        for line in f:
            line = line.split('#', 1)[0].strip()
            if ':' in line:
                k, v = line.split(':', 1)
                info[k.strip()] = v.strip()
    return info


def _read_pgm(path):
    with open(path, 'rb') as f:
        data = f.read()
    tokens, pos = [], 0
    while len(tokens) < 4:                       # P5 <w> <h> <maxval>
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b'#':
            pos = data.index(b'\n', pos) + 1
            continue
        end = pos
        while end < len(data) and not data[end:end + 1].isspace():
            end += 1
        tokens.append(data[pos:end])
        pos = end
    if tokens[0] != b'P5':
        raise ValueError('only binary PGM (P5) maps are supported')
    w, h, maxval = int(tokens[1]), int(tokens[2]), int(tokens[3])
    if maxval > 255:
        raise ValueError('16-bit PGM not supported')
    pixels = data[pos + 1:pos + 1 + w * h]
    if len(pixels) != w * h:
        raise ValueError('truncated PGM')
    return w, h, pixels


def _pgm_size(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(64)
        parts = re.sub(rb'#[^\n]*', b'', head).split()
        return int(parts[1]), int(parts[2])
    except Exception:
        return None, None


def list_maps():
    seen, out = set(), []
    for source, d in _map_dirs():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            stem, ext = os.path.splitext(fn)
            if ext != '.yaml' or not NAME_RE.match(stem) or (source, stem) in seen:
                continue
            ypath = os.path.join(d, fn)
            try:
                info = _parse_yaml(ypath)
                image = os.path.join(d, info.get('image', ''))
                if not os.path.isfile(image):
                    continue
                w, h = _pgm_size(image)
                res = float(info.get('resolution', 0) or 0)
                out.append({
                    'id': f'{source}:{stem}', 'name': stem, 'source': source,
                    'width': w, 'height': h, 'resolution': res,
                    'meters': [round(w * res, 1), round(h * res, 1)] if w and res else None,
                    'mtime': int(os.path.getmtime(ypath)),
                })
                seen.add((source, stem))
            except (OSError, ValueError):
                continue
    return out


def resolve(map_id):
    """Map id -> absolute yaml path (None if missing)."""
    m = re.match(r'^(package|saved):([A-Za-z0-9_-]{1,40})$', map_id or '')
    if not m:
        return None
    for source, d in _map_dirs():
        if source == m.group(1):
            p = os.path.join(d, m.group(2) + '.yaml')
            if os.path.isfile(p):
                return p
    return None


def _png(w, h, gray):
    def chunk(tag, body):
        c = struct.pack('>I', len(body)) + tag + body
        return c + struct.pack('>I', zlib.crc32(tag + body) & 0xffffffff)
    raw = b''.join(b'\x00' + gray[y * w:(y + 1) * w] for y in range(h))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def preview_png(map_id, max_dim=600):
    ypath = resolve(map_id)
    if not ypath:
        return None
    key = (ypath, os.path.getmtime(ypath))
    with _cache_lock:
        if key in _preview_cache:
            return _preview_cache[key]
    info = _parse_yaml(ypath)
    w, h, px = _read_pgm(os.path.join(os.path.dirname(ypath), info['image']))
    f = max(1, math.ceil(max(w, h) / max_dim))
    if f > 1:                                    # block-minimum keeps thin walls visible
        nw, nh = w // f, h // f
        out = bytearray(nw * nh)
        for y in range(nh):
            rows = [px[(y * f + dy) * w:(y * f + dy) * w + nw * f] for dy in range(f)]
            for x in range(nw):
                out[y * nw + x] = min(min(r[x * f:(x + 1) * f]) for r in rows)
        w, h, px = nw, nh, bytes(out)
    png = _png(w, h, px)
    with _cache_lock:
        if len(_preview_cache) > 8:
            _preview_cache.clear()
        _preview_cache[key] = png
    return png


def delete_saved(map_id):
    m = re.match(r'^saved:([A-Za-z0-9_-]{1,40})$', map_id or '')
    if not m:
        raise ValueError('only maps saved from the panel can be deleted')
    ypath = os.path.join(config.SAVED_MAPS_DIR, m.group(1) + '.yaml')
    if not os.path.isfile(ypath):
        raise ValueError('no such map')
    image = os.path.join(config.SAVED_MAPS_DIR, _parse_yaml(ypath).get('image', ''))
    for p in (ypath, image):
        if os.path.isfile(p) and os.path.dirname(os.path.abspath(p)) == os.path.abspath(config.SAVED_MAPS_DIR):
            os.unlink(p)


def save_argv(name):
    if not NAME_RE.match(name or ''):
        raise ValueError('map name: letters, digits, - and _ only (max 40)')
    return ['ros2', 'run', 'nav2_map_server', 'map_saver_cli', '-f', os.path.join(config.SAVED_MAPS_DIR, name)]
