"""Read the visible signed-out NaverPlus Store search and cart entry points."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as PlaywrightError, Page

from .browser import NAVER_STORE_URL, BrowserAdapter, BrowserBlocked, BrowserError, StaleObservation

_HOME = ("shopping.naver.com", "/ns/home")
_RESULTS = ("search.shopping.naver.com", "/ns/search")
_LOGIN = "nid.naver.com"
_SEARCH_INPUT = 'input[name="query"][placeholder="검색어를 입력해 주세요"]'
_RESULT_CARD = '[class*="basicProductCard_basic_product_card__"]'
_LOGIN_REASONS = {
    "home": "네이버플러스 스토어 홈에서 로그인을 요구해 비로그인 실행을 중단했습니다.",
    "search": "네이버플러스 스토어 검색에서 로그인을 요구해 비로그인 실행을 중단했습니다.",
    "cart": "네이버플러스 스토어 장바구니에서 로그인을 요구해 비로그인 실행을 중단했습니다.",
}

_SNAPSHOT = r"""
({inputSelector, cardSelector, view}) => {
  const clean = value => (value || "").replace(/\s+/g, " ").trim().slice(0, 240);
  const visible = el => Boolean(el?.isConnected && el.getClientRects().length &&
    !el.closest("[hidden], [inert], [aria-hidden=true]") && getComputedStyle(el).visibility === "visible");
  const find = (selector, root = document) => [...root.querySelectorAll(selector)].filter(visible);
  const label = el => clean(el.getAttribute("aria-label") || el.innerText || el.getAttribute("title"));
  const headings = find("h1, h2, [role=alert], [role=dialog]").map(el => clean(el.innerText));
  const blockers = {
    signedIn: find("a,button").some(el => label(el) === "로그아웃"),
    login: Boolean(find("input[type=password]").length) ||
      headings.some(text => /^(?:네이버\s*로그인|로그인이?\s*(?:필요|요구)|로그인\s*후)/i.test(text)),
    access: Boolean(find("[data-sitekey], iframe[src*=captcha]").length) ||
      /access denied|forbidden|에러 페이지/i.test(document.title) ||
      headings.some(text => /^(?:Access Denied|Forbidden|접근이?\s*제한|비정상적인 접근|보안 확인|자동입력 방지)/i.test(text)),
  };

  const nodes = [];
  const controls = [];
  function add(el, role, title, key = role, data = {}) {
    if (!el || el.matches(":disabled") || el.closest("[aria-disabled=true]")) return;
    nodes.push(el);
    controls.push({
      role, key, label: title, href: null, destination: "", options: [], selected: false,
      tag: el.tagName.toLowerCase(), aria_role: el.getAttribute("role") || "",
      value: role === "search-input" || role === "search-submit" ? clean(el.value) : "",
      card_facts: data,
    });
  }

  const inputs = find(inputSelector);
  const input = inputs.length === 1 ? inputs[0] : null;
  if (input && !input.readOnly && !input.form && input.type === "text") {
    add(input, "search-input", "네이버플러스 스토어 상품 검색");
    add(input, "search-submit", "현재 검색어로 검색");
  }

  if (view === "results") {
    const carts = find('button[class~="N=a:gnb.directcart"]')
      .filter(el => label(el) === "장바구니" && el.type === "button" && !el.form);
    if (carts.length === 1) add(carts[0], "cart", "장바구니");

    const seen = new Set();
    for (const card of find(cardSelector)) {
      const link = find('a[class*="basicProductCard_link__"]', card)[0];
      if (!link) continue;
      let url;
      try { url = new URL(link.href); } catch { continue; }
      if (url.protocol !== "https:" || url.username || url.password ||
        !["smartstore.naver.com", "brand.naver.com"].includes(url.host)) continue;
      const match = /^\/([A-Za-z0-9_-]{1,55})\/products\/(\d{1,24})\/?$/.exec(url.pathname);
      if (!match) continue;
      const key = `${url.hostname}:${match[1]}:${match[2]}`;
      if (seen.has(key)) continue;
      const title = label(link) || clean(find(".blind", link)[0]?.textContent);
      if (!title) continue;
      const cardFacts = {name: [{text: title, unit: ""}]};
      const prices = [...new Set((card.innerText || "").match(/(?:\d[\d,]*)\s*원/g) || [])];
      if (prices.length === 1) cardFacts.price = [{text: clean(prices[0]), unit: ""}];
      add(link, "card", title, key, cardFacts);
      seen.add(key);
      if (seen.size >= 6) break;
    }
  }
  return {root: document.documentElement, nodes, payload: {
    view, controls, blockers, offers: [], rows: [], quantities: [], empty: false,
  }};
}
"""


class NaverStoreAdapter(BrowserAdapter):
    """Use observed search controls in Chrome without account or cart writes."""

    def __init__(
        self,
        page: Page,
        store_url: str = NAVER_STORE_URL,
        mode: str = "naver",
        approval=None,
        *,
        action_timeout_ms: int = 4_000,
        max_steps: int = 140,
        deadline: float | None = None,
    ):
        if mode != "naver":
            raise BrowserBlocked("네이버플러스 스토어 실행에는 네이버 비로그인 모드가 필요합니다.")
        super().__init__(
            page, store_url, mode=mode, approval=approval, action_timeout_ms=action_timeout_ms,
            max_steps=max_steps, deadline=deadline,
        )
        self._phase = "home"
        self._observers_ready = False
        self._interruption: str | None = None

    async def _initialize(self) -> None:
        if not self._observers_ready:
            self.page.on("response", self._on_response)
            self.page.on("dialog", self._dismiss_dialog)
            self.page.on("popup", self._close_popup)
            self._observers_ready = True

    async def _dismiss_dialog(self, dialog) -> None:
        if "로그인" in dialog.message:
            self._interruption = _LOGIN_REASONS[self._phase]
        else:
            self._interruption = "네이버플러스 스토어가 확인이 필요한 대화 상자를 표시했습니다."
        await dialog.dismiss()

    async def _close_popup(self, page: Page) -> None:
        self._interruption = (
            _LOGIN_REASONS[self._phase] if urlsplit(page.url).hostname == _LOGIN
            else "네이버플러스 스토어가 확인되지 않은 새 창을 열었습니다."
        )
        if not page.is_closed():
            await page.close()

    def _require_current_url(self) -> None:
        if self._interruption:
            raise BrowserBlocked(self._interruption)
        current = urlsplit(self.page.url)
        if current.hostname == _LOGIN:
            raise BrowserBlocked(_LOGIN_REASONS[self._phase])
        if current.hostname == "shopv.pstatic.net" and current.path.startswith("/web/maintenance/"):
            raise BrowserBlocked("네이버플러스 스토어가 서비스 오류 화면을 표시했습니다.")
        super()._require_current_url()

    async def open_home(self) -> None:
        self._phase = "home"
        await super().open_home()
        try:
            await self.page.locator(f"{_SEARCH_INPUT}:visible").first.wait_for(
                state="visible", timeout=self.action_timeout_ms,
            )
        except PlaywrightError as exc:
            self._require_current_url()
            await self._read()
            raise BrowserError("비로그인 화면의 상품 검색창을 확인할 수 없습니다.") from exc

    async def _capture(self):
        self._require_current_url()
        current = urlsplit(self.page.url)
        view = "results" if (current.hostname, current.path) == _RESULTS else "home"
        try:
            handle = await self.page.evaluate_handle(_SNAPSHOT, {
                "inputSelector": _SEARCH_INPUT, "cardSelector": _RESULT_CARD, "view": view,
            })
            payload = await handle.get_property("payload")
            data = await payload.json_value()
            await payload.dispose()
        except PlaywrightError as exc:
            self._require_current_url()
            raise StaleObservation("네이버플러스 스토어의 화면이 바뀌었습니다.") from exc
        blockers = data["blockers"]
        if any(blockers.values()):
            await handle.dispose()
            if blockers["login"]:
                raise BrowserBlocked(_LOGIN_REASONS[self._phase])
            if blockers["signedIn"]:
                raise BrowserBlocked("네이버 로그인 상태가 감지되어 비로그인 실행을 중단했습니다.")
            raise BrowserBlocked("네이버플러스 스토어에서 접근 제한을 확인했습니다.")
        return handle, data

    async def execute(self, observation, action, target_id=None, value=None, **guards) -> None:
        target = next((item for item in observation.targets if item.id == target_id), None)
        if target and target.role == "card":
            raise BrowserBlocked("네이버플러스 스토어 모드는 검색 화면의 상품 카드까지 조회합니다.")
        if not target or action != "CLICK" or target.role not in {"search-submit", "cart"}:
            await super().execute(observation, action, target_id, value, **guards)
            return

        search = target.role == "search-submit"
        self._phase = "search" if search else "cart"
        node = self._nodes.get(target.id)
        if observation is not self._active or self.page.url != self._observation_url or node is None:
            raise StaleObservation("현재 네이버플러스 스토어의 조작 대상을 다시 관찰해야 합니다.")
        if await self._read() != self._snapshot:
            raise StaleObservation("네이버플러스 스토어의 화면 내용이 변경됐습니다.")
        self._active = None
        try:
            expected = await node.evaluate(r"""(el, search) => {
                if (!el.isConnected || el.form || el.closest('[hidden],[inert],[aria-disabled=true]')) return null;
                if (search && el.matches('input[name="query"][type="text"]') && !el.readOnly) {
                    return el.value.trim().replace(/\s+/g, ' ');
                }
                return !search && el.matches('button[class~="N=a:gnb.directcart"][type="button"]') ? 'cart' : null;
            }""", search)
            if not expected or not await node.is_visible() or not await node.is_enabled():
                raise StaleObservation("선택한 네이버플러스 스토어 요소를 조작할 수 없습니다.")
        except PlaywrightError as exc:
            self._require_current_url()
            raise StaleObservation("선택한 네이버플러스 스토어 요소가 교체됐습니다.") from exc

        self._step()
        previous = self.page.url
        try:
            if search:
                await node.press("Enter", timeout=self.action_timeout_ms)
            else:
                await node.click(timeout=self.action_timeout_ms)
            await self.page.wait_for_url(
                lambda url: url != previous, wait_until="domcontentloaded", timeout=self.action_timeout_ms,
            )
        except PlaywrightError as exc:
            self._require_current_url()
            await self._read()
            raise BrowserError("요청한 네이버플러스 스토어 화면의 이동을 확인할 수 없습니다.") from exc
        self._require_current_url()
        if search:
            result = urlsplit(self.page.url)
            query = parse_qs(result.query).get("query", [""])[0]
            if (result.hostname, result.path) != _RESULTS or " ".join(query.split()) != expected:
                raise BrowserBlocked("입력한 검색어와 네이버플러스 스토어 결과 주소를 대조할 수 없습니다.")
            try:
                await self.page.locator(f"{_RESULT_CARD}:visible").first.wait_for(
                    state="visible", timeout=self.action_timeout_ms,
                )
            except PlaywrightError:
                self._require_current_url()
        else:
            raise BrowserBlocked("비로그인으로 조회할 수 있는 네이버플러스 스토어 장바구니 화면을 확인하지 못했습니다.")

    async def open_cart(self) -> None:
        self._phase = "cart"
        observation = await self.observe()
        cart = [item for item in observation.targets if item.role == "cart"]
        if observation.view != "results" or len(cart) != 1:
            raise BrowserError("검색 화면에서 네이버플러스 스토어 장바구니 버튼을 확인할 수 없습니다.")
        await self.execute(observation, cart[0].action, cart[0].id)

    async def read_cart(self):
        raise BrowserBlocked("네이버플러스 스토어 비로그인 장바구니 상품을 확인할 수 없습니다.")
