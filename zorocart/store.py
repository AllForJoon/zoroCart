"""Synthetic Korean storefront for the visible-browser demo."""

from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from html import escape
from secrets import token_urlsafe
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/store")


@dataclass(frozen=True)
class Variant:
    id: str
    label: str
    price: int
    pack: int
    capacity: int
    unit: str
    attributes: tuple[tuple[str, str], ...]
    stock: int = 8
    shipping: str = "로켓배송"
    shipping_price: int = 0


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    category: str
    emoji: str
    variants: tuple[Variant, ...]


PRODUCTS = (
    Product("wipes-daily", "매일봄 도톰한 무향 물티슈", "물티슈", "🧻", (
        Variant("single-unscented", "무향 80매 · 1팩", 7_200, 1, 80, "piece",
                (("unscented", "무향"), ("thick", "도톰한 원단"))),
        Variant("single-floral", "플로럴향 80매 · 1팩", 7_000, 1, 80, "piece",
                (("scented", "플로럴향"), ("thick", "도톰한 원단"))),
        Variant("double-unscented", "무향 80매 × 2팩", 12_900, 2, 160, "piece",
                (("unscented", "무향"), ("thick", "도톰한 원단"))),
    )),
    Product("wipes-basic", "차분한집 베이직 무향 물티슈", "물티슈", "🧻", (
        Variant("single", "무향 60매 · 1팩", 6_900, 1, 60, "piece",
                (("unscented", "무향"),), shipping="일반배송", shipping_price=2_500),
    )),
    Product("bags-easy", "정돈생활 30L 손잡이형 쓰레기봉투", "쓰레기봉투", "🛍️", (
        Variant("30l-handle", "30L 손잡이형 40매 · 1팩", 10_900, 1, 40, "piece",
                (("30l", "용량 30L"), ("handle", "손잡이형"))),
        Variant("20l-handle", "20L 손잡이형 40매 · 1팩", 8_900, 1, 40, "piece",
                (("20l", "용량 20L"), ("handle", "손잡이형"))),
    )),
    Product("bags-flat", "모아쓰는 생활 쓰레기봉투", "쓰레기봉투", "🛍️", (
        Variant("30l-flat", "30L 평판형 30매 · 1팩", 7_900, 1, 30, "piece",
                (("30l", "용량 30L"), ("flat", "손잡이 없는 평판형"))),
    )),
    Product("bags-simple", "하루정리 30L 손잡이형 쓰레기봉투", "쓰레기봉투", "🛍️", (
        Variant("30l-handle", "30L 손잡이형 20매 · 1팩", 8_400, 1, 20, "piece",
                (("30l", "용량 30L"), ("handle", "손잡이형")),
                shipping="일반배송", shipping_price=3_000),
    )),
    Product("pasta-durum", "오늘식탁 듀럼밀 스파게티면", "파스타면", "🍝", (
        Variant("500g", "스파게티면 500g · 1봉", 4_900, 1, 500, "g",
                (("spaghetti", "스파게티 파스타면"), ("durum", "듀럼밀 세몰리나 100%"),
                 ("2-servings", "2인분 이상")),),
    )),
    Product("pasta-mini", "한끼식탁 스파게티면 소포장", "파스타면", "🍝", (
        Variant("100g", "스파게티면 100g · 1봉", 3_400, 1, 100, "g",
                (("spaghetti", "스파게티 파스타면"), ("1-serving", "1인분")), stock=1),
    )),
    Product("sauce-tomato", "오늘식탁 바질 토마토 파스타 소스", "토마토 파스타 소스", "🍅", (
        Variant("440g-tomato", "토마토 바질 소스 440g · 1병", 7_900, 1, 440, "g",
                (("tomato-sauce", "토마토 파스타 소스"), ("2-servings", "2~3인분"),
                 ("ready-to-cook", "바로 조리에 사용하는 파스타용 소스"))),
        Variant("400g-cream", "크림 소스 400g · 1병", 8_200, 1, 400, "g",
                (("cream-sauce", "크림 파스타 소스"), ("2-servings", "2~3인분"))),
    )),
    Product("sauce-snack", "상큼한집 찍어 먹는 토마토 살사", "토마토 파스타 소스", "🍅", (
        Variant("220g-salsa", "토마토 살사 220g · 1병", 6_900, 1, 220, "g",
                (("salsa", "나초용 살사"), ("not-pasta-sauce", "파스타용 소스 아님"))),
    )),
    Product("camp-chair", "솔바람 등받이 접이식 캠핑 의자", "캠핑 의자", "🪑", (
        Variant("olive-single", "올리브 · 접이식 의자 1개", 17_900, 1, 1, "piece",
                (("folding", "접이식"), ("camping-chair", "캠핑 의자"), ("backrest", "등받이형"))),
        Variant("olive-pair", "올리브 · 접이식 의자 2개 세트", 31_900, 2, 2, "piece",
                (("folding", "접이식"), ("camping-chair", "캠핑 의자"), ("backrest", "등받이형"))),
    )),
    Product("camp-fixed", "뜰앞 원목 캠핑용 야외 의자", "캠핑 의자", "🪑", (
        Variant("fixed-single", "원목 고정식 의자 1개", 19_900, 1, 1, "piece",
                (("fixed", "고정식"), ("camping-chair", "캠핑 의자"))),
    )),
    Product("camp-lantern", "솔바람 충전식 LED 캠핑 랜턴", "캠핑 랜턴", "🏮", (
        Variant("amber", "전구색 · 랜턴 1개", 15_900, 1, 1, "piece",
                (("camping-lantern", "야외용 캠핑 랜턴"), ("led", "LED"),
                 ("rechargeable", "USB 충전식"), ("warm", "전구색"))),
    )),
    Product("camp-keylight", "솔바람 장식용 미니 랜턴 키링", "캠핑 랜턴", "🏮", (
        Variant("keyring", "장식용 키링 1개", 3_900, 1, 1, "piece",
                (("keyring", "장식용 키링"), ("decoration", "야외 공간 조명용 아님"))),
    )),
    Product("pantry-coffee", "아침한잔 드립백 커피", "커피", "☕", (
        Variant("mild", "마일드 드립백 5개 · 1팩", 6_500, 1, 5, "piece",
                (("coffee", "커피"),), shipping="일반배송", shipping_price=3_000),
    )),
)

