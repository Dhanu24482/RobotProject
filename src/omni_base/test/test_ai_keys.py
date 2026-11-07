"""AIClient key + model handling: which (key, model) pairs the robot uses and which it skips (no network, no real keys)."""
import time

import pytest

pytest.importorskip('rclpy')
from omni_base import voice_node as V  # noqa: E402


class Log:
    def __init__(self):
        self.lines = []

    def warn(self, m):
        self.lines.append(('warn', m))

    def error(self, m):
        self.lines.append(('error', m))

    info = warn

    def text(self):
        return ' '.join(m for _, m in self.lines)


class FakeChat:
    """Answers, or raises the given error. `calls` counts attempts."""

    def __init__(self, outcome):
        self.outcome, self.calls = outcome, 0

    def send_message(self, q):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome

        class R:
            text = self.outcome
        return R()


class Chats(dict):
    """chats[key] means (key, first model); chats[(key, model)] is explicit."""
    first = None

    def __missing__(self, k):
        return dict.__getitem__(self, (k, self.first))


def make(monkeypatch, outcomes, models=('m1',)):
    """An AIClient whose keys k0, k1, ... behave as `outcomes` says.

    Each outcome is a string (works), an exception (fails), or {model: outcome} to differ per model."""
    keys = ['k%d' % i for i in range(len(outcomes))]
    monkeypatch.setenv('GEMINI_API_KEYS', ','.join(keys))
    monkeypatch.setenv('GEMINI_MODELS', ','.join(models))
    chats = Chats()
    chats.first = models[0]
    for k, o in zip(keys, outcomes):
        for m in models:
            chats[(k, m)] = FakeChat(o[m] if isinstance(o, dict) else o)

    def build(self, key, model):
        self.chat = chats[(key, model)]
        return True
    monkeypatch.setattr(V.AIClient, '_build_client', build)
    log = Log()
    return V.AIClient(log), chats, log


E429 = Exception('429 RESOURCE_EXHAUSTED: You exceeded your current quota')
E429_DAY = Exception("429 RESOURCE_EXHAUSTED. {'violations': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}")
E429_MINUTE = Exception("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED', 'details': ["
                        "{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaId': "
                        "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}, "
                        "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '29s'}]}}")
E503 = Exception('503 UNAVAILABLE: This model is currently experiencing high demand')
E400 = Exception('400 INVALID_ARGUMENT: API key not valid. Please pass a valid API key. API_KEY_INVALID')
E403 = Exception('403 PERMISSION_DENIED: forbidden')
E404 = Exception('404 NOT_FOUND: This model models/m1 is no longer available to new users.')
EOTHER = Exception('ValueError: weird prompt')


# ── classification ─────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('err,kind', [(E429, 'rate'), (E429_DAY, 'rate'), (E503, 'busy'), (E400, 'rejected'),
                                      (E403, 'rejected'), (E404, 'gone'), (EOTHER, 'other'),
                                      (Exception('Deadline exceeded'), 'busy')])
def test_classification(err, kind):
    assert V.AIClient._classify(err) == kind


def test_status_codes_are_whole_numbers_only():
    # a retry delay / id that merely CONTAINS 401, 403, 404 or 500 must not read as that HTTP status
    assert V.AIClient._classify(Exception('weird prompt retryDelay 85401s id=1403 port=9404 value 5000')) == 'other'


# ── keys (single model) ────────────────────────────────────────────────────────────────────────────
def test_a_working_key_answers_and_is_kept(monkeypatch):
    ai, chats, _ = make(monkeypatch, ['hello', 'never used'])
    assert ai.ask('hi') == 'hello'
    assert chats['k1'].calls == 0


def test_quota_exhausted_key_is_skipped_and_the_next_one_answers(monkeypatch):
    ai, chats, log = make(monkeypatch, [E429, 'from k1'])
    assert ai.ask('hi') == 'from k1'
    assert 'k0' not in log.text()                                                      # never logs a key


def test_rejected_key_is_skipped_for_hours_not_minutes(monkeypatch):
    ai, chats, _ = make(monkeypatch, [E400, 'from k1'])
    assert ai.ask('hi') == 'from k1'
    assert ai.cooldowns[('k0', 'm1')] - time.time() > 3600


