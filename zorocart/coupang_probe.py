"""Observe the desktop guest add contract without forwarding any cart write."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import Error as PlaywrightError, Route, async_playwright

_SHOP = "www.coupang.com"
_CART = "cart.coupang.com"
_PRODUCT_PATH = re.compile(r"/vp/products/[0-9]+")
_ASSET_TYPES = {"stylesheet", "image", "font", "script"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_FIELD = re.compile(r"[A-Za-z0-9_.\[\]-]{1,100}")
_SECRET = re.compile(r"token|cookie|secret|password|signature|session|address", re.I)


def _product(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != _SHOP
        or not _PRODUCT_PATH.fullmatch(parsed.path)
        or parsed.fragment
        or any(
            key not in {"itemId", "vendorItemId"} or not re.fullmatch(r"[0-9]+", value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        )
    ):
        raise argparse.ArgumentTypeError("An observed desktop Coupang product URL is required.")
    return url


def _cart(url: str) -> str:
    if url != f"https://{_CART}/cartView.pang":
        raise argparse.ArgumentTypeError("The approved desktop cart read URL is required.")
    return url


def _shape(value, depth: int = 0):
    if depth >= 5:
        return type(value).__name__
    if isinstance(value, dict):
        return {
            key: _shape(entry, depth + 1) for key, entry in list(value.items())[:30]
            if _FIELD.fullmatch(key) and not _SECRET.search(key)
        }
    if isinstance(value, list):
        return [_shape(item, depth + 1) for item in value[:2]]
    if isinstance(value, str):
        if re.fullmatch(r"[0-9]+", value):
            return "digit string"
        if value.startswith(("[", "{")):
            try:
                return {"json_encoded": _shape(json.loads(value), depth + 1)}
            except ValueError:
                pass
    return type(value).__name__


def _request_shape(request) -> dict:
    parsed = urlsplit(request.url)
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    body = request.post_data
    payload = None
    if body:
        try:
            if media_type == "application/json":
                payload = _shape(json.loads(body))
            elif media_type == "application/x-www-form-urlencoded":
                form: dict[str, list[str]] = {}
                for key, value in parse_qsl(body, keep_blank_values=True, max_num_fields=100):
                    form.setdefault(key, []).append(value)
                payload = _shape({key: vals[0] if len(vals) == 1 else vals for key, vals in form.items()})
        except ValueError:
            payload = None
    return {
        "host": parsed.hostname,
        "path": parsed.path,
        "method": request.method,
        "media_type": media_type,
        "query_fields": [
            key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
            if _FIELD.fullmatch(key) and not _SECRET.search(key)
        ],
        "payload_shape": payload,
    }


async def probe(product: str, cart: str, channel: str | None) -> dict:
    read_pages = {_SHOP: urlsplit(product).path, _CART: urlsplit(cart).path}
    captures = []
    capture_active = False
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel=channel, headless=False)
        context = await browser.new_context(service_workers="block")

        async def route_guest_request(route: Route) -> None:
            request = route.request
            url = urlsplit(request.url)
            host = url.hostname
            if capture_active and host in read_pages:
                if request.method in _WRITE_METHODS:
                    shape = _request_shape(request)
                    if shape not in captures:
                        captures.append(shape)
                if request.method == "OPTIONS":
                    origin = request.headers.get("origin")
                    method = request.headers.get("access-control-request-method")
                    if origin == f"https://{_SHOP}" and method in _WRITE_METHODS:
                        # A local preflight response lets Chromium emit the
                        # subsequent write for inspection. Neither goes online.
                        await route.fulfill(status=204, headers={
                            "access-control-allow-origin": origin,
                            "access-control-allow-credentials": "true",
                            "access-control-allow-methods": method,
                            "access-control-allow-headers": request.headers.get(
                                "access-control-request-headers", ""
                            ),
                        })
                        return
            if not capture_active and request.method == "GET" and url.scheme == "https":
                if request.is_navigation_request() and read_pages.get(host) == url.path:
                    await route.continue_()
                    return
                if not request.is_navigation_request() and request.resource_type in _ASSET_TYPES and (
                    host in read_pages or (host and host.endswith(".coupangcdn.com"))
                ):
                    await route.continue_()
                    return
            await route.abort()

        async def block_websocket(websocket):
            await websocket.close()

        await context.route("**/*", route_guest_request)
        await context.route_web_socket("**", block_websocket)
        page = await context.new_page()
        result = {"product_http": None, "guest_add_visible": False, "requests": captures}
        try:
            response = await page.goto(product, wait_until="domcontentloaded", timeout=20_000)
            result["product_http"] = response.status if response else None
            if response is not None and response.status in {401, 403, 429}:
                result["status"] = "access-limited"
                return result
            if page.url != product:
                result["status"] = "unexpected-navigation"
                return result
            button = page.locator("button.prod-cart-btn")
            try:
                await button.wait_for(state="visible", timeout=8_000)
            except PlaywrightError:
                result["status"] = "guest-add-unavailable"
                return result
            if await button.count() != 1 or not await button.is_enabled():
                result["status"] = "guest-add-unavailable"
                return result
            result["guest_add_visible"] = True
            capture_active = True
            try:
                await button.click(timeout=2_000)
            except PlaywrightError:
                pass
            finally:
                await asyncio.sleep(1)
                capture_active = False
            if not captures:
                response = await page.goto(cart, wait_until="domcontentloaded", timeout=20_000)
                result["cart_http"] = response.status if response else None
                result["cart_empty_text"] = (
                    await page.get_by_text(re.compile(r"장바구니에 담긴 상품이 없|장바구니가 비어")).count() > 0
                )
            result["status"] = "captured" if captures else "no-write-attempt-observed"
            return result
        except PlaywrightError:
            result["status"] = "browser-request-unavailable"
            return result
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("product", type=_product)
    parser.add_argument("--cart", type=_cart, default=f"https://{_CART}/cartView.pang")
    parser.add_argument("--channel")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.product, args.cart, args.channel)), ensure_ascii=False, indent=2))