PRODUCT_BY_ID = {product.id: product for product in PRODUCTS}
ALIASES = {
    "물티슈": ("물티슈", "습윤티슈", "wipes", "wet tissue"),
    "쓰레기봉투": ("쓰레기봉투", "쓰레기백", "휴지통봉투", "비닐봉투", "손잡이봉투", "trash bags"),
    "파스타면": ("파스타", "스파게티", "듀럼밀", "건면", "링귀니", "펜네", "pasta"),
    "토마토 파스타 소스": ("토마토", "소스", "tomato sauce", "파스타소스", "스파게티소스"),
    "캠핑 의자": ("의자", "캠핑체어", "접이식체어", "camping chair"),
    "캠핑 랜턴": ("랜턴", "야외조명", "캠핑조명", "led조명", "캠핑램프", "lantern"),
    "커피": ("커피", "드립백", "coffee"),
}

BUDGET_INCREASE = {
    "물티슈": 9_000,
    "쓰레기봉투": 13_000,
    "파스타면": 7_000,
    "토마토 파스타 소스": 6_000,
    "캠핑 의자": 16_000,
    "캠핑 랜턴": 13_000,
}
FAULTS = frozenset({
    "budget", "sold-out", "spec-mismatch", "missing-info", "stale-target",
    "add-timeout-applied", "add-timeout-unapplied", "price-change", "clear-mismatch", "blocked",
})
COOKIE = "zorocart_demo_session"
ADD_RESPONSE_DELAY_SECONDS = 15
STALE_OPTION_DELAY_MS = 800


@dataclass
class Session:
    cart: dict[tuple[str, str], int] = field(default_factory=lambda: {
        ("wipes-daily", "single-unscented"): 1,
        ("pantry-coffee", "mild"): 1,
    })
    fault: str = ""
    adds: int = 0
    detail_visits: dict[str, int] = field(default_factory=dict)


_sessions: OrderedDict[str, Session] = OrderedDict()


