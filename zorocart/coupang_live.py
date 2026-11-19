"""Visible desktop Coupang evidence in an isolated, signed-out Chromium profile."""

from __future__ import annotations

from dataclasses import replace
import re
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from .browser import BrowserAdapter, BrowserBlocked, BrowserError, StaleObservation
from .coupang_evidence import parse_pack

DESKTOP_SNAPSHOT = r'''
() => {
  const clean = e => (typeof e === 'string' ? e : e?.innerText ?? '').replace(/\s+/g, ' ').trim();
  const visible = e => Boolean(e?.isConnected && e.getClientRects().length &&
    !e.closest('[hidden], [inert], [aria-hidden=true]') && getComputedStyle(e).visibility === 'visible');
  const all = (root, selector) => [...(root?.querySelectorAll(selector) ?? [])].filter(visible);
  const first = (root, selector) => all(root, selector)[0];
  const target = (root, selector, text) => all(root, selector).find(e => clean(e) === text);
  const fields = (entries) => Object.fromEntries(Object.entries(entries).map(
    ([name, values]) => [name, (Array.isArray(values) ? values : [values]).filter(Boolean).map(text => ({text, unit: ''}))]));
  const unique = list => [...new Set(list.filter(Boolean))];
  const nodes = [];
  const controls = [];
  const rows = [];
  const offers = [];
  const path = location.pathname;
  const pageHeading = all(document, 'h1, h2, [role=alert]').map(clean);
  const blocked = /^(Access Denied|Forbidden)$/i.test(document.title.trim()) ||
    pageHeading.some(x => /^(Access Denied|Forbidden|접근이 제한|접근 권한이 없|비정상적인 접근|보안 확인|로그인이 필요)/i.test(x)) ||
    Boolean(first(document, 'input[type=password], iframe[src*=captcha], [data-sitekey]'));
  const view = blocked ? 'blocked' : path === '/' ? 'home' : path === '/np/search' ? 'results' :
    /^\/vp\/products\/\d+$/.test(path) ? 'detail' : path === '/cartView.pang' ? 'cart' : 'unknown';
  const logout = Boolean(first(document, 'a[href*="/logout"]'));
  function add(role, element, label, key = role, href = null, card_facts = {}) {
    if (!element || element.matches(':disabled,[aria-disabled=true]')) return;
    const form = role === 'search-submit' ? element.form : null;
    nodes.push(element);
    controls.push({role, key, label, href, tag: element.tagName.toLowerCase(),
      aria_role: element.getAttribute('role') || '', value: ['search-input', 'quantity'].includes(role) ? element.value : '',
      form: form ? (element.hasAttribute('formaction') ? element.formAction : form.action) : '',
      method: form ? (element.getAttribute('formmethod') || form.method) : '',
      destination: form ? (element.getAttribute('formtarget') || form.target) : '',
      selected: false, card_facts, options: []});
  }
  function product(value) {
    try {
      const u = new URL(value, location.origin);
      const id = /^\/vp\/products\/(\d+)$/.exec(u.pathname)?.[1];
      const option = u.searchParams.get('vendorItemId') || '';
      if (u.origin !== 'https://www.coupang.com' || !id || !/^\d+$/.test(option)) return null;
      const stable = new URL(u.pathname, u.origin);
      for (const key of ['itemId', 'vendorItemId']) {
        const v = u.searchParams.get(key);
        if (v && /^\d+$/.test(v)) stable.searchParams.set(key, v);
      }
      return {id, option, url: stable.href};
    } catch { return null; }
  }
  function shipment(root) {
    const observed = all(root, 'img').map(img => [img.alt, img.title, img.getAttribute('aria-label'), img.getAttribute('src')].filter(Boolean).join(' '));
    if (observed.some(s => /로켓프레시|rocket[-_]?fresh/i.test(s))) return '로켓프레시';
    if (observed.some(s => /로켓배송|rocket[-_]?delivery/i.test(s))) return '로켓배송';
    return '';
  }
  const cartLink = all(document, 'a[href*="cart.coupang.com/cartView.pang"]').find(e => e.id === 'wa-cart-link');
  if (cartLink) add('cart', cartLink, '장바구니', 'cart', 'https://cart.coupang.com/cartView.pang');
  if (view === 'home' || view === 'results') {
    const input = first(document, '#wa-search-form input[name=q], #wa-search-form-tablet input[name=q], #wa-sticky-search input[name=q]');
    const form = input?.form;
    add('search-input', input, '쿠팡 상품 검색');
    const buttons = all(form, 'button');
    const submit = buttons.filter(e => e.getAttribute('type') === 'submit');
    add('search-submit', buttons.find(e => [clean(e), e.title, e.getAttribute('aria-label')].includes('검색')) ||
      (submit.length === 1 ? submit[0] : null), '검색');
  }
  if (view === 'results') {
    const tokens = (new URL(location.href).searchParams.get('q') || '').toLowerCase().split(/\s+/).filter(Boolean);
    const ranked = new Map();
    for (const link of all(document, 'a[href*="/vp/products/"]')) {
      const p = product(link.href);
      const title = clean(first(link, '[class*=productName], [class*=product-name], [class*=ProductName]')) ||
        first(link, 'img[alt]')?.alt?.trim() || '';
      const text = clean(link);
      if (!p || !title || !text || ranked.has(p.option)) continue;
      const t = title.toLowerCase();
      const clues = tokens.map(s => s.replace(/형$/, '').replace(/쓰레기봉투$/, '봉투'));
      let rank = clues.filter(s => t.includes(s) || (/^\d+l$/.test(s) && t.includes(s.replace(/l$/, '리터')))).length * 12;
      if (/(?:^|[\s,×])1\s*(?:개|팩|세트)(?=$|[\s,×])/.test(t)) rank += 9;
      if (/(?:^|[\s,×])(?:무향|손잡이)/.test(t)) rank += 4;
      ranked.set(p.option, {p, link, title, text, rank});
    }
    for (const item of [...ranked.values()].sort((a, b) => b.rank - a.rank).slice(0, 6)) {
      add('card', item.link, item.title, item.p.option, item.p.url,
        fields({name: item.title, price: item.text.match(/[\d,]+원/g)?.slice(0, 3), shipping: shipment(item.link)}));
    }
  }
  if (view === 'detail') {
    const root = document.querySelector('.prod-atf-contents');
    const p = product(location.href);
    const title = clean(first(root, 'h1.product-title'));
    const button = target(root, 'button.prod-cart-btn', '장바구니 담기');
    const input = first(root, '.prod-buy-quantity-and-footer input[type=text]');
    const prices = unique(all(root, '.final-price-amount').map(clean));
    const specs = all(root, 'li').map(clean).filter(x => /^(개당 수량|팩당 수량|총 수량|손잡이 여부|개당 용량)\s*:/.test(x));
    const looksLikeOption = el => /^\d[^\n]{0,70}×[^\n]{1,60}$/.test(clean(el)) && !clean(el).includes('원');
    const option = clean(all(root, 'span, div').find(el => el.children.length === 0 && looksLikeOption(el))) || title;
    add('quantity', input, '주문 수량');
    add('add', button, '장바구니 담기');
    if (p && title) offers.push({product_id: p.id, option_id: p.option, selected: true, product_fields: {},
      fields: fields({name: title, 'current-price': prices.length === 1 ? prices[0] : '', option,
        specs, attributes: [title, option, ...specs], shipping: shipment(root),
        stock: button && !button.disabled && input?.value === '1' ? '최대 1 구매 가능' : ''})});
  }
  let empty = false;
  if (view === 'cart') {
    const root = document.querySelector('#mainContent') || document.body;
    const cartText = (root?.innerText || '').split('\n').map(x => x.trim());
    empty = cartText.some(x => /^(장바구니가\s*비어\s*있습니다\.?|장바구니에\s*담은\s*상품이\s*없습니다\.?)$/.test(x)) &&
      !first(root, '[id^=item_]');
    for (const row of all(root, '[id^=item_]')) {
      const link = first(row, '[id=name] a[href*="/vp/products/"], a[href*="/vp/products/"]');
      const p = product(link?.href);
      const name = clean(link?.children[0]);
      const option = clean(link?.children[1]).replace(/^옵션\s*:\s*/, '');
      const input = first(row, 'input.cart-quantity-input');
      const prices = unique(all(row, 'span.twc-font-bold').filter(el => clean(el.nextElementSibling) === '원')
        .map(el => clean(el)).filter(x => /^\d[\d,]*$/.test(x)));
      rows.push({product_id: p?.id || '', option_id: p?.option || '', clear_id: row.id.replace(/^item_/, ''),
        fields: fields({name, option, 'current-price': prices.length === 1 ? prices[0] : '', quantity: input?.value,
          shipping: shipment(row)})});
    }
    const clear = all(root, 'div').find(e => e.children.length === 0 && clean(e) === '선택삭제');
    add('clear-cart', clear, '장바구니 전체 선택 후 삭제');
  }
  const quantities = controls.filter(c => c.role === 'quantity').map(c => c.value);
  return {root: document.documentElement, nodes, payload: {view, controls, offers, rows, empty, logout, quantities}};
}
'''


