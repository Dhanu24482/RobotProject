"""canvas_spec: whatever the model sends, the page only ever gets a small, harmless, well-formed picture."""
import json
import math

import pytest

from omni_base import canvas_spec as C

CIRCLE = {'title': 'Circumference of a circle', 'seconds': 30,
          'shapes': [{'kind': 'circle', 'x': 50, 'y': 50, 'r': 34, 'color': 'accent', 'fill': True},
                     {'kind': 'arrow', 'x': 50, 'y': 50, 'x2': 84, 'y2': 50, 'color': 'yellow'},
                     {'kind': 'text', 'x': 67, 'y': 43, 'text': 'r', 'size': 'large'}],
          'formula': 'C = 2πr', 'notes': ['r is the radius', 'π ≈ 3.14159']}


def reply(say='ok', canvas=None):
    return json.dumps({'say': say, 'canvas': canvas})


# ── the happy path ─────────────────────────────────────────────────────────────────────────────────
def test_a_good_spec_survives_intact():
    s = C.sanitize(CIRCLE)
    assert s['title'] == 'Circumference of a circle' and s['seconds'] == 30
    assert s['formula'] == 'C = 2πr' and s['notes'] == ['r is the radius', 'π ≈ 3.14159']
    assert [x['kind'] for x in s['shapes']] == ['circle', 'arrow', 'text']
    assert s['shapes'][0] == {'kind': 'circle', 'x': 50.0, 'y': 50.0, 'r': 34.0, 'color': 'accent', 'fill': True}
    assert s['shapes'][2]['size'] == 'large' and s['shapes'][2]['text'] == 'r'


def test_the_result_is_plain_json_data():
    json.dumps(C.sanitize(CIRCLE))


# ── nothing to show -> voice only ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('spec', [None, {}, [], 'a circle', 5, {'shapes': []}, {'title': 'only a title'},
                                  {'shapes': [{'kind': 'nonsense'}]}, {'shapes': 'circle'}])
def test_no_picture_when_there_is_nothing_to_draw(spec):
    assert C.sanitize(spec) is None


def test_null_canvas_means_voice_only():
    assert C.parse_ai_json(reply('Hello there.', None)) == ('Hello there.', None)


# ── numbers are clamped, junk is dropped ───────────────────────────────────────────────────────────
def test_numbers_are_clamped_into_the_drawing_area():
    s = C.sanitize({'shapes': [{'kind': 'circle', 'x': -50, 'y': 900, 'r': 5000}]})['shapes'][0]
    assert (s['x'], s['y'], s['r']) == (0.0, 100.0, 60.0)


@pytest.mark.parametrize('bad', ['abc', None, float('nan'), float('inf'), [], {}, True])
def test_a_shape_with_a_bad_number_is_dropped(bad):
    assert C.sanitize({'shapes': [{'kind': 'circle', 'x': bad, 'y': 5, 'r': 5}], 'formula': 'x'})['shapes'] == []


def test_numbers_given_as_strings_are_accepted():
    s = C.sanitize({'shapes': [{'kind': 'dot', 'x': '12.5', 'y': '70'}]})['shapes'][0]
    assert (s['x'], s['y']) == (12.5, 70.0)


# ── only whitelisted things come through ───────────────────────────────────────────────────────────
def test_unknown_kinds_fields_and_colours_are_removed():
    s = C.sanitize({'shapes': [{'kind': 'circle', 'x': 1, 'y': 1, 'r': 5, 'color': 'javascript:alert(1)',
                                'onclick': 'evil()', 'href': 'http://x', 'style': 'x', 'foo': 1},
                               {'kind': 'script', 'x': 1, 'y': 1},
                               {'kind': 'image', 'href': 'http://evil'}]})
    assert len(s['shapes']) == 1
    assert set(s['shapes'][0]) == {'kind', 'x', 'y', 'r'}


def test_markup_in_text_is_kept_as_plain_text_never_interpreted():
    s = C.sanitize({'shapes': [{'kind': 'text', 'x': 5, 'y': 5, 'text': '<script>x</script>'}],
                    'title': '<img src=x onerror=alert(1)>', 'formula': '<b>x</b>'})
    assert s['shapes'][0]['text'] == '<script>x</scr'                                  # truncated, still just text
    assert s['title'].startswith('<img') and C.clean_text('a\x00b\x1bc\nd', 20) == 'a b c d'


def test_limits_on_counts_and_lengths():
    spec = {'title': 'T' * 500, 'formula': 'F' * 500, 'notes': ['n' * 500] * 9,
            'shapes': [{'kind': 'dot', 'x': i, 'y': i} for i in range(100)]}
    s = C.sanitize(spec)
    assert len(s['shapes']) == C.MAX_SHAPES and len(s['notes']) == C.MAX_NOTES
    assert len(s['title']) == 50 and len(s['formula']) == 48 and all(len(n) == 70 for n in s['notes'])


