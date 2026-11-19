from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import async_playwright

from .browser import BrowserAdapter, CoupangApproval
from .engine import ShoppingEngine
from .model import ZoroClient
from .store import COOKIE as DEMO_CART_COOKIE

CHECKPOINT = 'bf1b76b9b4df03b05fcbb73444e1cd4db0f566da'
SERVING_COMMIT = '0ddcf516e458a82892d7fe0d0dabeb113d3cf424'

SCENARIOS = {
    'household': '상품가 합계 3만 원 안에서 무향 물티슈 1팩과 30L 손잡이형 쓰레기봉투 1팩 담아줘. 가능하면 로켓배송으로.',
    'pasta': '2인분 토마토 파스타를 만들 거야. 올리브유, 소금, 마늘은 집에 있어. 상품가 합계 2만 원 안에서 필요한 재료를 담아줘.',
    'camping': '주말에 두 명이 캠핑장에서 1박 할 거야. 텐트와 침낭은 챙겼고 캠핑장에 테이블은 있어. 해가 진 뒤 텐트 앞에서 둘이 쉬는 데 필요한 준비물을 상품가 합계 6만 원 안에서 담아줘.',
}

# Browser evidence emitted by the adapters is limited to public offer and cart fields.
# Strip common secrets from free-form inputs and any string passed to the log.
_PRIVATE = re.compile(
    r'(?i)(?:bearer\s+[\w.\-]+|(?:gh[pousr]_|hf_|sk-)[\w\-]{8,}|'
    r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|\b01[016789][ -]?\d{3,4}[ -]?\d{4}\b|'
    r'(?:비밀번호|password|배송\s*주소|shipping address)\s*[:=]?\s*[^\n;,]+|'
    r'(?:[가-힣]+(?:특별시|광역시|특별자치도|시|군|구)\s+){1,3}[가-힣0-9·]+(?:로|길)\s*\d[\d-]*(?:\s+\d+동)?(?:\s+\d+호)?)'
)


def sanitized(value):
    if isinstance(value, str):
        return _PRIVATE.sub('[가림]', value)
    if isinstance(value, dict):
        return {key: sanitized(entry) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitized(entry) for entry in value]
    return value


