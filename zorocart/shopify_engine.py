"""Plan, score and verify orders against a signed-out Shopify storefront."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import json
import re
from xml.etree import ElementTree

import httpx

from .browser import CartLine, Offer
from .engine import Hold, ShoppingEngine, feasible
from .shopify_live import ShopifyError, ShopifyStore, ShopifyVariant

ECB_DAILY = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
_COUNTS = re.compile(r'\b(\d+)\s*(wipes?|sheets?|bags?|liners?|pieces?|tablets?|count|g|kg|ml|l)\b', re.I)
_PACKS = re.compile(r'\b(\d+)\s*(?:separate\s+)?(?:packs|bottles|jars|rolls)\b|\b(\d+)\s*[x×]\s*\d+', re.I)
_HANDLES = re.compile(r'\b(?:drawstring|tie|carry|loop)[ -]handles?\b|\bwith handles\b', re.I)
_UNSCENTED = re.compile(r'\b(?:fragrance[ -]free|unscented|unperfumed)\b', re.I)
_WIPE_PACK = re.compile(r'\b(?:pack|packet|pouch|reseal (?:the )?label)\b', re.I)
_WIPE_TUB = re.compile(r'\b(?:canister|tub|jar)s?\b', re.I)


@dataclass(frozen=True)
class SterlingRate:
    pounds_to_won: Decimal
    reference_date: date

    def won(self, minor: int) -> int:
        return int((Decimal(minor) * self.pounds_to_won / 100).to_integral_value(rounding=ROUND_CEILING))

    def minor_limit(self, won: int | None) -> int | None:
        if won is None:
            return None
        return int((Decimal(won) * 100 / self.pounds_to_won).to_integral_value(rounding=ROUND_FLOOR))


async def sterling_rate() -> SterlingRate:
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            response = await client.get(ECB_DAILY)
            response.raise_for_status()
        if len(response.content) > 120_000:
            raise ValueError('Unexpected ECB publication length')
        root = ElementTree.fromstring(response.content)
        source = next(node for node in root.iter() if 'time' in node.attrib)
        day = date.fromisoformat(source.attrib['time'])
        rates = {row.attrib['currency']: Decimal(row.attrib['rate']) for row in source}
        today = datetime.now(timezone.utc).date()
        ratio = rates['KRW'] / rates['GBP']
        if not today - timedelta(days=7) <= day <= today or not ratio.is_finite() or ratio <= 0:
            raise ValueError('Invalid ECB publication date or rate')
        return SterlingRate(ratio, day)
    except (httpx.HTTPError, ElementTree.ParseError, StopIteration, KeyError, ValueError, InvalidOperation, ArithmeticError) as exc:
        raise Hold('ECB의 최신 파운드 환율을 확인할 수 없어 원화 예산을 검증하지 못했습니다.') from exc


async def english_query(client, item, previous: str | None) -> str:
    facts = json.dumps({'item': item.name, 'required': item.required, 'previous_query': previous}, ensure_ascii=False)
    expected = 'wipes' if '물티슈' in item.name else 'bin liners' if '쓰레기봉투' in item.name.replace(' ', '') else None
    guidance = f' The phrase must include the exact words {expected}.' if expected else ''
    for attempt in range(2):
        text = (await client.chat([
            {'role': 'system', 'content': 'Return only one short English product search phrase for a British '
             'supermarket. Examples: 무향 물티슈 → fragrance free wet wipes; 30L 손잡이형 쓰레기봉투 → '
             '30L drawstring bin liners. 물티슈 is always wet wipes. Preserve defining specifications and '
             'omit order quantities, brands, prices and shipping. If a previous query is given, choose '
             'a different concise synonym or remove a secondary word. No commentary.' + guidance},
            {'role': 'user', 'content': facts},
        ], max_tokens=50, temperature=0.2 if attempt else 0.0)).strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .&'/-]{1,78}[A-Za-z0-9]", text) and (
            previous is None or text.casefold() != previous.casefold()
        ) and (expected is None or expected in text.casefold()):
            return text
    raise Hold(f'{item.name}의 영어 검색어를 확인할 수 없습니다.')


def pack_evidence(variant: ShopifyVariant) -> tuple[int | None, int | None, str | None]:
    title = f'{variant.title} {variant.label}'
    bundle = _PACKS.search(title)
    labelled_pack = re.search(r'\bpack of (\d+)\b|\b(\d+)[ -]pack\b', title, re.I)
    if labelled_pack and not bundle and re.search(r'\b(?:bin liners?|trash bags?|wipes?)\b', title, re.I):
        return 1, int(next(filter(None, labelled_pack.groups()))), 'piece'
    counts = list(_COUNTS.finditer(title))
    count = next((match for match in counts if match[2].lower() not in {'g', 'kg', 'ml', 'l'}), None)
    count = count or (counts[0] if counts else None)
    if count:
        amount, name = count.groups()
        unit = name.lower() if name.lower() in {'g', 'kg', 'ml', 'l'} else 'piece'
        return int(next(filter(None, bundle.groups()))) if bundle else 1, int(amount), unit
    return None, None, None


class _OfferView:
    def __init__(self, store: ShopifyStore):
        self.store = store
        self.rate: SterlingRate
        self.variants: dict[tuple[str, str], ShopifyVariant] = {}
        self.open_url = ''

    def offer(self, variant: ShopifyVariant) -> Offer:
        if variant.currency != 'GBP':
            raise Hold('영국 쇼핑몰의 상품가와 장바구니 통화가 파운드로 일치해야 합니다.')
        packs, amount, unit = pack_evidence(variant)
        facts = f'{variant.label} {variant.title} {variant.description}'
        attributes = [variant.title]
        if match := _HANDLES.search(facts):
            attributes.append(f'판매 설명에서 손잡이형 확인 ({match[0]})')
        if match := _UNSCENTED.search(facts):
            attributes.append(f'판매 설명에서 무향 확인 ({match[0]})')
        if 'wipes' in variant.title.casefold() and _WIPE_PACK.search(facts) and not _WIPE_TUB.search(facts):
            attributes.append('판매 설명에서 팩형 포장 확인')
        attributes.append(variant.description[:1000])
        self.variants[(str(variant.product_id), str(variant.variant_id))] = variant
        return Offer(str(variant.product_id), str(variant.variant_id), variant.title, variant.label or '기본 옵션',
                     self.rate.won(variant.price_minor), packs, amount, unit, int(variant.available),
                     '', None, tuple(attributes), '', variant.url, True)

    async def open_product(self, url: str) -> None:
        self.open_url = url

    async def details(self) -> tuple[Offer, ...]:
        return tuple(self.offer(variant) for variant in await self.store.details(self.open_url))


class ShopifyShoppingEngine(ShoppingEngine):
    def __init__(self, client, store: ShopifyStore, event, **options):
        self.store = store
        self.offers = _OfferView(store)
        super().__init__(client, self.offers, event, **options)
        self.native_totals: dict[str, int] = {}
        self.native_result: dict | None = None

    async def search_item(self, item) -> list:
        query = None
        for retry in (False, True):
            query = await english_query(self.client, item, query)
            self.event('search_query', item=item.id, query=query, retry=retry)
            variants = (await self.store.search(query))[:6]
            self.event('store_catalog_read', query=query, inspected_cards=len(variants))
            candidates = await asyncio.gather(*(self.evaluate_variant(item, f'{item.id}-{index}', variant)
                                                 for index, variant in enumerate(variants, start=1)))
            accepted = [candidate for candidate in candidates if candidate]
            if accepted:
                return sorted(accepted, key=lambda candidate: (-candidate.score, candidate.price, candidate.id))
        self.holds[item.id] = '판매 카탈로그에서 조건을 확인한 후보가 없습니다.'
        return []

    async def evaluate_variant(self, item, candidate_id, variant):
        offer = self.offers.offer(variant)
        if '물티슈' in item.name and item.purchase_unit == '팩' and '판매 설명에서 팩형 포장 확인' not in offer.attributes:
            self.event('candidate_hold', item=item.id, candidate_id=candidate_id,
                       evidence={'title': offer.title, 'label': offer.label}, reason='판매 정보에서 팩형 포장 확인 불가')
            return None
        if '봉투' in item.name and any('손잡이' in term for term in item.required) and not any(
            attribute.startswith('판매 설명에서 손잡이형 확인') for attribute in offer.attributes
        ):
            self.event('candidate_hold', item=item.id, candidate_id=candidate_id,
                       evidence={'title': offer.title, 'label': offer.label}, reason='판매 정보에서 손잡이 사양 확인 불가')
            return None
        return await self.evaluate(item, candidate_id, offer)

    async def select(self, groups, items, spent=0):
        combinations = feasible(self.plan, groups, spent)
        if not combinations:
            raise Hold('전체 예산과 필수 조건을 동시에 충족하는 조합을 찾지 못했습니다.')
        selected = []
        for index, item in enumerate(items):
            eligible = {combo[index].id for combo in combinations}
            candidate = next(option for option in groups[index] if option.id in eligible)
            offer = candidate.offer
            state = {
                '품목': item.name, '필수 조건': item.required, '제외 조건': item.excluded,
                '판매 상품': offer.title, '판매 옵션': offer.label, '판매처 표기': offer.attributes[1:-1],
                '구성': {'판매 단위당 팩 수': offer.pack_count, '팩당 내용물': offer.capacity,
                       '내용물 단위': '개' if offer.capacity_unit == 'piece' else offer.capacity_unit},
                '주문 수량': candidate.quantity, '예산 확인': True,
            }
            instructions = ('이 후보가 요청한 상품 종류와 필수 조건을 충족하는지 판매처 표기와 구성을 대조하세요. '
                            '무향은 fragrance free, 끈 손잡이는 drawstring handles 표기가 근거입니다. '
                            '예산과 재고는 별도로 검증되었습니다. 필수 조건만 판단하세요.')
            criteria = {'buy': '품목과 필수 사양이 맞고 확인된 주문 수량으로 담습니다.',
                        'hold': '상품이나 필수 사양이 다르거나 확인할 근거가 부족합니다.'}
            decision = await self.client.choice(state, instructions, criteria)
            if decision.choice != 'buy' or decision.probability < self.choice_threshold:
                self.event('choice_recheck', item=item.id, choice=decision.choice, probability=decision.probability)
                await self.offers.open_product(offer.url)
                actual = next((entry for entry in await self.offers.details() if self.key(entry) == self.key(offer)), None)
                if actual is None or not self.same_offer(actual, offer):
                    raise Hold(f'{item.name}의 판매 정보를 다시 확인할 수 없습니다.')
                state['판매처 상세'] = actual.attributes[-1][:500]
                decision = await self.client.choice(state, instructions, criteria, reasoning=True)
            self.event('product_choice', item=item.id, choice=decision.choice,
                       probabilities=decision.probabilities, candidates={'buy': candidate.id})
            if decision.choice != 'buy' or decision.probability < self.choice_threshold:
                self.holds[item.id] = f'{item.name}의 자동 담기 확신이 기준에 미달했습니다.'
                raise Hold(self.holds[item.id])
            selected.append(candidate)
            combinations = [combo for combo in combinations if combo[index].id == candidate.id]
        return selected

    async def run(self, request: str) -> dict:
        try:
            await self.prepare_plan(request)
            if self.plan.include_shipping:
                raise Hold('배송지 기준 요금을 장바구니에서 확인할 수 없어 배송비 포함 예산을 검증하지 못했습니다.')
            self.offers.rate = await sterling_rate()
            self.event('exchange_rate', currency='GBP', date=self.offers.rate.reference_date.isoformat(),
                       won_per_pound=str(self.offers.rate.pounds_to_won))
            await self.store.open()
            baseline = await self.store.read_cart()
            if baseline.currency != 'GBP':
                raise Hold('McGrocer 장바구니 통화를 파운드로 확인할 수 없습니다.')
            groups = [await self.search_item(item) for item in self.items]
            candidates = await self.select(groups, self.items)
            candidates = await self.revalidate_remaining(self.items, candidates, 0)
            orders = [(self.offers.variants[self.key(candidate.offer)], candidate.quantity) for candidate in candidates]
            self.selections = {self.key(candidate.offer): candidate for candidate in candidates}
            self.item_by_key = {self.key(candidate.offer): item.id for item, candidate in zip(self.items, candidates)}

            counts = {'before_lines': len(baseline.lines), 'before_units': sum(line.quantity for line in baseline.lines)}
            self.cart_reset = {'status': 'pending', **counts}
            self.event('cart_reset_started', **counts)
            empty = await self.store.clear_cart(baseline)
            if empty.lines:
                self.cart_reset['status'] = 'incomplete'
                raise Hold('McGrocer 장바구니가 비어 있는 것을 확인할 수 없습니다.')
            removed = {'removed_lines': counts['before_lines'], 'removed_units': counts['before_units']}
            self.cart_reset.update(status='verified', **removed)
            self.event('cart_reset_verified', **removed)
            self.event('store_cart_add', lines=[{'variant_id': str(variant.variant_id), 'quantity': quantity}
                                                for variant, quantity in orders])
            cart = await self.store.add_to_cart(orders, expected=empty,
                                                budget_minor=self.offers.rate.minor_limit(self.plan.total_budget))
            if cart.currency != 'GBP':
                raise Hold('추가 후 장바구니 통화가 변경됐습니다.')
            rows = []
            for line in cart.lines:
                key = str(line.product_id), str(line.variant_id)
                candidate = self.selections.get(key)
                rows.append(CartLine(*key, line.title, line.label or '기본 옵션', self.offers.rate.won(line.price_minor),
                                     line.quantity, candidate.offer.pack_count if candidate else None, '', None))
                self.native_totals[key[1]] = line.line_total_minor
            self.native_result = {'currency': cart.currency, 'total_minor': cart.total_minor,
                                  'reference_date': self.offers.rate.reference_date.isoformat()}
            self.verify(tuple(rows))
            if len(self.verified) != len(self.items):
                raise Hold('장바구니에서 요청한 품목 전부의 수량을 확인할 수 없습니다.')
            return self.result('complete')
        except (Hold, ShopifyError) as exc:
            self.event('held', reason=str(exc))
            return self.result('partial' if self.verified else 'held', str(exc))

    def result(self, status: str, reason: str | None = None) -> dict:
        result = super().result(status, reason)
        if self.native_result is not None:
            result['store_money'] = self.native_result
            for row in result['added']:
                row['store_total_minor'] = self.native_totals[row['option_id']]
        return result