def test_polygon_points_are_paired_and_capped_and_need_three():
    ok = C.sanitize({'shapes': [{'kind': 'poly', 'points': [10, 10, 90, 10, 50, 80], 'fill': True}]})['shapes'][0]
    assert ok['points'] == [10.0, 10.0, 90.0, 10.0, 50.0, 80.0] and ok['fill'] is True
    assert C.sanitize({'shapes': [{'kind': 'poly', 'points': [10, 10, 90, 10]}], 'formula': 'x'})['shapes'] == []
    many = C.sanitize({'shapes': [{'kind': 'poly', 'points': list(range(100))}]})['shapes'][0]
    assert len(many['points']) == C.MAX_POINTS * 2


def test_dashed_and_fill_only_where_they_make_sense():
    s = C.sanitize({'shapes': [{'kind': 'dot', 'x': 1, 'y': 1, 'dashed': True, 'fill': True},
                               {'kind': 'line', 'x': 1, 'y': 1, 'x2': 9, 'y2': 9, 'dashed': True, 'fill': True}]})['shapes']
    assert 'dashed' not in s[0] and 'fill' not in s[0]
    assert s[1].get('dashed') is True and 'fill' not in s[1]


def test_arc_angles_are_bounded():
    s = C.sanitize({'shapes': [{'kind': 'arc', 'x': 50, 'y': 50, 'r': 20, 'start': -9999, 'end': 9999}]})['shapes'][0]
    assert (s['start'], s['end']) == (-720.0, 720.0)


# ── how long it stays ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('given,expected', [(30, 30), (1, C.SECONDS_MIN), (0, C.SECONDS_MIN), (-5, C.SECONDS_MIN),
                                            (9999, C.SECONDS_MAX), ('45', 45), (None, C.SECONDS_DEFAULT),
                                            ('soon', C.SECONDS_DEFAULT), (float('nan'), C.SECONDS_DEFAULT),
                                            (True, C.SECONDS_DEFAULT), (12.6, 13)])
def test_the_ai_can_suggest_how_long_but_only_within_sane_bounds(given, expected):
    assert C.clamp_seconds(given) == expected
    spec = dict(CIRCLE, seconds=given)
    assert C.sanitize(spec)['seconds'] == expected


# ── reading the model's reply ──────────────────────────────────────────────────────────────────────
def test_say_and_canvas_are_split():
    say, canvas = C.parse_ai_json(reply('Multiply the diameter by pi.', CIRCLE))
    assert say == 'Multiply the diameter by pi.' and canvas['formula'] == 'C = 2πr'


def test_code_fences_and_chatter_around_the_json_are_tolerated():
    j = reply('Hi.', None)
    assert C.parse_ai_json('```json\n' + j + '\n```') == ('Hi.', None)
    assert C.parse_ai_json('Sure! Here you go: ' + j + ' Hope that helps.') == ('Hi.', None)


@pytest.mark.parametrize('plain', ['Just a plain answer.', 'Use {braces} like this', '', '  ', 'a } b { c'])
def test_plain_text_is_the_answer_and_never_goes_silent(plain):
    assert C.parse_ai_json(plain) == (plain.strip(), None)


def test_cut_off_json_never_reads_braces_aloud_and_keeps_the_sentence():
    cut = '{"say": "Multiply by pi.", "canvas": {"shapes": [{"kind": "circle", "x": 5'
    assert C.parse_ai_json(cut) == ('Multiply by pi.', None)
    assert C.parse_ai_json('{"say": "It is \\"nice\\" today.", "canvas": {"sha') == ('It is "nice" today.', None)


def test_cut_off_json_with_no_sentence_says_nothing_so_the_caller_can_apologise():
    assert C.parse_ai_json('{"canvas": {"shapes": [') == ('', None)


def test_json_that_is_not_an_object():
    assert C.parse_ai_json('[1, 2, 3]') == ('[1, 2, 3]', None)
    assert C.parse_ai_json('"hi"') == ('"hi"', None)


def test_missing_say_does_not_crash():
    assert C.parse_ai_json(json.dumps({'canvas': CIRCLE}))[0] == ''
    assert C.parse_ai_json(json.dumps({'answer': 'Four.'}))[0] == 'Four.'


def test_hostile_canvas_inside_a_valid_reply_is_neutralised():
    evil = {'shapes': [{'kind': 'circle', 'x': 'NaN', 'y': 5, 'r': 5}, {'kind': '__proto__'},
                       {'kind': 'text', 'x': 1, 'y': 1, 'text': '<svg onload=alert(1)>'}],
            'seconds': 1e9, 'title': 'x' * 9999}
    say, canvas = C.parse_ai_json(reply('Hello.', evil))
    assert say == 'Hello.' and len(canvas['shapes']) == 1 and canvas['seconds'] == C.SECONDS_MAX
    assert all(math.isfinite(v) for s in canvas['shapes'] for v in s.values() if isinstance(v, float))