def test_invalid_key_no_longer_poisons_every_answer(monkeypatch):
    ai, chats, _ = make(monkeypatch, [E400, 'ok'])
    assert [ai.ask('a'), ai.ask('b'), ai.ask('c')] == ['ok', 'ok', 'ok']
    assert chats['k0'].calls == 1                                  # tried once, then left alone


def test_skips_several_bad_keys_in_a_row(monkeypatch):
    ai, chats, _ = make(monkeypatch, [E429, E400, E403, E429, 'the good one'])
    assert ai.ask('hi') == 'the good one'


def test_overloaded_model_gives_up_after_two_tries_not_all_keys(monkeypatch):
    ai, chats, _ = make(monkeypatch, [E503] * 6)
    assert 'busy' in ai.ask('hi').lower()
    assert sum(c.calls for c in chats.values()) == V.AIClient.BUSY_TRIES


def test_all_keys_exhausted(monkeypatch):
    ai, _, _ = make(monkeypatch, [E429, E429])
    assert 'busy' in ai.ask('hi').lower()


def test_all_keys_rejected_says_so(monkeypatch):
    ai, _, _ = make(monkeypatch, [E400, E403])
    assert 'none of my ai keys' in ai.ask('hi').lower()


def test_other_errors_do_not_burn_keys(monkeypatch):
    ai, chats, _ = make(monkeypatch, [EOTHER, 'unused'])
    assert 'could not process' in ai.ask('hi')
    assert chats['k1'].calls == 0 and not ai.cooldowns


def test_cooldown_grows_for_a_key_that_keeps_failing(monkeypatch):
    ai, _, _ = make(monkeypatch, [E429, 'ok'])
    ai.ask('1')
    first = ai.cooldowns[('k0', 'm1')] - time.time()
    ai.cooldowns.clear()
    ai.idx['m1'] = 0
    ai._activate()
    ai.ask('2')
    assert ai.cooldowns[('k0', 'm1')] - time.time() > first * 1.5


def test_success_resets_the_streak(monkeypatch):
    ai, _, _ = make(monkeypatch, ['fine'])
    ai.fail_streak[('k0', 'm1')] = 3
    ai.ask('hi')
    assert ai.fail_streak[('k0', 'm1')] == 0


def test_empty_answer_is_handled(monkeypatch):
    ai, _, _ = make(monkeypatch, [''])
    assert 'not sure' in ai.ask('hi')


# ── Google's own retry hints ───────────────────────────────────────────────────────────────────────
def test_per_minute_limit_uses_googles_retry_delay(monkeypatch):
    ai, _, _ = make(monkeypatch, ['x'])
    assert ai._classify(E429_MINUTE) == 'rate'
    assert ai._rate_cooldown(E429_MINUTE, 1) == pytest.approx(31.0)          # 29 s + 2 s margin, not a blind 60 s


def test_daily_quota_waits_long(monkeypatch):
    ai, _, _ = make(monkeypatch, ['x'])
    assert ai._rate_cooldown(E429_DAY, 1) == V.AIClient.KEY_COOLDOWN_MAX


def test_unknown_rate_limit_backs_off_progressively(monkeypatch):
    ai, _, _ = make(monkeypatch, ['x'])
    assert [ai._rate_cooldown(E429, n) for n in (1, 2, 3)] == [60.0, 120.0, 240.0]
    assert ai._rate_cooldown(E429, 20) == V.AIClient.KEY_COOLDOWN_MAX


def test_retry_delay_is_clamped(monkeypatch):
    ai, _, _ = make(monkeypatch, ['x'])
    assert ai._rate_cooldown(Exception("429 retryDelay': '1s'"), 1) == 5.0
    assert ai._rate_cooldown(Exception("429 retryDelay': '9000s'"), 1) == 300.0


# ── models: the fallback that matters when a daily quota is spent ──────────────────────────────────
def test_default_models_and_override(monkeypatch):
    monkeypatch.delenv('GEMINI_MODELS', raising=False)
    monkeypatch.setenv('GEMINI_API_KEYS', 'k0')
    monkeypatch.setattr(V.AIClient, '_build_client', lambda self, k, m: True)
    assert V.AIClient(Log()).models == list(V.AIClient.DEFAULT_MODELS)
    monkeypatch.setenv('GEMINI_MODELS', ' a , b ,a')
    assert V.AIClient(Log()).models == ['a', 'b']


