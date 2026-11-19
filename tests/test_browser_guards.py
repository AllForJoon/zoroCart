import asyncio
import os
import socket
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.async_api import Error as PlaywrightError, async_playwright

from zorocart.browser import BrowserAdapter, BrowserBlocked, StaleObservation
from zorocart.store import router


def test_live_approval_is_required_before_navigation():
    with pytest.raises(BrowserBlocked):
        BrowserAdapter(None, "https://www.coupang.com", mode="coupang")


def test_transient_read_navigation_retries_once():
    class Page:
        url = "about:blank"
        attempts = 0

        async def route(self, *_):
            pass

        def on(self, *_):
            pass

        async def goto(self, url, **_):
            self.attempts += 1
            if self.attempts == 1:
                raise PlaywrightError("transient connection failure")
            self.url = url
            return SimpleNamespace(status=200)

        def is_closed(self):
            return False

    page = Page()
    adapter = BrowserAdapter(page, "http://127.0.0.1:8873")
    asyncio.run(adapter.open_home())
    assert page.attempts == adapter.steps == 2
    assert page.url == adapter.home_url


def test_browser_revalidates_nodes_and_cart_write_payloads():
    asyncio.run(exercise_browser_guards())


async def exercise_browser_guards():
    app = FastAPI()
    app.include_router(router)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started and not server_task.done():
                await asyncio.sleep(0.02)
            if server_task.done():
                await server_task
                raise RuntimeError("The synthetic store did not start")

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    channel=os.environ.get("ZOROCART_CHROMIUM_CHANNEL"),
                    executable_path=os.environ.get("ZOROCART_CHROMIUM_EXECUTABLE"),
                )
                try:
                    context = await browser.new_context(service_workers="block")
                    page = await context.new_page()
                    adapter = BrowserAdapter(page, address, action_timeout_ms=2_000)
                    await assert_browser_guards(adapter, page)
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await server_task


async def assert_browser_guards(adapter, page):
    async def step(role, value=None, **kwargs):
        observation = await adapter.observe()
        target = next(item for item in observation.targets if item.role == role)
        await adapter.execute(observation, target.action, target.id, value, **kwargs)

    await adapter.open_home()
    await step("cart")
    baseline = await adapter.read_cart()
    await adapter.open_home()
    await step("search-input", "무향 물티슈")
    await step("search-submit")
    await step("card")

    observation = await adapter.observe()
    option = next(item for item in observation.targets if item.role == "option")
    await page.locator('[data-zc-control="option"]').first.evaluate(
        "element => element.replaceWith(element.cloneNode(true))"
    )
    with pytest.raises(StaleObservation):
        await adapter.execute(observation, option.action, option.id)

    await step("option")
    offer = next(item for item in await adapter.details() if item.selected)
    await step("quantity", 1)
    await page.locator('input[name="option"]').evaluate(
        "element => { element.value = 'unexpected-variant' }"
    )
    with pytest.raises(BrowserBlocked, match="상품·옵션·수량"):
        await step("add", expected_offer=offer, expected_quantity=1)
    await adapter.open_cart()
    assert await adapter.read_cart() == baseline

    await adapter.open_product(offer.url)
    await step("option")
    await step("quantity", 1)
    await step("add", expected_offer=offer, expected_quantity=1)
    await adapter.open_cart()
    cart = {(line.product_id, line.option_id): line for line in await adapter.read_cart()}
    identity = (offer.product_id, offer.option_id)
    for line in baseline:
        key = (line.product_id, line.option_id)
        assert cart[key].quantity == line.quantity + (key == identity)
    assert cart[identity].unit_price == offer.unit_price

    before_clear = await adapter.read_cart()
    with pytest.raises(StaleObservation):
        await step("clear-cart", expected_cart=baseline)
    assert await adapter.read_cart() == before_clear

    await page.locator('input[name="cart"]').evaluate(
        "element => { element.value = '[]' }"
    )
    with pytest.raises(BrowserBlocked, match="삭제 대상"):
        await step("clear-cart", expected_cart=before_clear)
    await adapter.open_cart()
    assert await adapter.read_cart() == before_clear

    await step("clear-cart", expected_cart=before_clear)
    await adapter.open_cart()
    assert await adapter.read_cart() == ()
    assert all(t.role != "clear-cart" for t in (await adapter.observe()).targets)

    with pytest.raises(BrowserBlocked):
        await adapter.open_product(adapter.home_url + "/checkout")
