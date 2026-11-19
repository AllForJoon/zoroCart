import asyncio
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError

from zorocart.shopify_engine import SterlingRate, _OfferView
from zorocart.shopify_live import (
    MCGROCER_CART_URL,
    MCGROCER_URL,
    ShopifyError,
    ShopifyStore,
    ShopifyUncertain,
    ShopifyVariant,
)


def test_default_variant_and_inner_bag_count_are_separate_from_order_quantity():
    title = 'Ocado Tall Drawstring Kitchen Bin Liners 30L 20 Pack'
    variant = ShopifyVariant(100, 301, title, '', 'Drawstring handles.', 'example', 666, 'GBP', True,
                             MCGROCER_URL + '/products/example')
    view = _OfferView(None)
    view.rate = SterlingRate(Decimal('1754.4723'), date(2026, 11, 18))

    offer = view.offer(variant)

    assert offer.label == '기본 옵션'
    assert (offer.pack_count, offer.capacity, offer.capacity_unit) == (1, 20, 'piece')
    assert offer.unit_price == 11685


def test_wipes_pack_evidence_excludes_a_canister_even_with_a_pack_size():
    product = ShopifyVariant(100, 301, 'Fragrance Free Wipes 50', '', 'Pack size: 50 wipes. Resealable canister.',
                             'example', 320, 'GBP', True, MCGROCER_URL + '/products/example')
    view = _OfferView(None)
    view.rate = SterlingRate(Decimal('1754.4723'), date(2026, 11, 18))

    assert '판매 설명에서 팩형 포장 확인' not in view.offer(product).attributes
    assert '판매 설명에서 팩형 포장 확인' in view.offer(replace(product, description='50 wipes in a pack.')).attributes


_WIPES = (101, 601, "wipes-60", "Unscented Wipes 60 Count", 128)
_BAGS = (102, 602, "bags-20", "30L Drawstring Handle Bags 20 Pack", 666)
_PRODUCTS = (_WIPES, _BAGS)
_CLEAR = MCGROCER_CART_URL + "/clear.js"
_ADD = MCGROCER_CART_URL + "/add.js"
_READ = MCGROCER_CART_URL + ".js"


class Merchant:
    def __init__(self):
        self.items: dict[int, int] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.add_timeout: str | None = None

    @property
    def writes(self):
        return [(url, data) for method, url, data in self.calls if method == "POST" and url in {_ADD, _CLEAR}]

    def cart_payload(self):
        rows = []
        for identity, quantity in self.items.items():
            product, _, _, title, price = next(
                (item for item in _PRODUCTS if item[1] == identity),
                (301, identity, "old-1", "Existing item", 100),
            )
            rows.append({
                "product_id": product, "variant_id": identity, "product_title": title,
                "variant_title": None, "quantity": quantity, "original_price": price,
                "final_line_price": quantity * price,
            })
        return {
            "currency": "GBP", "items": rows, "item_count": sum(self.items.values()),
            "total_price": sum(row["final_line_price"] for row in rows),
        }

    async def fetch(self, url, *, method, data, max_redirects, max_retries, **_):
        assert max_redirects == max_retries == 0
        self.calls.append((method, url, data))
        if url == "https://mcgrocer-com.myshopify.com/api/ucp/mcp" and method == "POST":
            assert data["params"]["name"] == "search_catalog"
            payload = {"result": {"structuredContent": {"products": [
                {"id": f"gid://shopify/Product/{product}", "handle": handle,
                 "url": f"{MCGROCER_URL}/products/{handle}"}
                for product, _, handle, _, _ in _PRODUCTS
            ]}}}
        elif url == _READ and method == "GET":
            payload = self.cart_payload()
        elif url == _CLEAR and method == "POST":
            assert data == {}
            self.items.clear()
            payload = self.cart_payload()
        elif url == _ADD and method == "POST":
            if self.add_timeout == "before":
                raise PlaywrightError("Simulated timeout before processing")
            for line in data["items"]:
                self.items[line["id"]] = self.items.get(line["id"], 0) + line["quantity"]
            if self.add_timeout == "after":
                raise PlaywrightError("Simulated timeout after processing")
            payload = self.cart_payload()
        else:
            product = next(
                (item for item in _PRODUCTS if url == f"{MCGROCER_URL}/products/{item[2]}.js"),
                None,
            )
            assert product is not None and method == "GET", f"Unexpected fixture request {method} {url}"
            product_id, variant, handle, title, price = product
            payload = {
                "id": product_id, "handle": handle, "title": title,
                "description": "<p>Single pack</p><script>not visible</script>",
                "variants": [{"id": variant, "title": "Default Title", "available": True, "price": price}],
            }

        async def read_json():
            return payload

        async def dispose():
            pass

        return SimpleNamespace(url=url, status=200, headers={}, json=read_json, dispose=dispose)