def get_session(request: Request) -> tuple[str, Session]:
    key = request.cookies.get(COOKIE, "")
    if key in _sessions:
        _sessions.move_to_end(key)
        return key, _sessions[key]

    fault = request.query_params.get("fault", "")
    key = token_urlsafe(24)
    session = Session(fault=fault if fault in FAULTS else "")
    _sessions[key] = session
    if len(_sessions) > 512:
        _sessions.popitem(last=False)
    return key, session


def with_session(response: Response, key: str) -> Response:
    response.set_cookie(COOKIE, key, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return response


def variants_for(product: Product, session: Session) -> tuple[Variant, ...]:
    if session.fault == "budget":
        increase = BUDGET_INCREASE.get(product.category, 0)
        return tuple(replace(variant, price=variant.price + increase) for variant in product.variants)
    if session.fault == "sold-out" and product.category in BUDGET_INCREASE:
        return tuple(replace(variant, stock=0) for variant in product.variants)
    if session.fault == "price-change" and session.adds and product.id == "bags-easy":
        return tuple(replace(variant, price=variant.price + 14_000) for variant in product.variants)
    if session.fault == "spec-mismatch":
        mismatches = {
            "물티슈": {"scented"},
            "쓰레기봉투": {"flat", "20l"},
            "파스타면": {"1-serving"},
            "토마토 파스타 소스": {"cream-sauce", "salsa"},
            "캠핑 의자": {"fixed"},
            "캠핑 랜턴": {"keyring"},
        }.get(product.category)
        if mismatches:
            return tuple(
                variant for variant in product.variants
                if any(tag in mismatches for tag, _ in variant.attributes)
            )
    return product.variants


def money(amount: int) -> str:
    return f"{amount:,}원"


def attribute(name: str, text: str, value: str | int = "", *, unit: str = "") -> str:
    unit_attr = f' data-zc-unit="{escape(unit)}"' if unit else ""
    return (
        f'<span data-zc-field="{name}" data-zc-value="{escape(str(value))}"{unit_attr}>'
        f'{escape(text)}</span>'
    )


def view(key: str, session: Session, page: str, title: str, content: str, *, status: int = 200):
    count = sum(session.cart.values())
    fault = (
        f'<span class="fault">예외 검증 모드 · {escape(session.fault)}</span>'
        if session.fault else ""
    )
    html = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · ZoroCart 테스트 상점</title><style>{CSS}</style></head>
<body><header><div class="shell nav">
<a class="brand" href="/store">ZoroCart</a>
<span class="demo">로컬 테스트 상점</span>{fault}
<a class="cart-link" href="/store/cart" data-zc-control="cart">장바구니 <span>{count}</span></a>
</div></header>
<main class="shell" data-zc-view="{page}">{content}</main>
<footer class="shell">시연용으로 구성한 합성 상품입니다.</footer></body></html>"""
    return with_session(HTMLResponse(html, status_code=status), key)


def search_box(query: str = "") -> str:
    return f"""<form class="search" action="/store/search" method="get" role="search">
<label class="sr-only" for="query">상품 검색어</label>
<input id="query" name="q" value="{escape(query)}" maxlength="100"
       placeholder="상품을 검색하세요" data-zc-control="search-input" autocomplete="off" required>
<button type="submit" data-zc-control="search-submit">검색</button>
</form>"""


def blocked(key: str, session: Session):
    return view(key, session, "blocked", "접근 제한", """
<h1>접근 제한</h1><p>테스트 환경에서 접근 제한 상태를 재현했습니다.</p>""", status=403)


@router.get("")
@router.get("/")
async def homepage(request: Request):
    key, session = get_session(request)
    if session.fault == "blocked":
        return blocked(key, session)
    categories = "".join(
        f'<li>{escape(category)}</li>' for category in ALIASES if category != "커피"
    )
    return view(key, session, "home", "상품 검색", f"""
<section class="hero"><h1>상품 검색</h1>{search_box()}</section>
<ul class="categories" aria-label="상점 상품 분류">{categories}</ul>""")


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", text.casefold())


def matching_products(query: str) -> list[Product]:
    normalized = normalize(query)
    categories = {
        category for category, aliases in ALIASES.items()
        if any(normalize(alias) in normalized for alias in aliases)
    }
    if "토마토 파스타 소스" in categories and normalize("소스") in normalized:
        categories.discard("파스타면")
    return [
        product for product in PRODUCTS
        if product.category in categories
        or (len(normalized) > 1 and normalized in normalize(product.name))
    ]


def product_card(product: Product, variants: tuple[Variant, ...]) -> str:
    best = variants[0]
    heading = attribute("name", product.name, product.name)
    category = attribute("category", product.category, product.category)
    price = attribute("price", money(min(variant.price for variant in variants)),
                      min(variant.price for variant in variants))
    shipping = attribute("shipping", best.shipping, best.shipping)
    stock = max(variant.stock for variant in variants)
    availability = attribute("stock", "구매 가능" if stock else "품절", stock)
    return f"""<article class="card" data-zc-product data-zc-id="{product.id}">
<a href="/store/product/{product.id}" data-zc-control="card" data-zc-id="{product.id}">
<div class="product-image" aria-hidden="true">{product.emoji}</div>
<div class="card-content"><p class="category">{category}</p><h2>{heading}</h2>
<p class="price">{price}부터</p><p>{shipping} · {availability}</p></div></a></article>"""


@router.get("/search")
async def search(request: Request, q: str = ""):
    key, session = get_session(request)
    if session.fault == "blocked":
        return blocked(key, session)
    query = q.strip()[:100]
    cards = [
        product_card(product, variants)
        for product in matching_products(query)
        if (variants := variants_for(product, session))
    ]
    results = (
        '<div class="catalog">' + "".join(cards) + "</div>"
        if cards else '<p class="empty" data-zc-empty>검색 결과가 없습니다.</p>'
    )
    return view(key, session, "results", "검색 결과", f"""
{search_box(query)}
<h1>검색 결과 <small>{len(cards)}개</small></h1>
<p class="muted">검색어 {escape(query)}</p>{results}""")


CAPACITY_LABEL = {"g": "g", "ml": "mL", "serving": "인분", "piece": "개"}


def detail_variant(variant: Variant, selected: bool, *, incomplete: bool, product: Product) -> str:
    pack_unit = {"물티슈": "팩", "쓰레기봉투": "팩", "파스타면": "봉",
                 "토마토 파스타 소스": "병"}.get(product.category, "개")
    capacity_label = (
        "매" if product.category in {"물티슈", "쓰레기봉투"} else CAPACITY_LABEL[variant.unit]
    )
    cost = (
        attribute("shipping-price", "배송비 미정")
        if incomplete else attribute("shipping-price", money(variant.shipping_price), variant.shipping_price)
    )
    attributes_list = "" if incomplete else " ".join(
        attribute("attributes", label, name) for name, label in variant.attributes
    )
    option = attribute("option", variant.label, variant.label)
    price = (
        attribute("price", "옵션 가격 미정")
        if incomplete else attribute("price", money(variant.price), variant.price)
    )
    pack = attribute("pack", f"{variant.pack}{pack_unit}", variant.pack)
    capacity = attribute(
        "capacity", f"{variant.capacity}{capacity_label}", variant.capacity, unit=variant.unit,
    )
    stock = attribute("stock", f"최대 {variant.stock} 구매 가능" if variant.stock else "품절", variant.stock)
    shipping = attribute("shipping", variant.shipping, variant.shipping)
    remarks = attributes_list or '<span class="muted">상품 사양 미정</span>'
    disabled = " disabled" if not variant.stock else ""
    return f"""<div class="option-tile" data-zc-variant data-zc-id="{variant.id}">
<button type="button" class="option" data-zc-control="option" data-zc-id="{variant.id}"
        aria-pressed="{str(selected).lower()}"{disabled}>{option}</button>
<dl><div><dt>옵션 가격</dt><dd>{price}</dd></div>
<div><dt>주문 단위당 구성</dt><dd>{pack} · 총 {capacity}</dd></div>
<div><dt>재고</dt><dd>{stock}</dd></div>
<div><dt>배송</dt><dd>{shipping} · {cost}</dd></div></dl>
<p class="tags">{remarks}</p></div>"""


@router.get("/product/{product_id}")
async def detail(request: Request, product_id: str):
    key, session = get_session(request)
    if session.fault == "blocked":
        return blocked(key, session)
    product = PRODUCT_BY_ID.get(product_id)
    variants = variants_for(product, session) if product else ()
    if not variants:
        return view(key, session, "detail", "상품을 찾을 수 없습니다", """
<h1>상품을 찾을 수 없습니다</h1><p>검색 결과에서 다른 상품을 선택할 수 있습니다.</p>""", status=404)

    first = next((variant for variant in variants if variant.stock), variants[0])
    options = "".join(detail_variant(
        variant, variant.id == first.id, incomplete=session.fault == "missing-info", product=product,
    ) for variant in variants)
    name = attribute("name", product.name, product.name)
    category = attribute("category", product.category, product.category)
    no_stock = "disabled" if not first.stock else ""
    stale_delay = 0
    if session.fault == "stale-target":
        visits = session.detail_visits.get(product.id, 0) + 1
        session.detail_visits[product.id] = visits
        if visits >= 3:
            stale_delay = STALE_OPTION_DELAY_MS
    return view(key, session, "detail", product.name, f"""
<div class="detail" data-zc-product data-zc-id="{product.id}">
<div class="product-image detail-image" aria-hidden="true">{product.emoji}</div>
<section><p class="category">{category}</p><h1>{name}</h1>
<h2>판매 옵션</h2><div class="options">{options}</div>
<form class="add-form" action="/store/cart/add" method="post">
<input type="hidden" name="product" value="{product.id}">
<input type="hidden" name="option" value="{first.id}">
<label>주문 수량 <input type="number" name="quantity" min="1" max="{max(first.stock, 1)}"
 value="1" data-zc-control="quantity" required {no_stock}></label>
<button type="submit" data-zc-control="add" {no_stock}>장바구니 담기</button>
</form></section></div>
<script data-stale-delay="{stale_delay}">{DETAIL_JS}</script>""")


@router.post("/cart/add")
async def add_to_cart(request: Request):
    key, session = get_session(request)
    if session.fault == "blocked":
        return blocked(key, session)
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    product_id = form.get("product", [""])[0]
    option_id = form.get("option", [""])[0]
    raw_quantity = form.get("quantity", [""])[0]
    quantity = int(raw_quantity) if re.fullmatch(r"[0-9]{1,2}", raw_quantity) else 0
    product = PRODUCT_BY_ID.get(product_id)
    variant = next(
        (value for value in variants_for(product, session) if value.id == option_id), None,
    ) if product else None
    if (
        not variant or not 1 <= quantity <= variant.stock
        or session.fault == "missing-info"
    ):
        return view(key, session, "detail", "장바구니에 담을 수 없습니다", """
<h1>장바구니에 담을 수 없습니다</h1><p>판매 옵션과 수량을 확인하세요.</p>""", status=409)

    session.adds += 1
    delay = session.adds == 1 and session.fault.startswith("add-timeout-")
    cart_key = (product_id, option_id)
    if not (delay and session.fault == "add-timeout-unapplied"):
        session.cart[cart_key] = session.cart.get(cart_key, 0) + quantity
    if delay:
        await asyncio.sleep(ADD_RESPONSE_DELAY_SECONDS)
    return with_session(RedirectResponse("/store/cart", status_code=303), key)


def cart_snapshot(session: Session) -> str:
    rows = [(product_id, option_id, quantity)
            for (product_id, option_id), quantity in sorted(session.cart.items())]
    return json.dumps(rows, separators=(",", ":"))


@router.post("/cart/clear")
async def clear_cart(request: Request):
    key, session = get_session(request)
    if session.fault == "blocked":
        return blocked(key, session)
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"), keep_blank_values=True)
    if session.fault == "clear-mismatch" or not session.cart or form != {"cart": [cart_snapshot(session)]}:
        return view(key, session, "cart", "장바구니 비우기 보류", """
<h1>장바구니 비우기 보류</h1><p>지금 장바구니와 요청에 포함된 품목을 대조할 수 없습니다.</p>""", status=409)
    session.cart.clear()
    return with_session(RedirectResponse("/store/cart", status_code=303), key)


@router.get("/cart")
async def cart(request: Request):
    key, session = get_session(request)
    if session.fault == "blocked":
        return blocked(key, session)
    rows = []
    subtotal = 0
    for (product_id, option_id), quantity in session.cart.items():
        product = PRODUCT_BY_ID[product_id]
        current = next(
            (variant for variant in variants_for(product, session) if variant.id == option_id), None,
        )
        variant = current or next(variant for variant in product.variants if variant.id == option_id)
        subtotal += variant.price * quantity
        name = attribute("name", product.name, product.name)
        option = attribute("option", variant.label, variant.label)
        price = attribute("price", money(variant.price), variant.price)
        pack = attribute("pack", f"{variant.pack}개 구성", variant.pack)
        count = attribute("quantity", f"{quantity}개", quantity)
        shipping = attribute("shipping", variant.shipping, variant.shipping)
        cost = attribute("shipping-price", money(variant.shipping_price), variant.shipping_price)
        rows.append(f"""<tr data-zc-row data-zc-id="{product_id}" data-zc-option="{option_id}">
<th scope="row">{name}<small>{option} · {pack}</small></th><td>{price}</td>
<td>{count}</td><td>{shipping} · {cost}</td><td>{money(variant.price * quantity)}</td></tr>""")
    content = f"""<div class="table-scroll"><table><thead><tr>
<th scope="col">상품 및 옵션</th><th scope="col">옵션 가격</th><th scope="col">주문 수량</th>
<th scope="col">배송</th><th scope="col">상품가</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<div class="cart-controls">
<form action="/store/cart/clear" method="post">
<input type="hidden" name="cart" value="{escape(cart_snapshot(session))}">
<button type="submit" class="clear-cart" data-zc-control="clear-cart">장바구니 비우기</button>
</form>
<p class="cart-total">장바구니 상품가 합계 <strong>{attribute("subtotal", money(subtotal), subtotal)}</strong></p>
</div>"""
    if not rows:
        content = '<p class="empty" data-zc-empty>장바구니가 비어 있습니다.</p>'
    return view(key, session, "cart", "장바구니", f'<h1>장바구니</h1>{content}')


DETAIL_JS = """
const options = document.querySelector('.options');
const form = document.querySelector('.add-form');
options.addEventListener('click', (event) => {
  const current = event.target.closest('[data-zc-control="option"]');
  if (!current || current.disabled) return;
  options.querySelectorAll('[aria-pressed]').forEach(element => {
    element.setAttribute('aria-pressed', String(element === current));
  });
  form.elements.option.value = current.dataset.zcId;
  form.elements.quantity.max = current.closest('[data-zc-variant]')
    .querySelector('[data-zc-field="stock"]').dataset.zcValue;
  form.elements.quantity.value = '1';
});
const staleDelay = Number(document.currentScript.dataset.staleDelay);
if (staleDelay > 0 && !sessionStorage.getItem('zoroCartStaleTarget')) {
  window.setTimeout(() => {
    sessionStorage.setItem('zoroCartStaleTarget', '1');
    options.querySelectorAll('[data-zc-control="option"]').forEach(element => {
      element.replaceWith(element.cloneNode(true));
    });
  }, staleDelay);
}
"""


CSS = """
:root{color-scheme:light;--ink:#202a25;--muted:#53675c;--paper:#f7f9f5;--line:#d9e3dc;
  --green:#215c44;--pale:#e2eee4;--white:#fff}
*{box-sizing:border-box}body{margin:0;min-height:100vh;font:16px/1.55 system-ui,sans-serif;
  color:var(--ink);background:var(--paper)}
a{color:inherit;text-decoration:none}button,input{font:inherit}
.shell{width:min(100% - 48px,1080px);margin-inline:auto}header{background:var(--white);
  border-bottom:1px solid var(--line)}
.nav{display:flex;align-items:center;min-height:70px;gap:18px;flex-wrap:wrap}
.brand{font-weight:800;letter-spacing:-.05em;font-size:25px}
.demo,.fault,.category{color:var(--green);font-weight:650;font-size:13px}.fault{color:#7e4518}
.cart-link{margin-left:auto;background:var(--pale);border-radius:8px;padding:8px 12px}
.cart-link span{background:var(--green);color:var(--white);border-radius:50%;padding:1px 6px}
main{padding:40px 0 60px}footer{font-size:13px;color:var(--muted);border-top:1px solid var(--line);
  padding:16px 0 28px}
h1,h2,p{margin:0 0 12px}h1{font-size:clamp(28px,5vw,43px);letter-spacing:-.06em;line-height:1.2}
h1 small{font-size:19px;color:var(--muted)}h2{font-size:18px;letter-spacing:-.03em}
.muted{color:var(--muted)}.hero{padding:38px 0 26px}.search{display:flex;gap:8px;max-width:640px;
  margin:0 0 28px}.search input{width:100%;min-width:0;padding:12px 14px;border:1px solid var(--line);
  border-radius:8px;background:var(--white)}button{border:0;background:var(--green);color:white;
  padding:10px 18px;border-radius:8px;cursor:pointer;white-space:nowrap}button:disabled{
  background:#e3e8e3;color:#67716b;cursor:default}
:focus-visible{outline:3px solid #007c56;outline-offset:3px}
.sr-only{position:absolute;clip-path:inset(50%);width:1px;height:1px;overflow:hidden;white-space:nowrap}
.categories{display:flex;gap:10px;flex-wrap:wrap;list-style:none;margin:0;padding:0}
.categories li,.tags span{background:var(--pale);padding:4px 9px;border-radius:6px;font-size:13px}
.catalog{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(250px,100%),1fr));gap:16px}
.card{background:var(--white);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.card a{display:block;height:100%}.card a:hover h2{text-decoration:underline}
.card-content{padding:14px}.card .price{font-weight:750}
.product-image{display:grid;place-items:center;background:var(--pale);min-height:130px;font-size:56px}
.detail{display:grid;grid-template-columns:minmax(160px,1fr) minmax(320px,2fr);gap:28px}
.detail-image{min-height:220px;align-self:start;font-size:76px;border-radius:12px}
.detail h1{margin-bottom:26px}.options{display:grid;gap:10px}
.option-tile{background:var(--white);padding:14px;border:1px solid var(--line);border-radius:10px}
.option{color:var(--ink);background:var(--pale);padding:7px 10px;text-align:left;white-space:normal}
.option[aria-pressed="true"]{color:white;background:var(--green)}.option:disabled[aria-pressed="true"]{
  color:#67716b;background:#e3e8e3}
.option-tile dl{display:grid;gap:4px;margin:10px 0;font-size:14px}
.option-tile dl div{display:grid;grid-template-columns:115px auto}
.option-tile dt{color:var(--muted)}.option-tile dd{margin:0}
.tags{display:flex;flex-wrap:wrap;gap:4px;margin:0}.add-form{display:flex;gap:16px;
  align-items:end;flex-wrap:wrap;margin-top:20px}.add-form label{display:grid;gap:4px}
.add-form input{width:90px;padding:8px;border:1px solid var(--line);border-radius:8px}
.table-scroll{overflow:auto}table{width:100%;border-collapse:collapse;background:var(--white);
  text-align:left}th,td{padding:12px;border-bottom:1px solid var(--line)}
thead th{color:var(--muted);font-size:14px;font-weight:650}tbody th{min-width:220px}
tbody small{display:block;color:var(--muted);font-weight:400}
.cart-controls{display:flex;align-items:center;justify-content:space-between;gap:12px;
  flex-wrap:wrap;margin-top:18px}.cart-total{display:flex;gap:12px;margin:0}
.clear-cart{padding:8px 12px;color:#8a321c;background:var(--white);border:1px solid #e4c9bd}
.clear-cart:hover{background:#faeee8}.empty{padding:18px;
  background:var(--white);border:1px solid var(--line);border-radius:8px}
@media(max-width:690px){.shell{width:calc(100% - 28px)}.nav{gap:10px}.detail{grid-template-columns:1fr}
  .detail-image{min-height:115px}.hero{padding-top:16px}}
"""
