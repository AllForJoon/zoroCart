"""Bounded McGrocer discovery and browser-session Shopify Ajax carts."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError, Page

MCGROCER_URL = "https://mcgrocer.com"
MCGROCER_CART_URL = MCGROCER_URL + "/cart"
_CART_READ_URL = MCGROCER_CART_URL + ".js"
_CART_WRITES = {MCGROCER_CART_URL + suffix for suffix in ("/add.js", "/clear.js")}
_CATALOG_URL = "https://mcgrocer-com.myshopify.com/api/ucp/mcp"
_CATALOG_PROFILE = "https://shopify.dev/ucp/agent-profiles/examples/2026-08-25/valid-with-capabilities.json"
_HANDLE = re.compile(r"[a-z0-9][a-z0-9-]{0,200}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_ACCESS_STATUS = {401, 403, 429, 430}


class ShopifyError(RuntimeError):
    """A store request or a required piece of evidence could not be verified."""


class ShopifyAccessBlocked(ShopifyError):
    """The merchant or platform restricted access to the required surface."""


class ShopifyUncertain(ShopifyError):
    """A cart write must be reconciled by reading the current cart."""


@dataclass(frozen=True)
class ShopifyVariant:
    product_id: int
    variant_id: int
    title: str
    label: str
    description: str
    handle: str
    price_minor: int
    currency: str
    available: bool
    url: str


@dataclass(frozen=True)
class ShopifyCartLine:
    product_id: int
    variant_id: int
    title: str
    label: str
    quantity: int
    price_minor: int
    line_total_minor: int


@dataclass(frozen=True)
class ShopifyCart:
    currency: str
    lines: tuple[ShopifyCartLine, ...]
    total_minor: int

    @property
    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)


class _Description(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.hidden: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.hidden = tag

    def handle_endtag(self, tag):
        if self.hidden == tag:
            self.hidden = None

    def handle_data(self, data):
        if self.hidden is None:
            self.parts.append(data)


def _plain_text(value, limit: int = 260) -> str:
    if not isinstance(value, str):
        return ""
    unescaped = unescape(value)
    return " ".join(part for part in unescaped.split() if part.isprintable())[:limit]


def _description(value) -> str:
    if not isinstance(value, str):
        return ""
    parser = _Description()
    parser.feed(value[:40_000])
    parser.close()
    return _plain_text(" ".join(parser.parts), 2_400)


def _int(value, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ShopifyError("Shopify의 상품 식별자, 가격 또는 수량을 확인할 수 없습니다.")
    return value


def _variant_label(value) -> str:
    label = _plain_text(value)
    return label if label.casefold() != "default title" else ""


def _product_handle(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc != "mcgrocer.com" or parsed.fragment:
        return None
    if not parsed.path.startswith("/products/"):
        return None
    handle = parsed.path.removeprefix("/products/").rstrip("/")
    return handle if _HANDLE.fullmatch(handle) else None


def _permitted_api(url: str, data: dict | None, mutation: bool) -> bool:
    if mutation:
        return url in _CART_WRITES and data is not None
    if data is not None:
        return url == _CATALOG_URL and data.get("params", {}).get("name") == "search_catalog"
    if url == _CART_READ_URL:
        return True
    return url.endswith(".js") and _product_handle(url[:-3]) is not None


def _cart(data: dict) -> ShopifyCart:
    currency = data.get("currency")
    rows = data.get("items")
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency) or not isinstance(rows, list):
        raise ShopifyError("Shopify 장바구니 통화 또는 상품 목록을 확인할 수 없습니다.")
    lines = []
    for row in rows:
        if not isinstance(row, dict):
            raise ShopifyError("Shopify 장바구니 행을 확인할 수 없습니다.")
        title = _plain_text(row.get("product_title") or row.get("title"))
        if not title:
            raise ShopifyError("Shopify 장바구니의 상품명을 확인할 수 없습니다.")
        lines.append(ShopifyCartLine(
            product_id=_int(row.get("product_id"), minimum=1),
            variant_id=_int(row.get("variant_id", row.get("id")), minimum=1),
            title=title,
            label=_variant_label(row.get("variant_title")),
            quantity=_int(row.get("quantity"), minimum=1),
            price_minor=_int(row.get("original_price", row.get("price"))),
            line_total_minor=_int(row.get("final_line_price", row.get("line_price"))),
        ))
    lines.sort(key=lambda line: (line.variant_id, line.quantity, line.price_minor, line.line_total_minor))
    cart = ShopifyCart(currency, tuple(lines), _int(data.get("total_price")))
    if (
        cart.item_count != _int(data.get("item_count"))
        or (not cart.lines and cart.total_minor)
        or cart.total_minor > sum(line.line_total_minor for line in cart.lines)
    ):
        raise ShopifyError("Shopify 장바구니 상품 수량과 합계를 대조할 수 없습니다.")
    return cart


def _variants(data: dict, handle: str, currency: str) -> tuple[ShopifyVariant, ...]:
    title = _plain_text(data.get("title"))
    product_id = _int(data.get("id"), minimum=1)
    raw_variants = data.get("variants")
    if data.get("handle") != handle or not title or not isinstance(raw_variants, list) or not raw_variants:
        raise ShopifyError("McGrocer 상품 상세의 판매 옵션을 확인할 수 없습니다.")
    description = _description(data.get("description"))
    result = []
    seen: set[int] = set()
    for variant in raw_variants:
        if not isinstance(variant, dict):
            raise ShopifyError("McGrocer 상품 상세의 판매 옵션을 확인할 수 없습니다.")
        identity = _int(variant.get("id"), minimum=1)
        available = variant.get("available")
        if identity in seen or type(available) is not bool:
            raise ShopifyError("McGrocer 상품 상세의 판매 옵션 또는 재고를 확인할 수 없습니다.")
        seen.add(identity)
        result.append(ShopifyVariant(
            product_id=product_id,
            variant_id=identity,
            title=title,
            label=_variant_label(variant.get("title")),
            description=description,
            handle=handle,
            price_minor=_int(variant.get("price")),
            currency=currency,
            available=available and not variant.get("requires_selling_plan") and not data.get("requires_selling_plan"),
            url=f"{MCGROCER_URL}/products/{handle}",
        ))
    return tuple(result)


class ShopifyStore:
    """Read McGrocer's advertised catalog and change only the current guest cart."""

    def __init__(self, page: Page, *, timeout_ms: int = 12_000):
        self.page = page
        self.timeout_ms = timeout_ms
        self._operations = asyncio.Lock()
        self._observed: dict[str, int] = {}
        self._currency: str | None = None

    @property
    def currency(self) -> str | None:
        return self._currency

    async def _navigate(self, url: str) -> None:
        if url not in {MCGROCER_URL + "/", MCGROCER_CART_URL}:
            raise ShopifyError("McGrocer의 지정 조회 화면만 열 수 있습니다.")
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except PlaywrightError as exc:
            raise ShopifyError("McGrocer 브라우저 화면을 열 수 없습니다.") from exc
        if response and response.status in _ACCESS_STATUS:
            raise ShopifyAccessBlocked(f"McGrocer가 브라우저 접근을 제한했습니다 (HTTP {response.status}).")
        if not response or response.status != 200 or self.page.url != url:
            raise ShopifyError("McGrocer의 지정 조회 화면을 확인할 수 없습니다.")

    async def open(self) -> None:
        await self._navigate(MCGROCER_URL + "/")
        await self.read_cart()

    def _check_page(self) -> None:
        if self.page.url not in {MCGROCER_URL + "/", MCGROCER_CART_URL}:
            raise ShopifyError("McGrocer 장바구니와 같은 브라우저 화면인지 확인할 수 없습니다.")

    async def _json(self, url: str, *, data: dict | None = None, mutation: bool = False) -> dict:
        self._check_page()
        if not _permitted_api(url, data, mutation):
            raise ShopifyError("Shopify 요청이 지정된 상품 조회·장바구니 범위를 벗어났습니다.")
        try:
            response = await self.page.context.request.fetch(
                url, method="GET" if data is None else "POST", data=data,
                headers={"Accept": "application/json, text/event-stream" if url == _CATALOG_URL else "application/json"},
                max_redirects=0, max_retries=0, fail_on_status_code=False, timeout=self.timeout_ms,
            )
        except PlaywrightError as exc:
            error = ShopifyUncertain if mutation else ShopifyError
            raise error("Shopify 요청 응답을 확인할 수 없습니다.") from exc
        try:
            if response.status in _ACCESS_STATUS or response.headers.get("cf-mitigated") == "challenge":
                raise ShopifyAccessBlocked(f"Shopify가 요청 접근을 제한했습니다 (HTTP {response.status}).")
            if response.url != url or 300 <= response.status < 400:
                raise ShopifyAccessBlocked("Shopify 요청이 지정된 주소를 벗어났습니다.")
            if not 200 <= response.status < 300:
                error = ShopifyUncertain if mutation else ShopifyError
                raise error(f"Shopify가 요청을 처리하지 못했습니다 (HTTP {response.status}).")
            try:
                result = await response.json()
            except (PlaywrightError, ValueError) as exc:
                error = ShopifyUncertain if mutation else ShopifyError
                raise error("Shopify 응답 형식을 확인할 수 없습니다.") from exc
            if not isinstance(result, dict):
                error = ShopifyUncertain if mutation else ShopifyError
                raise error("Shopify 응답 형식을 확인할 수 없습니다.")
            return result
        finally:
            await response.dispose()

    async def read_cart(self) -> ShopifyCart:
        cart = _cart(await self._json(_CART_READ_URL))
        if self._currency is not None and cart.currency != self._currency:
            raise ShopifyError("McGrocer에 표시된 통화가 실행 중 바뀌었습니다.")
        self._currency = cart.currency
        return cart

    async def search(self, query: str) -> tuple[ShopifyVariant, ...]:
        if (
            not isinstance(query, str) or not 1 <= len(query) <= 120
            or query.strip() != query or not query.isprintable()
        ):
            raise ShopifyError("조회할 Shopify 상품 검색어를 확인할 수 없습니다.")
        if self._currency is None:
            await self.read_cart()
        data = await self._json(_CATALOG_URL, data={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_catalog",
                "arguments": {
                    "meta": {"ucp-agent": {"profile": _CATALOG_PROFILE}},
                    "catalog": {"query": query, "pagination": {"limit": 6}, "context": {"address_country": "GB"}},
                },
            },
        })
        result = data.get("result")
        if not isinstance(result, dict) or result.get("isError") or data.get("error"):
            raise ShopifyError("McGrocer가 광고한 Shopify Catalog의 검색 결과를 확인할 수 없습니다.")
        content = result.get("structuredContent")
        if content is None:
            try:
                content = json.loads(result["content"][0]["text"])
            except (IndexError, KeyError, TypeError, ValueError) as exc:
                raise ShopifyError("Shopify Catalog의 검색 결과 형식을 확인할 수 없습니다.") from exc
        products = content.get("products") if isinstance(content, dict) else None
        if not isinstance(products, list):
            raise ShopifyError("Shopify Catalog의 상품 목록을 확인할 수 없습니다.")
        candidates = []
        seen: set[str] = set()
        for product in products[:6]:
            if not isinstance(product, dict) or not isinstance(product.get("url"), str):
                continue
            handle = _product_handle(product["url"])
            identity = product.get("id")
            if not handle or product.get("handle") != handle or handle in seen or not isinstance(identity, str):
                continue
            match = re.fullmatch(r"gid://shopify/Product/(\d+)", identity)
            if match is None:
                continue
            product_id = int(match[1])
            if self._observed.get(handle, product_id) != product_id:
                raise ShopifyError("Shopify 검색 결과의 상품 식별자가 바뀌었습니다.")
            self._observed[handle] = product_id
            seen.add(handle)
            try:
                candidates.extend(await self.details(product["url"]))
            except ShopifyAccessBlocked:
                raise
            except ShopifyError:
                continue
        return tuple(candidates)

    async def details(self, url: str) -> tuple[ShopifyVariant, ...]:
        handle = _product_handle(url)
        if handle is None or handle not in self._observed or self._currency is None:
            raise ShopifyError("McGrocer 검색에서 확인한 상품만 상세 조회할 수 있습니다.")
        variants = _variants(await self._json(f"{MCGROCER_URL}/products/{handle}.js"), handle, self._currency)
        if variants[0].product_id != self._observed[handle]:
            raise ShopifyError("검색 결과와 McGrocer 상품 상세의 식별자가 일치하지 않습니다.")
        return variants

    async def refresh(self, variant: ShopifyVariant) -> ShopifyVariant:
        current = next(
            (item for item in await self.details(variant.url)
             if (item.product_id, item.variant_id) == (variant.product_id, variant.variant_id)),
            None,
        )
        if current is None or current.handle != variant.handle:
            raise ShopifyError("McGrocer에서 선택한 판매 옵션을 다시 확인할 수 없습니다.")
        return current

    async def _reconcile(self, path: str, data: dict) -> ShopifyCart:
        try:
            await self._json(MCGROCER_URL + path, data=data, mutation=True)
        except ShopifyUncertain:
            pass
        try:
            return await self.read_cart()
        except ShopifyAccessBlocked:
            raise
        except ShopifyError as exc:
            raise ShopifyUncertain("Shopify 변경 뒤 장바구니 반영을 확인할 수 없습니다.") from exc

    async def clear_cart(self, expected: ShopifyCart) -> ShopifyCart:
        async with self._operations:
            current = await self.read_cart()
            if current != expected:
                raise ShopifyError("McGrocer 장바구니가 확인 이후 변경되어 비우기를 중단했습니다.")
            if not current.lines:
                return current
            cleared = await self._reconcile("/cart/clear.js", {})
            if cleared.lines or cleared.total_minor:
                raise ShopifyUncertain("McGrocer 장바구니가 완전히 비워졌는지 확인할 수 없습니다.")
            return cleared

    async def add_to_cart(
        self,
        orders: Sequence[tuple[ShopifyVariant, int]],
        *,
        expected: ShopifyCart,
        budget_minor: int | None = None,
    ) -> ShopifyCart:
        if not orders or len(orders) > 3 or (budget_minor is not None and (
            type(budget_minor) is not int or budget_minor < 0
        )):
            raise ShopifyError("McGrocer 주문 품목 또는 상품가 예산을 확인할 수 없습니다.")
        async with self._operations:
            selected: dict[int, tuple[ShopifyVariant, int]] = {}
            for variant, quantity in orders:
                if type(quantity) is not int or quantity <= 0 or variant.variant_id in selected:
                    raise ShopifyError("McGrocer 주문에서 판매 옵션과 수량이 유일하게 확인돼야 합니다.")
                current = await self.refresh(variant)
                if current != variant or not current.available:
                    raise ShopifyError("McGrocer의 선택 옵션, 가격 또는 재고가 바뀌었습니다.")
                selected[current.variant_id] = (current, quantity)
            before = await self.read_cart()
            if before != expected or before.lines:
                raise ShopifyError("McGrocer 상품을 담기 전에 빈 장바구니를 다시 확인해야 합니다.")
            subtotal = sum(variant.price_minor * quantity for variant, quantity in selected.values())
            if budget_minor is not None and subtotal > budget_minor:
                raise ShopifyError("McGrocer 상품가가 확인된 예산을 초과합니다.")
            result = await self._reconcile("/cart/add.js", {
                "items": [{"id": variant_id, "quantity": quantity} for variant_id, (_, quantity) in selected.items()],
            })
            if len(result.lines) != len(selected) or result.total_minor > subtotal:
                raise ShopifyUncertain("McGrocer 장바구니에 담긴 품목 수 또는 상품가를 확인할 수 없습니다.")
            for line in result.lines:
                match = selected.get(line.variant_id)
                if match is None:
                    raise ShopifyUncertain("선택 범위 밖의 판매 옵션이 McGrocer 장바구니에 표시됐습니다.")
                variant, quantity = match
                if (line.product_id, line.quantity, line.price_minor) != (
                    variant.product_id, quantity, variant.price_minor,
                ) or line.line_total_minor > variant.price_minor * quantity:
                    raise ShopifyUncertain("McGrocer 장바구니의 판매 옵션, 수량 또는 상품가가 달라졌습니다.")
            return result

    async def open_cart(self) -> ShopifyCart:
        await self._navigate(MCGROCER_CART_URL)
        return await self.read_cart()
