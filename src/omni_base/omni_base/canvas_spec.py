"""The little picture Lumi can show on the voice page next to a spoken answer.

Pure Python, no ROS: shared by voice_node (which receives it from Gemini) and remote_node (which hands it to the
browser), so it can be unit-tested anywhere.

The model never sends HTML, SVG or code. It sends DATA: a handful of whitelisted shapes with numbers, a title, a
formula line, a few notes and how many seconds the picture should stay. Everything here is clamped, truncated and
filtered, and the page validates it AGAIN before drawing, so a confused or hostile model answer can only ever
produce a harmless drawing.

    {"title": "...", "seconds": 25,
     "shapes": [{"kind": "circle", "x": 50, "y": 50, "r": 30, "color": "accent", "fill": true}, ...],
     "formula": "C = 2πr", "notes": ["r is the radius", ...]}

The drawing area is 100 x 100 with (0, 0) at the top left.
"""
import json
import math
import re

MAX_SHAPES = 16
MAX_POINTS = 8                  # per polygon
MAX_NOTES = 3
KINDS = ('circle', 'line', 'arrow', 'rect', 'arc', 'dot', 'poly', 'text')
COLORS = ('accent', 'green', 'yellow', 'red', 'white')
SIZES = ('small', 'normal', 'large')

SECONDS_MIN = 8
SECONDS_MAX = 90
SECONDS_DEFAULT = 20

_CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f\u2028\u2029]')
_SPACES = re.compile(r'\s+')


def clean_text(value, limit):
    """A short single line of plain text, or ''. Never markup: the page also sets it with textContent."""
    if not isinstance(value, str):
        return ''
    return _SPACES.sub(' ', _CONTROL.sub(' ', value)).strip()[:limit]


def _num(value, lo, hi):
    if isinstance(value, bool):                    # True is not 1
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(min(hi, max(lo, v)), 1)


def clamp_seconds(value):
    v = _num(value, SECONDS_MIN, SECONDS_MAX)
    return SECONDS_DEFAULT if v is None else int(round(v))


def _shape(raw):
    if not isinstance(raw, dict):
        return None
    kind = raw.get('kind')
    if kind not in KINDS:
        return None
    out = {'kind': kind}

    def need(*names, **limits):
        """Copy the named numbers; the whole shape is dropped if one is missing or not a number."""
        for n in names:
            lo, hi = limits.get(n, (0, 100))
            v = _num(raw.get(n), lo, hi)
            if v is None:
                return False
            out[n] = v
        return True

    if kind in ('circle', 'dot'):
        ok = need('x', 'y') and (kind == 'dot' or need('r', r=(0.5, 60)))
    elif kind in ('line', 'arrow'):
        ok = need('x', 'y', 'x2', 'y2')
    elif kind == 'rect':
        ok = need('x', 'y', 'w', 'h')
    elif kind == 'arc':
        ok = need('x', 'y', 'r', 'start', 'end', r=(0.5, 60), start=(-720, 720), end=(-720, 720))
    elif kind == 'poly':
        flat = raw.get('points')
        pts = []
        if isinstance(flat, list):
            nums = [_num(v, 0, 100) for v in flat[:MAX_POINTS * 2]]
            pts = [nums[i:i + 2] for i in range(0, len(nums) - 1, 2)]
        pts = [p for p in pts if None not in p]
        ok = len(pts) >= 3
        out['points'] = [c for p in pts for c in p]
    else:                                          # text
        text = clean_text(raw.get('text'), 14)
        ok = bool(text) and need('x', 'y')
        out['text'] = text
        size = raw.get('size')
        if size in SIZES and size != 'normal':
            out['size'] = size
    if not ok:
        return None
    if raw.get('color') in COLORS:
        out['color'] = raw['color']
    if raw.get('dashed') is True and kind in ('circle', 'line', 'arrow', 'rect', 'arc', 'poly'):
        out['dashed'] = True
    if raw.get('fill') is True and kind in ('circle', 'rect', 'poly'):
        out['fill'] = True
    return out


def sanitize(spec):
    """The safe version of a picture spec, or None if there is nothing worth showing."""
    if not isinstance(spec, dict):
        return None
    shapes = []
    raw_shapes = spec.get('shapes')
    if isinstance(raw_shapes, list):
        for raw in raw_shapes[:MAX_SHAPES * 2]:      # look past a few bad ones, keep at most MAX_SHAPES good ones
            s = _shape(raw)
            if s:
                shapes.append(s)
            if len(shapes) >= MAX_SHAPES:
                break
    notes = []
    raw_notes = spec.get('notes')
    if isinstance(raw_notes, list):
        notes = [t for t in (clean_text(n, 70) for n in raw_notes[:MAX_NOTES * 2]) if t][:MAX_NOTES]
    out = {
        'title': clean_text(spec.get('title'), 50),
        'seconds': clamp_seconds(spec.get('seconds')),
        'shapes': shapes,
        'formula': clean_text(spec.get('formula'), 48),
        'notes': notes,
    }
    if not (out['shapes'] or out['formula'] or out['notes']):
        return None
    return out


_SAY = re.compile(r'"say"\s*:\s*"((?:[^"\\]|\\.)*)"')
_FENCE = re.compile(r'^```(?:json)?\s*(.*?)\s*```$', re.S | re.I)


def parse_ai_json(text):
    """Split the model's reply into (what to say aloud, safe picture spec or None).

    The model is asked for {"say": "...", "canvas": null | {...}}. If it ignores that and answers in plain text, the
    plain text IS the answer and there is no picture, so a model that misbehaves can never make Lumi go silent.
    """
    raw = (text or '').strip()
    body = raw
    m = _FENCE.match(body)
    if m:
        body = m.group(1)
    if not body.startswith('{'):
        i, j = body.find('{'), body.rfind('}')
        if i < 0 or j <= i:
            return raw, None
        body = body[i:j + 1]
    try:
        obj = json.loads(body)
    except ValueError:
        # Cut off or malformed JSON (for example a very long answer): never read braces and quotes aloud. Salvage the
        # spoken sentence if it is there, otherwise say nothing here and let the caller use its own fallback.
        m = _SAY.search(body)
        if m:
            try:
                return json.loads('"' + m.group(1) + '"').strip(), None
            except ValueError:
                return m.group(1).strip(), None
        if raw.startswith('{') or _FENCE.match(raw):
            return '', None                          # it WAS meant to be JSON, so it is not something to read out
        return raw, None                             # ordinary words that happen to contain braces
    if not isinstance(obj, dict):
        return raw, None
    say = obj.get('say')
    if not isinstance(say, str) or not say.strip():
        say = obj.get('answer') if isinstance(obj.get('answer'), str) else ''
    return say.strip(), sanitize(obj.get('canvas'))
