"""gemini_check / gemini_check_key: how the panel reads Google's answer for a key (no network: urlopen is faked)."""
import io
import json
import sys
import urllib.error

sys.path.insert(0, '/home/user/ros2_ws/panel')
from lumi_panel import config, server  # noqa: E402

MODELS = ['m1', 'm2']


def fake(monkeypatch, plan):
    """plan: {model: None (works) | (http_code, body)}. Records which models were asked."""
    asked = []

    def urlopen(req, timeout=0):
        model = req.full_url.split('/models/')[1].split(':')[0]
        asked.append(model)
        outcome = plan[model]
        if outcome is None:
            class R:
                status = 200

                def __enter__(s):
                    return s

                def __exit__(s, *a):
                    return False
            return R()
        code, body = outcome
        raise urllib.error.HTTPError(req.full_url, code, 'x', {}, io.BytesIO(json.dumps(body or {}).encode()))
    monkeypatch.setattr(server.urllib.request, 'urlopen', urlopen)
    return asked


def minute_429(delay='29s'):
    return {'error': {'status': 'RESOURCE_EXHAUSTED', 'details': [
        {'violations': [{'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}, {'retryDelay': delay}]}}


DAY_429 = {'error': {'details': [{'violations': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}]}}


# ── one model ──────────────────────────────────────────────────────────────────────────────────────
def test_working(monkeypatch):
    fake(monkeypatch, {'m1': None})
    r = server.gemini_check('k', 'm1')
    assert r['state'] == 'ok' and r['ok'] and r['model'] == 'm1'


def test_per_minute_limit_is_valid_but_limited_with_the_wait(monkeypatch):
    fake(monkeypatch, {'m1': (429, minute_429('29s'))})
    r = server.gemini_check('k', 'm1')
    assert r['state'] == 'limited' and not r['ok'] and '30 s' in r['detail']


def test_daily_quota(monkeypatch):
    fake(monkeypatch, {'m1': (429, DAY_429)})
    assert server.gemini_check('k', 'm1')['state'] == 'quota'


def test_unspecified_429_is_a_short_limit(monkeypatch):
    fake(monkeypatch, {'m1': (429, {})})
    assert server.gemini_check('k', 'm1')['state'] == 'limited'


def test_busy_gone_and_rejected(monkeypatch):
    for code, state in ((503, 'busy'), (404, 'gone'), (400, 'invalid'), (401, 'invalid'), (403, 'invalid')):
        fake(monkeypatch, {'m1': (code, {})})
        assert server.gemini_check('k', 'm1')['state'] == state


def test_network_failure(monkeypatch):
    def boom(req, timeout=0):
        raise OSError('no route')
    monkeypatch.setattr(server.urllib.request, 'urlopen', boom)
    assert server.gemini_check('k', 'm1')['state'] == 'error'


def test_result_never_contains_the_key(monkeypatch):
    fake(monkeypatch, {'m1': (429, minute_429())})
    assert 'SECRETKEY123' not in json.dumps(server.gemini_check('SECRETKEY123', 'm1'))


# ── several models: what "Check all" must NOT spend ────────────────────────────────────────────────
def test_primary_working_asks_only_the_primary(monkeypatch):
    asked = fake(monkeypatch, {'m1': None, 'm2': None})
    r = server.gemini_check_key('k', MODELS, with_fallback=True)
    assert r['state'] == 'ok' and asked == ['m1']                  # a working key never spends the fallback's quota


def test_default_check_does_not_touch_the_fallback(monkeypatch):
    asked = fake(monkeypatch, {'m1': (429, DAY_429), 'm2': None})
    r = server.gemini_check_key('k', MODELS, with_fallback=False)
    assert r['state'] == 'quota' and asked == ['m1']
    assert [m['model'] for m in r['models']] == ['m1']


def test_with_fallback_reports_fallback_only(monkeypatch):
    asked = fake(monkeypatch, {'m1': (429, DAY_429), 'm2': None})
    r = server.gemini_check_key('k', MODELS, with_fallback=True)
    assert r['state'] == 'fallback' and r['ok'] and asked == ['m1', 'm2']
    assert 'Works on m2' in r['detail']
    assert [(m['model'], m['state']) for m in r['models']] == [('m1', 'quota'), ('m2', 'ok')]


def test_everything_spent(monkeypatch):
    fake(monkeypatch, {'m1': (429, DAY_429), 'm2': (429, DAY_429)})
    r = server.gemini_check_key('k', MODELS, with_fallback=True)
    assert r['state'] == 'quota' and not r['ok'] and 'm2' in r['detail']


def test_invalid_key_stops_at_once(monkeypatch):
    asked = fake(monkeypatch, {'m1': (400, {}), 'm2': None})
    r = server.gemini_check_key('k', MODELS, with_fallback=True)
    assert r['state'] == 'invalid' and asked == ['m1']             # invalid on one model means invalid everywhere


def test_retired_primary_with_working_fallback(monkeypatch):
    fake(monkeypatch, {'m1': (404, {}), 'm2': None})
    assert server.gemini_check_key('k', MODELS, with_fallback=True)['state'] == 'fallback'


# ── the model list ─────────────────────────────────────────────────────────────────────────────────
def test_model_list_default_and_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENV_FILE', str(tmp_path / 'env'))
    store = config.EnvStore()
    assert store.gemini_models() == list(config.GEMINI_DEFAULT_MODELS)
    store._vars['GEMINI_MODELS'] = ' a-1 , b.2,a-1, bad name!, '
    assert store.gemini_models() == ['a-1', 'b.2']