def test_falls_back_to_the_next_model_when_every_key_is_spent_on_the_first(monkeypatch):
    spent = {'m1': E429_DAY, 'm2': 'from m2'}
    ai, chats, _ = make(monkeypatch, [spent, spent, spent], models=('m1', 'm2'))
    assert ai.ask('hi') == 'from m2'
    assert ai.active_model == 'm2'
    assert sum(chats[(k, 'm1')].calls for k in ('k0', 'k1', 'k2')) == 3          # each key tried once on m1, no more


def test_keeps_using_the_fallback_without_hammering_the_spent_model(monkeypatch):
    spent = {'m1': E429_DAY, 'm2': 'ok'}
    ai, chats, _ = make(monkeypatch, [spent, spent], models=('m1', 'm2'))
    for _ in range(5):
        assert ai.ask('hi') == 'ok'
    assert sum(chats[(k, 'm1')].calls for k in ('k0', 'k1')) == 2                # only the first question paid for the probing


def test_returns_to_the_primary_model_when_it_recovers(monkeypatch):
    class Flip(FakeChat):
        pass
    spent = {'m1': E429_DAY, 'm2': 'fallback'}
    ai, chats, _ = make(monkeypatch, [spent], models=('m1', 'm2'))
    assert ai.ask('1') == 'fallback' and ai.active_model == 'm2'
    chats[('k0', 'm1')].outcome = 'primary again'
    ai.cooldowns[('k0', 'm1')] = 0                                                  # its daily quota reset
    assert ai.ask('2') == 'primary again' and ai.active_model == 'm1'


def test_a_retired_model_is_skipped_for_every_key_at_once(monkeypatch):
    gone = {'m1': E404, 'm2': 'ok'}
    ai, chats, log = make(monkeypatch, [gone, gone, gone], models=('m1', 'm2'))
    assert ai.ask('hi') == 'ok'
    assert sum(chats[(k, 'm1')].calls for k in ('k0', 'k1', 'k2')) == 1          # one 404 is enough
    assert 'not available' in log.text()
    assert ai.ask('again') == 'ok'
    assert sum(chats[(k, 'm1')].calls for k in ('k0', 'k1', 'k2')) == 1


def test_a_rejected_key_is_dropped_on_every_model(monkeypatch):
    ai, chats, _ = make(monkeypatch, [E400, 'good'], models=('m1', 'm2'))
    assert ai.ask('hi') == 'good'
    assert ai.cooldowns[('k0', 'm2')] - time.time() > 3600


def test_everything_cooling_down_recovers_when_a_cooldown_ends(monkeypatch):
    ai, chats, _ = make(monkeypatch, [E429, E429], models=('m1',))
    assert 'busy' in ai.ask('hi').lower() and not ai.ready
    chats[('k0', 'm1')].outcome = 'back'
    ai.cooldowns[('k0', 'm1')] = 0
    assert ai.ask('hi again') == 'back' and ai.ready                                 # used to stay "unavailable" forever


def test_busy_on_one_model_does_not_burn_the_other(monkeypatch):
    ai, chats, _ = make(monkeypatch, [{'m1': E503, 'm2': 'ok'}, {'m1': E503, 'm2': 'ok'}], models=('m1', 'm2'))
    assert ai.ask('hi') == 'ok'                                                      # 2 busy tries on m1, then m2 answers
    assert sum(chats[(k, 'm1')].calls for k in ('k0', 'k1')) == 2


# ── latency: do not probe every spent key one by one (each probe is ~1 s on the real network) ─────
def test_daily_quota_probing_is_capped_so_the_answer_is_fast(monkeypatch):
    spent = {'m1': E429_DAY, 'm2': 'from m2'}
    ai, chats, _ = make(monkeypatch, [spent] * 15, models=('m1', 'm2'))
    assert ai.ask('hi') == 'from m2'
    assert sum(chats[('k%d' % i, 'm1')].calls for i in range(15)) == V.AIClient.DAILY_PROBES        # 3 probes, not 15