class CoupangDesktopAdapter(BrowserAdapter):
    """Translate observed Coupang controls into the existing shopping roles."""

    async def _initialize(self) -> None:
        if self.can_add or self.can_clear:
            await super()._initialize()
        elif not getattr(self, '_read_observers_ready', False):
            self.page.on('response', self._on_response)
            self.page.on('popup', self._close_unexpected_popup)
            self._read_observers_ready = True

    @staticmethod
    async def _close_unexpected_popup(page) -> None:
        if not page.is_closed():
            await page.close()

    async def open_home(self) -> None:
        await super().open_home()
        try:
            await self.page.locator('a[href*="login.coupang.com/login/login"]').first.wait_for(
                state='visible', timeout=self.action_timeout_ms)
        except PlaywrightError as exc:
            raise BrowserBlocked('쿠팡에서 비로그인 상태를 확인하지 못했습니다.') from exc
        try:
            await self.page.locator(
                '#wa-search-form input[name=q]:visible, #wa-search-form-tablet input[name=q]:visible, '
                '#wa-sticky-search input[name=q]:visible',
            ).first.wait_for(state='visible', timeout=self.action_timeout_ms)
        except PlaywrightError as exc:
            self._require_current_url()
            raise BrowserError('표시된 쿠팡 상품 검색창을 확인할 수 없습니다.') from exc

    async def _capture(self):
        self._require_current_url()
        try:
            if urlsplit(self.page.url).path == '/cartView.pang':
                await self.page.wait_for_function(
                    "document.querySelector('#mainContent') || /장바구니\\(\\d+\\)/.test(document.body?.innerText || '')",
                    timeout=self.action_timeout_ms,
                )
            handle = await self.page.evaluate_handle(DESKTOP_SNAPSHOT)
            payload = await handle.get_property('payload')
            data = await payload.json_value()
            await payload.dispose()
        except PlaywrightError as exc:
            self._require_current_url()
            raise StaleObservation('쿠팡 화면이 준비되었는지 확인할 수 없습니다.') from exc
        if data['view'] in {'blocked', 'unknown'} or data['logout']:
            await handle.dispose()
            raise BrowserBlocked('쿠팡에서 로그인, 접근 제한 또는 인식할 수 없는 화면을 확인했습니다.')
        return handle, data

    async def execute(self, observation, action, target_id=None, value=None, **guards):
        target = next((item for item in observation.targets if item.id == target_id), None)
        if target and target.role == 'search-submit' and action == 'CLICK' and not (self.can_add or self.can_clear):
            node = self._nodes.get(target.id)
            if observation is not self._active or node is None:
                raise StaleObservation('검색 버튼이 현재 화면에서 다시 관찰되어야 합니다.')
            self._require_current_url()
            try:
                form = await node.evaluate("""(el) => ({
                    attached: el.isConnected,
                    type: el.type,
                    action: el.form ? (el.hasAttribute('formaction') ? el.formAction : el.form.action) : '',
                    method: el.getAttribute('formmethod') || el.form?.method || '',
                    target: el.getAttribute('formtarget') ?? el.form?.target ?? '',
                })""")
            except PlaywrightError as exc:
                self._require_current_url()
                raise StaleObservation('쿠팡 검색 버튼이 변경됐습니다.') from exc
            if not form['attached']:
                raise StaleObservation('쿠팡 검색 버튼이 변경됐습니다.')
            if (form['type'] != 'submit' or form['method'].lower() != 'get' or form['target'] not in {'', '_self'}
                    or not self._in_scope(form['action']) or urlsplit(form['action']).path != '/np/search'):
                raise BrowserBlocked('쿠팡의 조회 전용 검색 양식을 확인할 수 없습니다.')
        if target and target.role == 'card' and action == 'CLICK':
            if observation is not self._active or self.page.url != self._observation_url or await self._read() != self._snapshot:
                raise StaleObservation('검색 화면의 상품 카드가 바뀌었습니다.')
            node = self._nodes.get(target.id)
            if node is None or not await node.is_visible() or not target.url:
                raise StaleObservation('검색 카드에서 상품 주소를 다시 확인할 수 없습니다.')
            await self.open_product(target.url)
            return
        await super().execute(observation, action, target_id, value, **guards)

    def _offers(self, data):
        offers = super()._offers(data)
        if not hasattr(self, '_current_offer_by_pair'):
            self._current_offer_by_pair = {}
        for offer in offers:
            self._current_offer_by_pair[(offer.product_id, offer.option_id)] = offer
        return offers

    def _cart_lines(self, data):
        lines = super()._cart_lines(data)
        result = []
        for line in lines:
            seen = getattr(self, '_current_offer_by_pair', {}).get((line.product_id, line.option_id))
            if seen and seen.title.startswith(line.title) and seen.capacity and seen.pack_count:
                parsed = parse_pack(line.label)
                volume = re.search(r'\b(\d+)\s*(?:l|리터)', seen.title, re.I)
                same_volume = not volume or bool(re.search(rf'\b{volume[1]}\s*(?:l|리터)', line.label, re.I))
                if same_volume and parsed.total_contents == seen.capacity and parsed.packages_per_order in {None, seen.pack_count}:
                    line = replace(line, label=seen.label, pack_count=seen.pack_count)
            result.append(line)
        return tuple(result)

    async def _clear(self, node, data, expected) -> None:
        if not self.can_write:
            raise BrowserBlocked('쿠팡 비로그인 장바구니의 삭제와 담기 요청을 모두 확인해야 초기화할 수 있습니다.')
        if not expected or frozenset(self._cart_lines(data)) != frozenset(expected):
            raise StaleObservation('쿠팡 장바구니의 삭제 대상이 변경됐습니다.')
        select_all = self.page.locator('#mainContent label').filter(has_text='전체 선택').locator('input[type=checkbox]')
        if await select_all.count() != 1 or not await select_all.is_visible():
            raise BrowserError('쿠팡 장바구니 전체 선택을 확인하지 못했습니다.')
        await select_all.check(timeout=self.action_timeout_ms)
        current = await self._read()
        await super()._clear(node, current, expected)

    async def _add(self, node, offer, quantity) -> None:
        if not self.can_add:
            raise BrowserBlocked('관찰된 쿠팡 비회원 담기 요청이 없어 상품을 담을 수 없습니다.')
        await super()._add(node, offer, quantity)
