"""Drawing and animation logic for Lumi's two eye displays.

Pure Python + Pillow: no ROS, no SPI, no display hardware, so it can be unit-tested and rendered to
pictures on any machine. eyes_node.py is the thin layer that connects it to the topic and the screens.

Geometry. Each 320x240 panel sits behind a ROUND hole in the face. Only the circle of diameter 180
centred on the panel is visible, and the original expressions were designed to fill it exactly
(box 70,30 -> 250,210). Everything here is drawn in a 180x180 square with that circle as a hard
boundary (a circular mask is applied last, so nothing can ever leak outside the hole).

Rendering. Shapes are drawn 3x larger and shrunk with LANCZOS (anti-aliased curves on a small TFT),
blinks squash the finished picture vertically (works for every expression, no special cases), and
animated expressions (heart beat, spinning star/spiral, breathing sleep) are functions of time.
"""
import math
import random

from PIL import Image, ImageDraw

SIZE = 180                 # the visible round area, in pixels (a 180x180 square holding a circle)
SS = 3                     # supersampling factor
BG = (0, 0, 0)
MIN_OPEN = 0.07            # a "closed" eye is a thin line, never fully gone

# ── colours ──────────────────────────────────────────────────────────────────
COLORS = {
    'neutral':   (0, 150, 255),     # the original Lumi blue
    'happy':     (255, 196, 40),    # warm yellow
    'sad':       (70, 100, 215),    # deeper blue
    'angry':     (255, 55, 45),     # red
    'surprised': (255, 255, 255),   # white
    'love':      (255, 60, 120),    # pink-red
    'excited':   (255, 214, 0),     # gold
    'dizzy':     (175, 95, 255),    # purple
    'sleepy':    (95, 115, 175),    # dim blue-grey
    'wink':      (255, 196, 40),    # same warm yellow as happy
}
# other names that mean the same thing
ALIASES = {
    'joy': 'happy', 'smile': 'happy', 'mad': 'angry', 'shocked': 'surprised', 'calm': 'neutral', 'idle': 'neutral',
    'heart': 'love', 'hearts': 'love', 'heart_eyes': 'love', 'heart-eyes': 'love', 'in_love': 'love',
    'star': 'excited', 'stars': 'excited', 'star_eyes': 'excited', 'star-eyes': 'excited',
    'sleep': 'sleepy', 'sleeping': 'sleepy', 'tired': 'sleepy', 'confused': 'dizzy',
}
EMOTIONS = tuple(COLORS)
ANIMATED = frozenset({'love', 'excited', 'dizzy', 'sleepy'})        # change with time even when nothing is requested
ASYMMETRIC = frozenset({'angry', 'sad', 'wink'})                           # left and right eye look different
NO_IDLE_BLINK = frozenset({'sleepy', 'wink', 'dizzy'})              # a blink would look odd on these
TRANSIENT = {'wink': 1.4}                                           # seconds, then back to the previous expression


def canonical(name):
    """Normalised emotion name, or None if we do not know it. 'blink' is handled by the animator."""
    n = (name or '').strip().lower().replace(' ', '_')
    n = ALIASES.get(n, n)
    return n if n in COLORS else None


def color_of(emotion):
    return COLORS.get(emotion, COLORS['neutral'])


def lerp_color(a, b, k):
    k = max(0.0, min(1.0, k))
    return tuple(int(round(a[i] + (b[i] - a[i]) * k)) for i in range(3))


def _scaled(color, k):
    return tuple(max(0, min(255, int(c * k))) for c in color)


# ── drawing helpers (local units: the 180x180 square, circle radius 90 at (90, 90)) ─────────────────────
class _Canvas:
    def __init__(self):
        self.img = Image.new('RGB', (SIZE * SS, SIZE * SS), BG)
        self.d = ImageDraw.Draw(self.img)

    @staticmethod
    def s(v):
        return v * SS

    def box(self, b):
        return [v * SS for v in b]

    def pts(self, ps):
        return [(x * SS, y * SS) for x, y in ps]


def _heart_unit():
    """Heart outline, centred and scaled so its farthest point is at radius 1."""
    raw = []
    for i in range(0, 361, 3):
        t = math.radians(i)
        raw.append((16 * math.sin(t) ** 3,
                    -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))))
    xs, ys = [p[0] for p in raw], [p[1] for p in raw]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    pts = [(x - cx, y - cy) for x, y in raw]
    r = max(math.hypot(x, y) for x, y in pts)
    return [(x / r, y / r) for x, y in pts]


