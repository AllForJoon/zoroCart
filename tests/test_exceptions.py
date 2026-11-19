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
from zorocart.model import ZoroClient
from zorocart.runner import Run, SCENARIOS

BASELINE = {
    ("wipes-daily", "single-unscented"): 1,
    ("pantry-coffee", "mild"): 1,
}
EXPECTED = {
    **BASELINE,
    ("wipes-daily", "single-unscented"): 2,
    ("bags-easy", "30l-handle"): 1,
}
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

    def __init__(self, fault: str):
        self.fault = fault
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
            elif user_prompt == SCENARIOS["household"]:
                text = json.dumps(PLAN, ensure_ascii=False)
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


async def exercise(fault: str, *, confirmation=None):
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
            transport = httpx.MockTransport(OfflineZoro(fault).respond)
            async with ZoroClient("http://model.invalid", transport=transport, event=run.event) as model:
                engine = ShoppingEngine(
                    model, adapter, run.event,
                    confirm=confirm if confirmation is not None else None,
                )
                result = await engine.run(run.request)
            cart = None
            if fault != "blocked":
                await adapter.open_cart()
                cart = {
                    (line.product_id, line.option_id): line.quantity
                    for line in await adapter.read_cart()
                }
            await context.close()
    return result, run.events, cart, writes


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
    assert not writes
    if fault != "blocked":
        assert cart == BASELINE


@pytest.mark.parametrize("reply", ["yes", "no", None])
def test_partial_cart_requires_explicit_confirmation(reply):
    confirmations = []

    def confirm(kind, message, writes_at_prompt, events_at_prompt):
        confirmations.append(kind)
        assert "물티슈만 담을까요?" in message
        assert not writes_at_prompt
        assert any(event["kind"] == "shopping_plan" for event in events_at_prompt)
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
        assert cart == {**BASELINE, ("wipes-daily", "single-unscented"): 2}
        assert writes == ["/store/cart/add"]
    else:
        assert result["status"] == "held"
        assert not result["added"]
        assert not writes
        assert cart == BASELINE
        assert not any(event["kind"] in {"cart_baseline", "cart_verification"} for event in events)


@pytest.mark.parametrize(("fault", "requires_retry"), [
    ("", False),
    ("stale-target", False),
    ("add-timeout-applied", False),
    ("add-timeout-unapplied", True),
])
def test_cart_recovery_never_duplicates_or_changes_existing_rows(fault, requires_retry, monkeypatch):
    monkeypatch.setattr(store, "ADD_RESPONSE_DELAY_SECONDS", 2.5)
    result, events, cart, writes = asyncio.run(exercise(fault))
    assert result["status"] == "complete", result
    assert result["merchandise_total"] == 18_100
    assert {(row["product_id"], row["option_id"], row["quantity"]) for row in result["added"]} == {
        ("wipes-daily", "single-unscented", 1), ("bags-easy", "30l-handle", 1),
    }
    assert cart == EXPECTED
    assert writes == ["/store/cart/add"] * (2 + int(requires_retry))
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
    assert cart == {**BASELINE, ("wipes-daily", "single-unscented"): 2}
    assert writes == ["/store/cart/add"]
