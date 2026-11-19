import asyncio
import os
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from zorocart.browser import BrowserBlocked, StaleObservation
from zorocart.naver_live import NAVER_STORE_URL, NaverStoreAdapter


SEARCH_INPUT = """<input type="text" name="query" placeholder="검색어를 입력해 주세요">"""
RESULTS = f"""<!doctype html><title>네이버플러스 스토어 검색</title>
{SEARCH_INPUT}
<button class="_headerControl_button_fixture N=a:gnb.directcart" type="button"
        onclick="location.href='https://nid.naver.com/nidlogin.login?url=private-redirect'">장바구니</button>
<article class="basicProductCard_basic_product_card__fixture">
  <a class="basicProductCard_link__fixture" href="https://smartstore.naver.com/shop1/products/100">
    <span class="blind">무향 물티슈 1팩</span>
  </a>
  <span>6,900원</span>
</article>
<article class="basicProductCard_basic_product_card__fixture">
  <a class="basicProductCard_link__fixture" href="https://smartstore.naver.com/shop2/products/100">
    <span class="blind">무향 물티슈 다른 판매처</span>
  </a>
  <span>8,900원</span>
</article>"""


def test_guest_home_redirect_stops_without_using_login():
    asyncio.run(exercise_guest(redirect_home=True))


def test_guest_search_observes_cards_and_stops_on_cart_login():
    asyncio.run(exercise_guest(redirect_home=False))


async def exercise_guest(*, redirect_home: bool):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel=os.environ.get("ZOROCART_CHROMIUM_CHANNEL"),
            executable_path=os.environ.get("ZOROCART_CHROMIUM_EXECUTABLE"),
        )
        try:
            page = await browser.new_page()
            requests = []

            async def fixture(route):
                request = route.request
                url = urlsplit(request.url)
                requests.append((request.method, url.hostname, url.path))
                if (url.hostname, url.path) == ("shopping.naver.com", "/ns/home"):
                    if redirect_home:
                        html = """<!doctype html><script>
                        location.replace('https://nid.naver.com/nidlogin.login?url=private-redirect');
                        </script>"""
                    else:
                        html = f"""<!doctype html><title>네이버플러스 스토어</title>
                        {SEARCH_INPUT}<script>
                        document.addEventListener('keydown', event => {{
                          if (event.key === 'Enter' && event.target.matches('input[name=query]')) {{
                            location.href = 'https://search.shopping.naver.com/ns/search?query=' +
                              encodeURIComponent(event.target.value);
                          }}
                        }});
                        </script>"""
                elif (url.hostname, url.path) == ("search.shopping.naver.com", "/ns/search"):
                    html = RESULTS
                elif url.hostname == "nid.naver.com":
                    html = '<!doctype html><title>NAVER 로그인</title><input type="password">'
                else:
                    await route.abort()
                    return
                await route.fulfill(content_type="text/html; charset=utf-8", body=html)

            await page.route("**/*", fixture)
            adapter = NaverStoreAdapter(page, NAVER_STORE_URL, action_timeout_ms=2_000)
            assert not (adapter.can_write or adapter.can_add or adapter.can_clear)

            if redirect_home:
                with pytest.raises(BrowserBlocked, match="홈에서 로그인을 요구") as error:
                    await adapter.open_home()
                assert "private-redirect" not in str(error.value)
                assert [host for _, host, _ in requests] == ["shopping.naver.com", "nid.naver.com"]
                return

            await adapter.open_home()
            observation = await adapter.observe()
            field = next(target for target in observation.targets if target.role == "search-input")
            await adapter.execute(observation, field.action, field.id, "무향 물티슈")
            observation = await adapter.observe()
            submit = next(target for target in observation.targets if target.role == "search-submit")
            await page.locator("input[name=query]").evaluate(
                "node => node.replaceWith(node.cloneNode(true))"
            )
            with pytest.raises(StaleObservation):
                await adapter.execute(observation, submit.action, submit.id)
            observation = await adapter.observe()
            submit = next(target for target in observation.targets if target.role == "search-submit")
            await adapter.execute(observation, submit.action, submit.id)

            observation = await adapter.observe()
            assert observation.view == "results"
            cards = [target for target in observation.targets if target.role == "card"]
            assert [(card.key, card.facts["price"]) for card in cards] == [
                ("smartstore.naver.com:shop1:100", "6,900원"),
                ("smartstore.naver.com:shop2:100", "8,900원"),
            ]
            with pytest.raises(BrowserBlocked):
                await adapter.execute(observation, cards[0].action, cards[0].id)
            with pytest.raises(BrowserBlocked, match="장바구니에서 로그인을 요구") as error:
                await adapter.open_cart()
            assert "private-redirect" not in str(error.value)
            assert [method for method, _, _ in requests] == ["GET", "GET", "GET"]
        finally:
            await browser.close()
