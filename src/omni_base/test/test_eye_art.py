"""Tests for the eye drawing + animation logic (pure Python, no ROS, no display)."""
import math
import random

import pytest

from omni_base import eye_art as A


# ── geometry: every expression, every moment, must fit the round hole by itself ────────────────────────
@pytest.mark.parametrize('emotion', A.EMOTIONS)
@pytest.mark.parametrize('side', ['L', 'R'])
def test_drawing_fits_the_round_hole_without_the_mask(emotion, side):
    r_max = A.SIZE / 2 + 1.5            # 1.5 px of anti-aliasing slack
    for t in (0.0, 0.17, 0.4, 0.83, 1.3):
        for openness in (1.0, 0.5, 0.1):
            img = A.raw(emotion, side, None, openness, t)
            px = img.load()
            for y in range(A.SIZE):
                for x in range(A.SIZE):
                    if px[x, y] != (0, 0, 0) and math.hypot(x + 0.5 - 90, y + 0.5 - 90) > r_max:
                        pytest.fail(f'{emotion}/{side} t={t} open={openness}: pixel {x},{y} is outside the hole')


def test_every_expression_actually_draws_something():
    for e in A.EMOTIONS:
        assert A.render(e, 'L').getbbox() is not None, e


def test_aliases():
    assert A.canonical('Heart Eyes') == 'love' and A.canonical('hearts') == 'love'
    assert A.canonical('SLEEPING') == 'sleepy' and A.canonical('stars') == 'excited'
    assert A.canonical('nonsense') is None and A.canonical('') is None and A.canonical(None) is None
    assert A.canonical('blink') is None            # handled by the animator, not an expression


# ── animator ──────────────────────────────────────────────────────────────────────────────────────────
def run(an, start, seconds, step=0.01):
    """Step the animator and return the list of (time, Frame)."""
    out, t = [], start
    while t <= start + seconds + 1e-9:
        out.append((t, an.frame(t)))
        t += step
    return out


def make(**kw):
    kw.setdefault('idle_blink', False)
    return A.EyeAnimator(now=0.0, rng=random.Random(1), **kw)


def test_change_is_close_swap_while_shut_open():
    an = make()
    assert an.request('happy', 0.0) is True
    frames = run(an, 0.0, 0.6)
    swap = next(t for t, f in frames if f.emotion == 'happy')
    assert swap >= A.EyeAnimator.CLOSE_S - 0.01, 'the picture must only change once the eyes are closed'
    open_at_swap = next(f.openness for t, f in frames if f.emotion == 'happy')
    assert open_at_swap <= A.MIN_OPEN + 0.05
    assert min(f.openness for _, f in frames) <= A.MIN_OPEN + 0.01
    assert frames[-1][1].openness == 1.0 and frames[-1][1].emotion == 'happy'
    total = A.EyeAnimator.CLOSE_S + A.EyeAnimator.OPEN_S
    assert any(f.openness < 1.0 for t, f in frames if t < total - 0.02) and frames[-1][0] > total


def test_openness_never_jumps():
    an = make()
    an.request('angry', 0.0)
    frames = run(an, 0.0, 0.6, step=0.005)
    for (_, a), (_, b) in zip(frames, frames[1:]):
        assert abs(a.openness - b.openness) < 0.12, 'a visible flash'


def test_colour_fades_across_the_transition_and_is_monotonic():
    an = make()
    an.request('angry', 0.0)
    frames = run(an, 0.0, 0.6)
    greens = [f.color[1] for _, f in frames]
    assert greens[0] == A.COLORS['neutral'][1] and greens[-1] == A.COLORS['angry'][1]
    assert all(b <= a + 1 for a, b in zip(greens, greens[1:])), 'neutral->angry green channel only falls'
    assert any(g not in (greens[0], greens[-1]) for g in greens), 'it must pass through in-between colours'


def test_same_or_unknown_request_does_nothing():
    an = make()
    assert an.request('neutral', 0.0) is False
    assert an.request('what', 0.0) is False
    assert an.frame(0.5).openness == 1.0


def test_blink_keeps_the_expression():
    an = make(emotion='sad')
    an.blink(1.0)
    frames = run(an, 1.0, 0.6)
    assert {f.emotion for _, f in frames} == {'sad'}
    assert min(f.openness for _, f in frames) <= A.MIN_OPEN + 0.01 and frames[-1][1].openness == 1.0