_HEART = _heart_unit()


def _star_points(cx, cy, r_out, r_in, rot_deg, n=5):
    out = []
    for i in range(n * 2):
        a = math.radians(rot_deg - 90 + i * 180.0 / n)
        r = r_out if i % 2 == 0 else r_in
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out


# ── expressions ────────────────────────────────────────────────────────────────────────────────────
def _mx(side, x):
    """Mirror an x coordinate for the right-hand eye, so 'inner' and 'outer' stay on the right sides of the face."""
    return x if side == 'L' else SIZE - x


def _shine(c, col, x, y, rx, ry=None, k=0.72):
    """A soft highlight, the same trick the heart uses: it is what makes a flat shape read as a glossy eye."""
    ry = rx if ry is None else ry
    c.d.ellipse(c.box([x - rx, y - ry, x + rx, y + ry]), fill=lerp_color(col, (255, 255, 255), k))


def _neutral(c, col, side, t):        # a friendly, glossy oval eye
    c.d.rounded_rectangle(c.box([34, 16, 146, 164]), radius=c.s(56), fill=col)
    c.d.rounded_rectangle(c.box([46, 28, 134, 96]), radius=c.s(44), fill=lerp_color(col, (255, 255, 255), 0.16))
    _shine(c, col, 64, 52, 14, 19)
    _shine(c, col, 112, 130, 6)


def _crescent_up(c, col):             # wink's closed eye: upper crescent  ^
    c.d.ellipse(c.box([0, 0, 180, 180]), fill=col)
    c.d.ellipse(c.box([-10, 30, 190, 230]), fill=BG)


def _happy(c, col, side, t):          # a joyful closed eye: one round-capped arch  ^
    cx, cy, r, w = 90, 138, 70, 26
    c.d.arc(c.box([cx - r - w // 2, cy - r - w // 2, cx + r + w // 2, cy + r + w // 2]), start=198, end=342,
            fill=col, width=c.s(w))
    for a in (198, 342):                                      # round the two ends off
        ex, ey = cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))
        c.d.ellipse(c.box([ex - w / 2, ey - w / 2, ex + w / 2, ey + w / 2]), fill=col)


def _sad(c, col, side, t):            # drooping upper lid (outer side lower), a highlight and a tear
    c.d.ellipse(c.box([22, 30, 158, 166]), fill=col)
    lid = [(-10, -10), (190, -10), (190, 38), (-10, 92)]      # inner end high, outer end low (left eye)
    c.d.polygon(c.pts([(_mx(side, x), y) for x, y in lid]), fill=BG)
    _shine(c, col, _mx(side, 104), 120, 11, 14)
    tx, ty = _mx(side, 36), 146                                # the tear, on the outer side
    c.d.polygon(c.pts([(tx, ty - 26), (tx - 9, ty - 4), (tx + 9, ty - 4)]), fill=lerp_color(col, (255, 255, 255), 0.55))
    c.d.ellipse(c.box([tx - 9.5, ty - 12, tx + 9.5, ty + 7]), fill=lerp_color(col, (255, 255, 255), 0.55))


def _angry(c, col, side, t):          # a hard brow slanting down towards the nose over a fierce pupil
    c.d.ellipse(c.box([16, 24, 164, 172]), fill=col)
    brow = [(-10, -10), (190, -10), (190, 100), (-10, 20)]    # inner end low, outer end high (left eye)
    c.d.polygon(c.pts([(_mx(side, x), y) for x, y in brow]), fill=BG)
    px = _mx(side, 98)
    c.d.ellipse(c.box([px - 20, 112, px + 20, 152]), fill=_scaled(col, 0.42))


def _surprised(c, col, side, t):      # wide open: white eye, black pupil, a pair of glints
    c.d.ellipse(c.box([12, 12, 168, 168]), fill=col)
    c.d.ellipse(c.box([54, 54, 126, 126]), fill=BG)
    c.d.ellipse(c.box([64, 62, 82, 80]), fill=col)
    c.d.ellipse(c.box([100, 100, 110, 110]), fill=col)