@dataclass
class Run:
    request: str
    environment: str = 'local'
    id: str = field(default_factory=lambda: uuid4().hex)
    events: list[dict] = field(default_factory=list)
    result: dict | None = None
    pending: dict | None = None
    reply: asyncio.Future | None = None
    started: float = field(default_factory=time.monotonic)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def event(self, kind: str, **fields):
        if 'elapsed_ms' in fields:
            fields['duration_ms'] = fields.pop('elapsed_ms')
        self.events.append(sanitized({'kind': kind, 'elapsed_ms': round((time.monotonic() - self.started) * 1000), **fields}))

    async def ask(self, kind: str, message: str) -> str | None:
        self.pending = {'type': kind, 'message': sanitized(message)}
        self.reply = asyncio.get_running_loop().create_future()
        self.event('confirmation_requested', confirmation=kind, message=message)
        try:
            answer = await self.reply
            self.event('confirmation_resolved', confirmation=kind, accepted=(answer == 'yes' if kind == 'partial' else bool(answer)))
            return answer
        finally:
            self.pending = None
            self.reply = None

    def artifact(self) -> dict:
        calls = [e for e in self.events if e['kind'] == 'model_call']
        return {'run_id': self.id, 'environment': self.environment, 'timestamp': self.timestamp,
                'request': sanitized(self.request), 'pending': self.pending, 'result': self.result,
                'metrics': {'elapsed_ms': (self.events[-1]['elapsed_ms'] if self.result and self.events else
                                            round((time.monotonic() - self.started) * 1000)),
                            'generation_calls': sum('/chat/completions' in str(e) for e in calls),
                            'decision_calls': sum('/systemone' in str(e) for e in calls),
                            'search_retries': sum(e.get('retry') is True for e in self.events if e['kind'] == 'search_query'),
                            'held_candidates': sum(e['kind'] == 'candidate_hold' or
                                                   (e['kind'] == 'candidate_score' and bool(e.get('reason')))
                                                   for e in self.events),
                            'held_items': len(self.result['remaining']) if self.result else 0}, 'events': self.events}

    def save(self, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.artifact(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(destination)


async def execute_run(run: Run, store_url: str, *, headless=False, interactive=False,
                      timeout=420, max_steps=140, approval=None, profile_dir: Path | None = None,
                      demo_cart_session: str | None = None):
    engine = None
    browser_context = None
    if run.environment == 'coupang':
        if approval is None or profile_dir is None or headless:
            raise ValueError('쿠팡 모드에는 쿠팡 승인 범위, 전용 프로필, 보이는 브라우저가 필요합니다.')
        if not isinstance(approval, CoupangApproval):
            approval = CoupangApproval.from_file(approval)
        approval.validate()
    if demo_cart_session is not None and run.environment != 'local':
        raise ValueError('시연 장바구니 세션은 로컬 상점에서만 사용할 수 있습니다.')
    deadline = time.monotonic() + timeout
    try:
        async with asyncio.timeout(timeout), async_playwright() as pw:
            options = {'headless': headless}
            if channel := os.environ.get('ZOROCART_CHROMIUM_CHANNEL'):
                options['channel'] = channel
            if executable := os.environ.get('ZOROCART_CHROMIUM_EXECUTABLE'):
                options['executable_path'] = executable
            if profile_dir:
                browser_context = await pw.chromium.launch_persistent_context(str(profile_dir), service_workers='block', **options)
                browser_instance = None
            else:
                browser_instance = await pw.chromium.launch(**options)
                browser_context = await browser_instance.new_context(service_workers='block')
            page = await browser_context.new_page()
            for existing in browser_context.pages:
                if existing != page:
                    await existing.close()
            adapter_class = BrowserAdapter
            if run.environment == 'coupang' and approval.desktop_ui:
                from .coupang_live import CoupangDesktopAdapter

                adapter_class = CoupangDesktopAdapter
            adapter = adapter_class(page, store_url, mode=run.environment, approval=approval,
                                    max_steps=max_steps, deadline=deadline)
            if demo_cart_session is not None:
                origin = urlsplit(adapter.home_url)
                await browser_context.add_cookies([{'name': DEMO_CART_COOKIE, 'value': demo_cart_session,
                                                   'url': f'{origin.scheme}://{origin.netloc}/',
                                                   'httpOnly': True, 'sameSite': 'Strict'}])
            async with ZoroClient(os.environ.get('ZORO_URL', 'http://127.0.0.1:30001'),
                                  model=os.environ.get('ZORO_MODEL', CHECKPOINT),
                                  api_key=os.environ.get('ZORO_API_KEY'), event=run.event,
                                  timeout=min(150, timeout)) as client:
                run.event('run_started', checkpoint=os.environ.get('ZORO_MODEL', CHECKPOINT),
                          reference_serving_commit=SERVING_COMMIT, visible_browser=not headless)
                engine = ShoppingEngine(client, adapter, run.event, confirm=run.ask if interactive else None,
                                        score_threshold=float(os.environ.get('ZOROCART_MIN_SCORE', '2.5')),
                                        choice_threshold=float(os.environ.get('ZOROCART_MIN_PROBABILITY', '0.70')))
                run.result = await engine.run(run.request)
            await browser_context.close()
            if browser_instance:
                await browser_instance.close()
    except TimeoutError:
        reason = '실행 제한 시간에 도달했습니다. 마지막으로 확인한 장바구니 결과를 표시합니다.'
        run.event('run_timeout', reason=reason)
        if engine:
            run.result = engine.result('partial' if engine.verified else 'held', reason)
        else:
            run.result = {'status': 'error', 'reason': reason, 'cart_reset': None,
                          'added': [], 'merchandise_total': 0, 'remaining': []}
    except Exception as exc:
        # Raw transport/Playwright errors can contain request bodies, URLs, or session state.
        run.event('run_error', error_type=type(exc).__name__)
        reason = f'실행 중 오류가 발생했습니다 ({type(exc).__name__}).'
        if exc.__class__.__module__.startswith('zorocart.'):
            reason = sanitized(str(exc))[:250]
        run.result = (engine.result('partial' if engine.verified else 'held', reason) if engine else
                      {'status': 'error', 'reason': reason, 'cart_reset': None,
                       'added': [], 'merchandise_total': 0, 'remaining': []})
    run.result = sanitized(run.result)
    run.event('run_finished', status=run.result['status'])
    return run.result