def test_request_in_the_middle_of_a_transition_is_smooth_and_lands_on_the_last_request():
    an = make()
    an.request('happy', 0.0)
    an.frame(0.20)                         # already re-opening
    an.request('surprised', 0.20)
    frames = run(an, 0.20, 0.8, step=0.005)
    for (_, a), (_, b) in zip(frames, frames[1:]):
        assert abs(a.openness - b.openness) < 0.12
    assert frames[-1][1].emotion == 'surprised' and frames[-1][1].openness == 1.0


def test_wink_returns_to_what_it_was():
    an = make(emotion='sad')
    an.request('wink', 0.0)
    f = run(an, 0.0, 0.8)[-1][1]
    assert f.emotion == 'wink'
    f = run(an, 0.8, 3.0)[-1][1]
    assert f.emotion == 'sad' and f.openness == 1.0
    an.request('love', 5.0)
    run(an, 5.0, 0.8)
    an.request('wink', 6.0)
    assert run(an, 6.0, 4.0)[-1][1].emotion == 'love'


def test_idle_blinks_happen_on_their_own_and_only_where_they_look_right():
    an = A.EyeAnimator(now=0.0, rng=random.Random(3), idle_blink=True)
    fr = run(an, 0.0, 30.0, step=0.02)
    blinks = sum(1 for (_, a), (_, b) in zip(fr, fr[1:]) if a.openness > 0.9 and b.openness <= 0.9)
    assert 4 <= blinks <= 10, f'{blinks} blinks in 30 s'
    assert {f.emotion for _, f in fr} == {'neutral'}
    for e in A.NO_IDLE_BLINK:
        an = A.EyeAnimator(now=0.0, emotion=e, rng=random.Random(3), idle_blink=True)
        assert all(f.openness == 1.0 for _, f in run(an, 0.0, 30.0, step=0.05)), e


def test_idle_blink_can_be_switched_off():
    an = make()
    assert all(f.openness == 1.0 for _, f in run(an, 0.0, 60.0, step=0.1))


def test_no_transitions_mode_is_instant():
    an = make(transitions=False)
    an.request('love', 0.0)
    f = an.frame(0.0)
    assert f.emotion == 'love' and f.openness == 1.0 and f.color == A.COLORS['love']


def test_frames_are_only_flagged_dirty_when_something_changed():
    an = make()
    assert an.frame(0.0).dirty is True            # first picture
    assert an.frame(0.5).dirty is False           # static + idle: nothing to push
    assert an.busy() is False
    an.request('excited', 1.0)
    assert an.frame(1.05).dirty is True and an.busy() is True
    run(an, 1.05, 1.0)
    assert an.frame(2.5).dirty is True            # excited keeps moving
    assert an.busy() is True


def test_colour_lerp_edges():
    assert A.lerp_color((0, 0, 0), (255, 100, 50), 0) == (0, 0, 0)
    assert A.lerp_color((0, 0, 0), (255, 100, 50), 1) == (255, 100, 50)
    assert A.lerp_color((0, 0, 0), (255, 100, 50), 5) == (255, 100, 50)


# ── the "send only the round hole" optimisation must land on exactly the same panel pixels ─────────────
def test_sending_only_the_hole_equals_sending_the_whole_frame():
    """Mimics adafruit_rgb_display's image(): with rotation=90 it rotates the picture a quarter turn
    (expand=True) into the panel's native frame and writes it at (x, y)."""
    from PIL import Image
    W, H = 320, 240
    hole_x, hole_y = 70, 30
    native_x, native_y = 30, 70                      # the constants eyes_node uses
    eye = A.render('love', 'L', None, 1.0, 0.3)

    full = Image.new('RGB', (W, H), (0, 0, 0))
    full.paste(eye, (hole_x, hole_y))
    native_full = full.rotate(90, expand=True)       # what the driver sends for a full frame (240 x 320)

    native_crop = eye.rotate(90, expand=True)        # what it sends for the cropped square
    expected = native_full.crop((native_x, native_y, native_x + A.SIZE, native_y + A.SIZE))
    assert list(native_crop.getdata()) == list(expected.getdata())

    # and nothing of the picture lies outside that square
    outside = native_full.copy()
    outside.paste((0, 0, 0), (native_x, native_y, native_x + A.SIZE, native_y + A.SIZE))
    assert outside.getbbox() is None