def _love(c, col, side, t):           # beating heart with a small highlight
    pulse = 1.0 + 0.10 * (0.5 + 0.5 * math.sin(t * 2 * math.pi * 1.2))
    r = 80 * pulse                    # at the top of the beat: 88 < 90, still inside the hole
    c.d.polygon(c.pts([(90 + x * r, 92 + y * r * 0.98) for x, y in _HEART]), fill=col)
    shine = _scaled(lerp_color(col, (255, 255, 255), 0.7), 1.0)
    c.d.ellipse(c.box([36, 38, 58, 56]), fill=shine)


def _excited(c, col, side, t):        # twinkling star, slowly turning
    r_out = 80 * (1.0 + 0.06 * math.sin(t * 2 * math.pi * 1.5))
    c.d.polygon(c.pts(_star_points(90, 92, r_out, r_out * 0.45, t * 25)), fill=col)


def _dizzy(c, col, side, t):          # spinning spiral
    pts, n = [], 96
    rot = -t * 300.0
    for i in range(n + 1):
        k = i / n
        a = math.radians(rot + k * 3.2 * 360.0)
        r = 5 + 74 * k
        pts.append((90 + r * math.cos(a), 90 + r * math.sin(a)))
    c.d.line(c.pts(pts), fill=col, width=c.s(11), joint='curve')


def _wink(c, col, side, t):
    if side == 'R':
        _crescent_up(c, col)          # the winking eye
    else:
        c.d.ellipse(c.box([22, 22, 158, 158]), fill=col)     # the open eye


def _sleepy(c, col, side, t):         # closed eyes: a soft downward arc that "breathes"
    col = _scaled(col, 0.72 + 0.28 * math.sin(t * 2 * math.pi * 0.35))
    box = [16, 25, 164, 125]
    c.d.arc(c.box(box), start=22, end=158, fill=col, width=c.s(14))
    cx, cy, rx, ry = 90, 75, 74, 50
    for a in (22, 158):
        ex, ey = cx + rx * math.cos(math.radians(a)), cy + ry * math.sin(math.radians(a))
        c.d.ellipse(c.box([ex - 7, ey - 7, ex + 7, ey + 7]), fill=col)


_DRAW = {
    'neutral': _neutral, 'happy': _happy, 'sad': _sad, 'angry': _angry, 'surprised': _surprised,
    'love': _love, 'excited': _excited, 'dizzy': _dizzy, 'wink': _wink, 'sleepy': _sleepy,
}

_MASK = None


def _circle_mask():
    global _MASK
    if _MASK is None:
        big = Image.new('L', (SIZE * SS, SIZE * SS), 0)
        ImageDraw.Draw(big).ellipse([0, 0, SIZE * SS - 1, SIZE * SS - 1], fill=255)
        _MASK = big.resize((SIZE, SIZE), Image.LANCZOS)
    return _MASK