def test_a_paused_model_is_not_touched_until_the_recheck_then_probing_moves_on(monkeypatch):
    spent = {'m1': E429_DAY, 'm2': 'ok'}
    ai, chats, _ = make(monkeypatch, [spent] * 9, models=('m1', 'm2'))
    ai.ask('1')
    n1 = sum(chats[('k%d' % i, 'm1')].calls for i in range(9))
    ai.ask('2'); ai.ask('3')
    assert sum(chats[('k%d' % i, 'm1')].calls for i in range(9)) == n1                 # paused: no new probes
    ai.model_gone['m1'] = 0                                                              # the 5 minutes are over
    ai.ask('4')
    assert sum(chats[('k%d' % i, 'm1')].calls for i in range(9)) == n1 + V.AIClient.DAILY_PROBES
    assert chats[('k3', 'm1')].calls + chats[('k4', 'm1')].calls + chats[('k5', 'm1')].calls == 3   # the NEXT three keys


def test_the_first_model_comes_back_when_a_probed_key_has_quota_again(monkeypatch):
    spent = {'m1': E429_DAY, 'm2': 'fallback'}
    ai, chats, _ = make(monkeypatch, [spent] * 6, models=('m1', 'm2'))
    ai.ask('1')
    for i in (3, 4, 5):                                                                  # the next keys' quota has reset
        chats[('k%d' % i, 'm1')].outcome = 'primary'
    ai.model_gone['m1'] = 0
    assert ai.ask('2') == 'primary' and ai.active_model == 'm1'


def test_a_single_model_is_never_paused(monkeypatch):
    ai, chats, _ = make(monkeypatch, [E429_DAY] * 5, models=('m1',))
    ai.ask('hi')
    assert sum(chats[('k%d' % i, 'm1')].calls for i in range(5)) == 5                    # nowhere else to go: try them all


# ── answers with an optional picture ───────────────────────────────────────────────────────────────
import json  # noqa: E402

PIC = {'title': 'Circle', 'seconds': 30, 'shapes': [{'kind': 'circle', 'x': 50, 'y': 50, 'r': 30}], 'formula': 'C = 2πr'}


def test_voice_only_answer_has_no_picture(monkeypatch):
    ai, _, _ = make(monkeypatch, [json.dumps({'say': 'We open at nine.', 'canvas': None})])
    assert ai.ask_rich('when do you open') == ('We open at nine.', None)


def test_an_answer_with_a_picture(monkeypatch):
    ai, _, _ = make(monkeypatch, [json.dumps({'say': 'Two pi r.', 'canvas': PIC})])
    say, canvas = ai.ask_rich('circumference?')
    assert say == 'Two pi r.' and canvas['formula'] == 'C = 2πr' and canvas['seconds'] == 30
    assert canvas['shapes'][0]['kind'] == 'circle'


def test_ask_still_returns_just_the_spoken_text(monkeypatch):
    ai, _, _ = make(monkeypatch, [json.dumps({'say': 'Hello.', 'canvas': PIC})])
    assert ai.ask('hi') == 'Hello.'


def test_a_model_that_ignores_the_format_still_speaks(monkeypatch):
    ai, _, _ = make(monkeypatch, ['Just plain words, no JSON.'])
    assert ai.ask_rich('hi') == ('Just plain words, no JSON.', None)


def test_markdown_in_the_spoken_part_is_removed(monkeypatch):
    ai, _, _ = make(monkeypatch, [json.dumps({'say': 'It is **four** `4` #1', 'canvas': None})])
    assert ai.ask_rich('2+2')[0] == 'It is four 4 1'


def test_failures_never_carry_a_picture(monkeypatch):
    ai, _, _ = make(monkeypatch, [E429, E429])
    say, canvas = ai.ask_rich('hi')
    assert 'busy' in say.lower() and canvas is None


def test_the_picture_is_sanitised_before_it_leaves_the_client(monkeypatch):
    evil = {'shapes': [{'kind': 'circle', 'x': 5000, 'y': 5, 'r': 5}, {'kind': 'script'}], 'seconds': 99999, 'formula': '<b>x</b>'}
    ai, _, _ = make(monkeypatch, [json.dumps({'say': 'ok', 'canvas': evil})])
    canvas = ai.ask_rich('x')[1]
    assert canvas['shapes'] == [{'kind': 'circle', 'x': 100.0, 'y': 5.0, 'r': 5.0}] and canvas['seconds'] == 90


def test_a_failed_attempt_is_rotated_past_and_the_picture_comes_from_the_good_key(monkeypatch):
    ai, _, _ = make(monkeypatch, [E429, json.dumps({'say': 'Pi.', 'canvas': PIC})])
    say, canvas = ai.ask_rich('circle')
    assert say == 'Pi.' and canvas is not None
