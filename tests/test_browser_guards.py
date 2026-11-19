import asyncio
import os
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from playwright.async_api import Error as PlaywrightError, async_playwright

from zorocart.browser import LOCAL_DOM, BrowserAdapter, BrowserBlocked, CoupangApproval, StaleObservation
from zorocart.coupang_live import CoupangDesktopAdapter
from zorocart.store import router


def test_live_approval_is_required_before_navigation():
    with pytest.raises(BrowserBlocked):
        BrowserAdapter(None, "https://www.coupang.com", mode="coupang")


def test_live_cart_has_separate_origin_and_path_scope():
    home = "https://www.coupang.com"
    cart = "https://cart.coupang.com"
    approval = CoupangApproval(
        reference="synthetic-scoping-test",
        home_url=home,
        cart_url=cart + "/fixture/cart",
        read_paths=("/", "/fixture/products"),
        add_path="/fixture/add",
        dom=LOCAL_DOM,
        add_fields={"product": "productId", "option": "optionId", "quantity": "count"},
        clear_path="/fixture/clear",
        clear_items_field="items",
        add_origin=cart,
    )
    adapter = BrowserAdapter(None, home, mode="coupang", approval=approval)
    assert adapter._in_scope(cart + "/fixture/cart")
    assert adapter._in_scope(home + "/fixture/products")
    assert not adapter._in_scope(cart + "/fixture/products")
    assert not adapter._in_scope(home + "/fixture/cart")
    assert not adapter._in_scope("https://shop.example/fixture/cart")
    assert adapter._is_write_url(cart + "/fixture/add", "/fixture/add")
    assert adapter._is_write_url(cart + "/fixture/clear", "/fixture/clear")
    assert not adapter._is_write_url(home + "/fixture/clear", "/fixture/clear")
    headers = {"origin": home, "access-control-request-method": "POST"}
    assert not adapter._allowed_preflight(cart + "/fixture/add", headers)
    adapter._pending_path = "/fixture/add"
    assert adapter._allowed_preflight(cart + "/fixture/add", headers)
    assert not adapter._allowed_preflight(cart + "/fixture/clear", headers)


def test_confirmed_desktop_reads_when_no_write_contract_is_configured():
    home = "https://www.coupang.com"
    cart = "https://cart.coupang.com"
    settings = dict(
        reference="synthetic-read-only-test",
        home_url=home,
        cart_url=cart + "/fixture/cart",
        read_paths=("/", "/fixture/search"),
    )
    with pytest.raises(ValueError):
        CoupangApproval(**settings).validate()
    approval = CoupangApproval(**settings, desktop_ui=True)
    adapter = BrowserAdapter(None, home, mode="coupang", approval=approval)
    assert adapter._in_scope(cart + "/fixture/cart")
    assert not any((adapter.can_write, adapter.can_add, adapter.can_clear))
    assert not any((approval.can_write, approval.can_add, approval.can_clear))


def test_native_desktop_guest_monitors_main_responses_without_routes_or_writes():
    approval = CoupangApproval(
        reference="fixture", home_url="https://www.coupang.com/", read_paths=("/",),
        cart_url="https://cart.coupang.com/cartView.pang", desktop_ui=True,
    )
    assert approval.native_read_only
    assert not replace(
        approval, add_path="/fixture/add",
        add_fields={"product": "productId", "option": "optionId", "quantity": "count"},
    ).native_read_only
    assert not replace(
        approval, clear_path="/fixture/clear", clear_items_field="items",
    ).native_read_only

    class Page:
        url = approval.home_url
        main_frame = object()

        def __init__(self):
            self.context = self
            self.listeners = {}

        async def route(self, *_):
            pytest.fail("Native guest reads must retain Chrome's normal read networking")

        def on(self, name, callback):
            self.listeners.setdefault(name, []).append(callback)

    async def exercise():
        page = Page()
        adapter = CoupangDesktopAdapter(page, approval.home_url, mode="coupang", approval=approval)
        await adapter._initialize()
        await adapter._initialize()
        [on_response] = page.listeners["response"]

        def denied(frame):
            return SimpleNamespace(
                status=403, request=SimpleNamespace(frame=frame, is_navigation_request=lambda: True),
            )

        on_response(denied(object()))
        adapter._require_current_url()
        with pytest.raises(BrowserBlocked):
            await adapter._add(None, None, 1)
        with pytest.raises(BrowserBlocked):
            await adapter._clear(None, {}, ())
        assert adapter._pending_path is None
        assert not adapter._write_dispatched

        on_response(denied(page.main_frame))
        with pytest.raises(BrowserBlocked, match="HTTP 403"):
            adapter._require_current_url()

    asyncio.run(exercise())


def test_desktop_guest_cart_recognizes_legacy_empty_page():
    async def exercise():
        approval = CoupangApproval(
            reference="fixture", home_url="https://www.coupang.com/", read_paths=("/",),
            cart_url="https://cart.coupang.com/cartView.pang", desktop_ui=True,
        )
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel=os.getenv("ZOROCART_CHROMIUM_CHANNEL"))
            page = await browser.new_page()

            async def serve(route):
                await route.fulfill(content_type="text/html; charset=utf-8", body="""<!doctype html>
                <html lang="ko"><title>장바구니</title><h1>장바구니(0)</h1>
                <p>장바구니에 담은 상품이 없습니다.</p></html>""")

            await page.route(approval.cart_url, serve)
            await page.goto(approval.cart_url)
            adapter = CoupangDesktopAdapter(page, approval.home_url, mode="coupang", approval=approval)
            assert await adapter.read_cart() == ()
            await browser.close()

    asyncio.run(exercise())


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

    @app.get("/store/product/embedded-frames", response_class=HTMLResponse)
    async def embedded_frames():
        return """<main data-zc-view="detail">
        <iframe src="/store/product/denied-frame"></iframe>
        <iframe src="/outside-scope"></iframe>
        </main>"""

    @app.get("/store/product/denied-frame", response_class=HTMLResponse)
    async def denied_frame():
        return HTMLResponse("<p>Embedded frame denied</p>", status_code=403)

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
                    await assert_child_frame_guards(adapter, page)
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await server_task


async def assert_child_frame_guards(adapter, page):
    # Observe a genuine 403 in an allowed frame and a separate request that
    # the adapter itself aborts. Neither blocks the top-level product view.
    address = adapter.home_url.removesuffix("/store")
    async with (
        page.expect_response(lambda response: response.url.endswith("/denied-frame")) as denied,
        page.expect_event("requestfailed", lambda request: request.url.endswith("/outside-scope")) as outside,
    ):
        await adapter._goto(address + "/store/product/embedded-frames")
    assert (await denied.value).status == 403
    assert (await outside.value).failure
    assert (await adapter.observe()).view == "detail"


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

    blocked_context = await page.context.browser.new_context(service_workers="block")
    try:
        blocked_page = await blocked_context.new_page()
        blocked_adapter = BrowserAdapter(blocked_page, adapter.home_url + "?fault=blocked")
        with pytest.raises(BrowserBlocked, match="HTTP 403"):
            await blocked_adapter.open_home()
    finally:
        await blocked_context.close()