def raw(emotion, side='L', color=None, openness=1.0, t=0.0):
    """The eye BEFORE the circular mask (used by tests to prove the drawing itself fits the hole)."""
    fn = _DRAW.get(emotion, _neutral)
    c = _Canvas()
    fn(c, color or color_of(emotion), side, t)
    openness = max(MIN_OPEN, min(1.0, openness))
    img = c.img.reduce(SS)                         # exact box-filter downscale (fast; SS is an integer)
    if openness < 0.999:                           # blink: squash vertically, keep centred
        h = max(2, int(round(SIZE * openness)))
        squashed = img.resize((SIZE, h), Image.BILINEAR)
        img = Image.new('RGB', (SIZE, SIZE), BG)
        img.paste(squashed, (0, (SIZE - h) // 2))
    return img


def render(emotion, side='L', color=None, openness=1.0, t=0.0):
    """One eye as a SIZE x SIZE RGB picture (the round hole's square). openness 1 = open, ~0 = closed."""
    return Image.composite(raw(emotion, side, color, openness, t), Image.new('RGB', (SIZE, SIZE), BG), _circle_mask())


# ── animation state machine ───────────────────────────────────────────────────────────────────────
class Frame:
    __slots__ = ('emotion', 'color', 'openness', 't', 'dirty')

    def __init__(self, emotion, color, openness, t, dirty):
        self.emotion, self.color, self.openness, self.t, self.dirty = emotion, color, openness, t, dirty


class EyeAnimator:
    """Decides, for any moment in time, what both eyes should show.

    Changing expression is a quick blink: the eyes close (CLOSE_S), the picture is swapped while they are
    shut, they open again (OPEN_S) - with the colour fading across the whole thing. A plain 'blink' does
    the same without changing the expression, and idle blinks happen every few seconds on their own.
    Time is passed in (never read here) so the logic is exactly testable.
    """
    CLOSE_S = 0.11
    OPEN_S = 0.17
    BLINK_EVERY = (3.2, 6.5)

    def __init__(self, now=0.0, emotion='neutral', idle_blink=True, transitions=True, rng=None):
        self.rng = rng or random.Random()
        self.idle_blink, self.transitions = idle_blink, transitions
        self.shown = emotion                 # what is drawn right now
        self.target = emotion                # what we are heading to
        self.base = emotion                  # what to go back to after a transient expression (wink)
        self.phase = 'idle'                  # idle | closing | opening
        self.p_start, self.p_dur, self.p_from, self.p_to = now, 0.0, 1.0, 1.0
        self.blink_only = False
        self.c_from = self.c_to = color_of(emotion)
        self.c_start, self.c_dur = now, 0.0
        self.shown_at = now
        self.t0 = now
        self._last_key = None
        self._next_blink = now + self.rng.uniform(*self.BLINK_EVERY)

    # -- helpers
    def _openness(self, now):
        if self.phase == 'idle':
            return 1.0
        k = 1.0 if self.p_dur <= 0 else min(1.0, max(0.0, (now - self.p_start) / self.p_dur))
        k = k * k * (3 - 2 * k)              # smoothstep
        return self.p_from + (self.p_to - self.p_from) * k

    def _color(self, now):
        if self.c_dur <= 0:
            return self.c_to
        return lerp_color(self.c_from, self.c_to, (now - self.c_start) / self.c_dur)

    def _start(self, phase, now, to_open, dur):
        cur = self._openness(now)
        self.phase, self.p_start, self.p_from, self.p_to = phase, now, cur, to_open
        self.p_dur = dur * (abs(to_open - cur) / (1.0 - MIN_OPEN)) if dur else 0.0

    # -- requests
    def request(self, emotion, now):
        """Change expression. Returns False if it is already showing/heading there."""
        emotion = canonical(emotion)
        if emotion is None or (emotion == self.target and not self.blink_only):
            return False
        if emotion in TRANSIENT and self.target not in TRANSIENT:
            self.base = self.target
        elif emotion not in TRANSIENT:
            self.base = emotion
        self.target, self.blink_only = emotion, False
        cur_col = self._color(now)
        if not self.transitions:
            self.shown, self.shown_at, self.phase = emotion, now, 'idle'
            self.c_from = self.c_to = color_of(emotion)
            self.c_dur = 0.0
            return True
        self.c_from, self.c_to, self.c_start = cur_col, color_of(emotion), now
        self.c_dur = self.CLOSE_S + self.OPEN_S
        if self.phase != 'closing':
            self._start('closing', now, MIN_OPEN, self.CLOSE_S)
        return True

    def blink(self, now):
        if self.phase == 'idle':
            self.blink_only = True
            self._start('closing', now, MIN_OPEN, self.CLOSE_S)

    # -- time
    def frame(self, now):
        # phase changes
        if self.phase == 'closing' and now >= self.p_start + self.p_dur:
            if not self.blink_only and self.shown != self.target:
                self.shown, self.shown_at = self.target, now
            self._start('opening', self.p_start + self.p_dur, 1.0, self.OPEN_S)
        if self.phase == 'opening' and now >= self.p_start + self.p_dur:
            self.phase, self.blink_only = 'idle', False
            self._next_blink = now + self.rng.uniform(*self.BLINK_EVERY)
        if self.phase == 'idle':
            if self.shown in TRANSIENT and now - self.shown_at >= TRANSIENT[self.shown]:
                self.request(self.base, now)
            elif self.idle_blink and self.shown not in NO_IDLE_BLINK and now >= self._next_blink:
                self.blink(now)
        color = self._color(now)
        openness = self._openness(now)
        t = now - self.t0
        key = (self.shown, color, round(openness, 3))
        dirty = self.phase != 'idle' or self.shown in ANIMATED or key != self._last_key
        self._last_key = key
        return Frame(self.shown, color, openness, t, dirty)

    def busy(self):
        """True while frames must keep coming (a transition, or a moving expression)."""
        return self.phase != 'idle' or self.shown in ANIMATED

    def seconds_until_idle_blink(self, now):
        return max(0.0, self._next_blink - now) if self.idle_blink and self.shown not in NO_IDLE_BLINK else 3600.0
