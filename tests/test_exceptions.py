"""Browser regressions against the synthetic store with an offline Zoro HTTP double."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from playwright.async_api import async_playwright

from zorocart import store
from zorocart.browser import BrowserAdapter
from zorocart.engine import ShoppingEngine
from zorocart.model import ModelResponseError, ZoroClient
from zorocart.runner import Run, SCENARIOS

BASELINE = {
    ("wipes-daily", "single-unscented"): 1,
    ("pantry-coffee", "mild"): 1,
}
EXPECTED = {
    ("wipes-daily", "single-unscented"): 1,
    ("bags-easy", "30l-handle"): 1,
}
CLEAR = "/store/cart/clear"
ADD = "/store/cart/add"
PLAN = {
    "kind": "direct",
    "total_budget": 30_000,
    "shipping_source": "explicit",
    "candidates": [
        {"name": "물티슈", "source": "direct", "quantity_source": "explicit", "purchase_unit": "팩",
         "required": ["무향"], "preferred": ["로켓배송"]},
        {"name": "쓰레기봉투", "source": "direct", "quantity_source": "explicit", "purchase_unit": "팩",
         "required": ["30L", "손잡이형"], "preferred": ["로켓배송"]},
    ],
}
QUERIES = {
    "물티슈": ("무향 물티슈", "물티슈 무향"),
    "쓰레기봉투": ("30L 손잡이형 쓰레기봉투", "쓰레기봉투 30L 손잡이형"),
}


class OfflineZoro:
    """Only the model transport is substituted. Every cart action uses Chromium."""

    def __init__(self, fault: str, draft: dict | str):
        self.fault = fault
        self.draft = draft
        self.delayed = False

    async def respond(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path == "/v1/chat/completions":
            messages = payload["messages"]
            system_prompt = messages[0]["content"]
            user_prompt = messages[-1]["content"]
            if system_prompt.startswith("Classify the Korean shopper's request."):
                assert user_prompt == SCENARIOS["household"]
                text = "DIRECT"
            elif user_prompt == SCENARIOS["household"] or user_prompt.startswith("쇼핑 요청: "):
                text = self.draft if isinstance(self.draft, str) else json.dumps(self.draft, ensure_ascii=False)
            else:
                prompt = json.loads(user_prompt.removeprefix("품목 정보: ").splitlines()[0])
                text = QUERIES[prompt["품목"]][int(prompt["이전 검색어"] is not None)]
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

        assert request.url.path == "/v1/systemone"
        state = payload["state"]
        questions = payload["questions"]
        if (
            self.fault == "stale-target" and not self.delayed
            and state.get("stage") == "선택된 판매 옵션 지정"
        ):
            self.delayed = True
            await asyncio.sleep(store.STALE_OPTION_DELAY_MS / 1000 + 0.2)
        answers = {name: self.answer(name, question, state) for name, question in questions.items()}
        return httpx.Response(200, json={"answers": answers})

    @staticmethod
    def answer(name: str, question: dict, state: dict) -> dict:
        criteria = question["criteria"]
        if question["type"] == "score":
            score = 4 if state["option"]["delivery"] == "로켓배송" else 3
            return {"type": "score", "probabilities": {
                str(index): float(index == score) for index in range(len(criteria))
            }}
        if name == "requirements":
            evidence = " ".join(state["option"]["attributes"]).casefold()
            single_pack = state["option"]["pack_count"] == 1
            matches = single_pack and all(
                required.casefold() in evidence for required in state["item"]["required"]
            )
            choice = "match" if matches else "mismatch"
        else:
            choice = next(iter(criteria))
        return {"type": "choice", "choice": choice, "probabilities": {
            key: float(key == choice) for key in criteria
        }}


@asynccontextmanager
async def running_store():
    app = FastAPI()
    writes: list[str] = []

    @app.middleware("http")
    async def record_writes(request: Request, call_next):
        if request.method not in {"GET", "HEAD"}:
            writes.append(request.url.path)
        return await call_next(request)

    app.include_router(store.router)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        config = uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=5)
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}", writes
        finally:
            server.should_exit = True
            await task


async def exercise(fault: str, *, confirmation=None, draft=PLAN, search_only: bool = False):
    run = Run(SCENARIOS["household"], environment="offline-test")
    async with running_store() as (origin, writes), async_playwright() as pw:
        async def confirm(kind: str, message: str):
            return confirmation(kind, message, tuple(writes), tuple(run.events))

        options = {"headless": True}
        if channel := os.getenv("ZOROCART_CHROMIUM_CHANNEL"):
            options["channel"] = channel
        async with await pw.chromium.launch(**options) as browser:
            context = await browser.new_context(service_workers="block")
            page = await context.new_page()
            store_url = f"{origin}/store" + (f"?fault={fault}" if fault else "")
            adapter = BrowserAdapter(page, store_url, action_timeout_ms=1500)
            transport = httpx.MockTransport(OfflineZoro(fault, draft).respond)
            async with ZoroClient("http://model.invalid", transport=transport, event=run.event) as model:
                engine = ShoppingEngine(
                    model, adapter, run.event,
                    confirm=confirm if confirmation is not None else None,
                    search_only=search_only,
                )
                try:
                    result = await engine.run(run.request)
                except ModelResponseError as error:
                    if not isinstance(draft, str):
                        raise
                    result = error
            cart = None
            if fault != "blocked":
                await adapter.open_cart()
                cart = {
                    (line.product_id, line.option_id): line.quantity
                    for line in await adapter.read_cart()
                }
            await context.close()
    return result, run.events, cart, writes


def test_search_only_verifies_both_result_views_without_changing_cart():
    result, events, cart, writes = asyncio.run(exercise("", search_only=True))

    assert result["status"] == "search_verified"
    assert result["cart_reset"] is None
    assert not result["added"]
    assert not writes
    assert cart == BASELINE

    searched = [event for event in events if event["kind"] == "search_results_visible"]
    assert [(event["item"], event["query"]) for event in searched] == [
        ("물티슈", "무향 물티슈"),
        ("쓰레기봉투", "30L 손잡이형 쓰레기봉투"),
    ]
    assert all(event["inspected_cards"] > 0 for event in searched)


def test_search_only_holds_when_both_queries_lack_readable_cards(monkeypatch):
    monkeypatch.setitem(QUERIES, "물티슈", ("없는 상품", "없는 물건"))
    result, events, cart, writes = asyncio.run(exercise("", search_only=True))

    assert result["status"] == "held"
    assert "상품 카드" in result["reason"]
    assert [event["retry"] for event in events if event["kind"] == "search_query"] == [False, True]
    assert not any(event["kind"] == "search_results_visible" for event in events)
    assert not writes
    assert cart == BASELINE


@pytest.mark.parametrize(("fault", "reason"), [
    ("budget", "예산"),
    ("sold-out", "품절"),
    ("spec-mismatch", "불일치"),
    ("missing-info", "상품가 확인 불가"),
    ("blocked", "접근 제한"),
])
def test_unsafe_or_unverifiable_cart_additions_are_held(fault, reason):
    result, events, cart, writes = asyncio.run(exercise(fault))
    logged_reasons = [event.get("reason") or "" for event in events]
    assert result["status"] == "held", result
    assert not result["added"]
    assert any(reason in message for message in logged_reasons), logged_reasons
    if fault == "blocked":
        assert not writes
    else:
        assert writes == [CLEAR]
        assert cart == {}


@pytest.mark.parametrize("draft", [
    {"kind": "direct", "clarification": "무향 물티슈와 봉투 중 어떤 품목인가요?"},
    "이 응답은 구매 계획 JSON이 아닙니다.",
])
def test_unvalidated_plan_keeps_the_existing_cart(draft):
    result, events, cart, writes = asyncio.run(exercise("", draft=draft))
    if isinstance(draft, str):
        assert isinstance(result, ModelResponseError) or result["status"] == "held"
        assert sum(event["kind"] == "plan_format_invalid" for event in events) == 2
    else:
        assert result["status"] == "held"
        assert result["reason"] == draft["clarification"]
    assert cart == BASELINE
    assert not writes
    assert not any(event["kind"] in {"cart_reset_started", "search_query"} for event in events)


def test_unverified_cart_reset_stops_before_search_or_add():
    result, events, cart, writes = asyncio.run(exercise("clear-mismatch"))
    assert result["status"] == "held"
    assert cart == BASELINE
    assert writes == [CLEAR]
    assert any(event["kind"] == "cart_reset_started" for event in events)
    assert not any(event["kind"] in {"cart_reset_verified", "search_query"} for event in events)


def test_store_rejects_a_clear_snapshot_after_the_cart_changes():
    async def exercise_snapshot():
        async with running_store() as (origin, _), httpx.AsyncClient(base_url=origin) as client:
            assert (await client.get("/store/cart")).status_code == 200
            snapshot = json.dumps(
                [(*key, quantity) for key, quantity in sorted(BASELINE.items())], separators=(",", ":"),
            )
            added = await client.post(ADD, data={
                "product": "pantry-coffee", "option": "mild", "quantity": 1,
            })
            assert added.status_code == 303
            response = await client.post(CLEAR, data={"cart": snapshot})
            assert response.status_code == 409
            current = await client.get("/store/cart")
            assert 'data-zc-field="quantity" data-zc-value="2"' in current.text
            assert 'data-zc-id="wipes-daily" data-zc-option="single-unscented"' in current.text

    asyncio.run(exercise_snapshot())


@pytest.mark.parametrize("reply", ["yes", "no", None])
def test_partial_cart_requires_explicit_confirmation(reply):
    confirmations = []

    def confirm(kind, message, writes_at_prompt, events_at_prompt):
        confirmations.append(kind)
        assert "물티슈만 담을까요?" in message
        assert writes_at_prompt == (CLEAR,)
        assert any(event["kind"] == "shopping_plan" for event in events_at_prompt)
        assert any(event["kind"] == "cart_reset_verified" for event in events_at_prompt)
        scored_items = {
            event["item"] for event in events_at_prompt
            if event["kind"] == "candidate_score" and event["requirements"] == "match"
        }
        assert len(scored_items) == 2
        assert not any(event["kind"] == "cart_baseline" for event in events_at_prompt)
        return reply

    result, events, cart, writes = asyncio.run(exercise("budget", confirmation=confirm))
    assert confirmations == ["partial"]
    if reply == "yes":
        assert result["status"] == "partial"
        assert result["merchandise_total"] == 16_200
        assert [(row["product_id"], row["option_id"], row["quantity"]) for row in result["added"]] == [
            ("wipes-daily", "single-unscented", 1),
        ]
        assert [row["item"] for row in result["remaining"]] == ["쓰레기봉투"]
        assert cart == {("wipes-daily", "single-unscented"): 1}
        assert writes == [CLEAR, ADD]
    else:
        assert result["status"] == "held"
        assert not result["added"]
        assert writes == [CLEAR]
        assert cart == {}
        assert not any(event["kind"] in {"cart_baseline", "cart_verification"} for event in events)


@pytest.mark.parametrize(("fault", "requires_retry"), [
    ("", False),
    ("stale-target", False),
    ("add-timeout-applied", False),
    ("add-timeout-unapplied", True),
])
def test_cart_recovery_never_duplicates_items_after_reset(fault, requires_retry, monkeypatch):
    monkeypatch.setattr(store, "ADD_RESPONSE_DELAY_SECONDS", 2.5)
    result, events, cart, writes = asyncio.run(exercise(fault))
    assert result["status"] == "complete", result
    assert result["merchandise_total"] == 18_100
    assert {(row["product_id"], row["option_id"], row["quantity"]) for row in result["added"]} == {
        ("wipes-daily", "single-unscented", 1), ("bags-easy", "30l-handle", 1),
    }
    assert cart == EXPECTED
    assert writes == [CLEAR, *([ADD] * (2 + int(requires_retry)))]
    reset = next(event for event in events if event["kind"] == "cart_reset_verified")
    assert reset["removed_lines"] == reset["removed_units"] == 2
    assert events.index(reset) < next(index for index, event in enumerate(events) if event["kind"] == "search_query")
    assert sum(event["kind"] == "add_uncertain" for event in events) == int(fault.startswith("add-timeout-"))
    assert any(event["kind"] == "add_not_reflected" for event in events) == requires_retry
    if fault == "stale-target":
        assert any(event["kind"] == "stale_observation" for event in events)


def test_price_change_recalculates_remaining_budget_and_preserves_verified_addition():
    result, events, cart, writes = asyncio.run(exercise("price-change"))
    assert result["status"] == "partial", result
    assert result["merchandise_total"] == 7_200
    assert [row["item"] for row in result["remaining"]] == ["쓰레기봉투"]
    assert "남은 예산" in result["reason"]
    assert any(event["kind"] == "offer_changed" for event in events)
    assert cart == {("wipes-daily", "single-unscented"): 1}
    assert writes == [CLEAR, ADD]


def test_dashboard_reuses_the_same_demo_cart_between_requests(tmp_path, monkeypatch):
    import re

    from fastapi.testclient import TestClient

    from zorocart import app as dashboard_app, store

    attached_sessions = []

    async def capture_session(run, store_url, *, headless, interactive, demo_cart_session):
        attached_sessions.append(demo_cart_session)
        run.result = {'status': 'complete', 'added': [], 'remaining': []}

    monkeypatch.setattr(dashboard_app, 'execute_run', capture_session)
    app = dashboard_app.create_app(store_url='http://127.0.0.1:8780/store', log_dir=tmp_path)
    with TestClient(app) as client:
        html = client.get('/').text
        token = re.search(r'name="zc-token" content="([^"]+)"', html).group(1)
        cookie = client.cookies.get(store.COOKIE)
        headers = {'X-ZoroCart-Token': token}
        for request in ('무향 물티슈 한 팩 담아줘.', '파스타면 한 봉 담아줘.'):
            response = client.post('/api/runs', headers=headers, json={'request': request})
            assert response.status_code == 202
            assert client.get('/api/runs/' + response.json()['id'], headers=headers).json()['result']
        assert cookie and attached_sessions == [cookie, cookie]
