import re

from fastapi.testclient import TestClient

from zorocart import app as dashboard_app
from zorocart.browser import NAVER_STORE_URL, CoupangApproval
from zorocart.runner import MCGROCER_URL
from zorocart.store import COOKIE

STORE_URL = 'http://127.0.0.1:8780/store'


def dashboard_headers(client):
    html = client.get('/').text
    token = re.search(r'name="zc-token" content="([^"]+)"', html).group(1)
    return {'X-ZoroCart-Token': token}


def test_dashboard_denies_unconfigured_coupang_before_scheduling(tmp_path, monkeypatch):
    async def unexpected_execution(*args, **kwargs):
        raise AssertionError('An unconfigured live run must never be scheduled.')

    monkeypatch.setattr(dashboard_app, 'execute_run', unexpected_execution)
    app = dashboard_app.create_app(store_url=STORE_URL, log_dir=tmp_path)
    with TestClient(app) as client:
        headers = dashboard_headers(client)
        assert client.get('/api/environments', headers=headers).json() == {
            'coupang_available': False, 'coupang_writes_available': False, 'mcgrocer_available': False,
        }
        request = {'request': '무향 물티슈 한 팩 담아줘.', 'environment': 'coupang'}
        response = client.post('/api/runs', headers=headers, json=request)
        assert response.status_code == 403


def test_dashboard_starts_guest_runs_with_dedicated_profile_and_separate_demo_session(tmp_path, monkeypatch):
    calls = []

    async def capture_run(run, store_url, **settings):
        calls.append((run.environment, store_url, settings))
        run.result = {'status': 'held' if run.environment == 'naver' else 'complete',
                      'cart_reset': None, 'added': [], 'remaining': []}

    approval = CoupangApproval(
        reference='offline-dashboard-routing',
        home_url='https://www.coupang.com',
        cart_url='https://cart.coupang.com/cartView.pang',
        read_paths=('/',),
        desktop_ui=True,
    )
    profile = tmp_path / 'coupang-guest'
    mcgrocer_state = tmp_path / 'mcgrocer-guest'
    monkeypatch.setattr(dashboard_app, 'execute_run', capture_run)
    app = dashboard_app.create_app(
        store_url=STORE_URL, log_dir=tmp_path, headless=True,
        coupang_approval=approval, coupang_profile_dir=profile, mcgrocer_state_dir=mcgrocer_state,
    )
    with TestClient(app) as client:
        headers = dashboard_headers(client)
        assert client.get('/api/environments', headers=headers).json() == {
            'coupang_available': True, 'coupang_writes_available': False, 'mcgrocer_available': True,
        }
        demo_session = client.cookies.get(COOKIE)
        for mode in ('coupang', 'naver', 'mcgrocer', 'local', 'coupang'):
            response = client.post('/api/runs', headers=headers, json={
                'request': '무향 물티슈 한 팩 담아줘.', 'environment': mode,
            })
            assert response.status_code == 202
            artifact = client.get('/api/runs/' + response.json()['id'], headers=headers).json()
            assert artifact['environment'] == mode
            if mode == 'naver':
                assert artifact['result']['status'] == 'held'
                assert artifact['result']['cart_reset'] is None
                assert not artifact['result']['added']
            else:
                assert artifact['result']['status'] == 'complete'
        assert demo_session and client.cookies.get(COOKIE) == demo_session
        live = ('coupang', approval.home_url, {
            'headless': False, 'interactive': True, 'approval': approval, 'profile_dir': profile,
        })
        local = ('local', STORE_URL, {
            'headless': True, 'interactive': True, 'demo_cart_session': demo_session,
        })
        naver = ('naver', NAVER_STORE_URL, {'headless': False, 'interactive': True})
        mcgrocer = ('mcgrocer', MCGROCER_URL, {
            'headless': False, 'interactive': True, 'mcgrocer_state_dir': mcgrocer_state,
        })
        assert calls == [live, naver, mcgrocer, local, live]