class Page:
    def __init__(self, merchant):
        self.url = "about:blank"
        self.context = SimpleNamespace(request=merchant)

    async def goto(self, url, **_):
        assert url in {MCGROCER_URL + "/", MCGROCER_CART_URL}
        self.url = url
        return SimpleNamespace(status=200)


async def open_store():
    merchant = Merchant()
    store = ShopifyStore(Page(merchant))
    await store.open()
    return merchant, store


def test_guest_clear_adds_two_lines_once_and_verifies_the_same_cart():
    async def exercise():
        merchant, store = await open_store()
        merchant.items = {3000: 1}
        before = await store.read_cart()
        variants = await store.search("fragrance free wet wipes")
        assert [variant.description for variant in variants] == ["Single pack", "Single pack"]
        assert [variant.variant_id for variant in variants] == [_WIPES[1], _BAGS[1]]

        blank = await store.clear_cart(before)
        assert blank.item_count == 0
        cart = await store.add_to_cart([(variant, 1) for variant in variants], expected=blank, budget_minor=794)
        assert (cart.currency, cart.item_count, cart.total_minor) == ("GBP", 2, 794)
        assert [line.variant_id for line in cart.lines] == [_WIPES[1], _BAGS[1]]
        assert merchant.writes == [
            (_CLEAR, {}),
            (_ADD, {"items": [{"id": _WIPES[1], "quantity": 1}, {"id": _BAGS[1], "quantity": 1}]}),
        ]
        assert await store.open_cart() == cart

    asyncio.run(exercise())


def test_stale_cart_and_over_budget_stop_before_any_write():
    async def exercise():
        merchant, store = await open_store()
        snapshot = await store.read_cart()
        merchant.items = {3000: 1}
        with pytest.raises(ShopifyError, match="변경"):
            await store.clear_cart(snapshot)
        assert merchant.items == {3000: 1}
        assert not merchant.writes

        merchant.items.clear()
        variants = await store.search("30L drawstring bin liners")
        with pytest.raises(ShopifyError, match="예산"):
            await store.add_to_cart([(variant, 1) for variant in variants], expected=snapshot, budget_minor=793)
        stale_variant = replace(variants[0], price_minor=variants[0].price_minor - 1)
        with pytest.raises(ShopifyError, match="바뀌었습니다"):
            await store.add_to_cart([(stale_variant, 1), (variants[1], 1)], expected=snapshot)
        assert merchant.items == {}
        assert not merchant.writes

    asyncio.run(exercise())


def test_account_page_and_unapproved_routes_stop_before_requests():
    async def exercise():
        merchant, store = await open_store()
        before = await store.read_cart()
        calls = list(merchant.calls)
        for path in ("/account", "/checkout"):
            store.page.url = MCGROCER_URL + path
            with pytest.raises(ShopifyError, match="브라우저 화면"):
                await store.clear_cart(before)
        store.page.url = MCGROCER_URL + "/"
        with pytest.raises(ShopifyError, match="확인한 상품만"):
            await store.details("https://unrelated.shop/products/wipes-60")
        with pytest.raises(ShopifyError, match="범위를 벗어났습니다"):
            await store._json(MCGROCER_URL + "/checkout", data={}, mutation=True)
        assert merchant.calls == calls

    asyncio.run(exercise())


@pytest.mark.parametrize("timeout", ["before", "after"])
def test_add_timeout_uses_cart_read_to_reconcile_without_a_second_post(timeout):
    async def exercise():
        merchant, store = await open_store()
        before = await store.read_cart()
        variants = await store.search("fragrance free wet wipes")
        merchant.add_timeout = timeout

        if timeout == "after":
            cart = await store.add_to_cart([(variant, 1) for variant in variants], expected=before)
            assert cart.item_count == 2 and cart.total_minor == 794
        else:
            with pytest.raises(ShopifyUncertain):
                await store.add_to_cart([(variant, 1) for variant in variants], expected=before)
            assert merchant.items == {}
        assert [url for url, _ in merchant.writes] == [_ADD]
        assert merchant.calls[-1] == ("GET", _READ, None)

    asyncio.run(exercise())
