"""Observe and operate the shopping surface through an isolated Playwright page."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit

from playwright.async_api import ElementHandle, Error as PlaywrightError, Page, Request, Route

from .coupang_evidence import parse_pack, parse_price, parse_quantity

NAVER_STORE_URL = "https://shopping.naver.com/ns/home"

CONTROL_ACTIONS = {
    "search-input": "TYPE_TEXT",
    "search-submit": "CLICK",
    "card": "CLICK",
    "option": "SELECT_OPTION",
    "quantity": "SET_QUANTITY",
    "add": "CLICK",
    "cart": "CLICK",
    "clear-cart": "CLICK",
}
VIEW_ROLES = {
    "home": {"search-input", "search-submit", "cart"},
    "results": {"search-input", "search-submit", "card", "cart"},
    "detail": {"option", "quantity", "add", "cart"},
    "cart": {"cart", "clear-cart"},
}
FIELDS = (
    "name", "price", "pack", "capacity", "stock", "shipping", "shipping-price",
    "attributes", "quantity", "option", "category",
)
BLOCKED_PATH = re.compile(
    r"(?:^|[/_.-])(?:login|signin|captcha|access-denied)(?:[/_.-]|$)", re.I
)
FORBIDDEN_PATH = re.compile(
    r"(?:^|[/_.-])(?:order|orders|checkout|payment|pay|purchase|buy)(?:[/_.-]|$)", re.I
)
SAFE_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,120}\Z")
COUNT = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
_ACCESS_REASON = {
    401: "상점에서 로그인이 필요하다고 응답했습니다",
    403: "상점에서 브라우저 접근 제한을 반환했습니다",
    429: "상점의 요청 횟수 제한에 도달했습니다",
}


class BrowserError(RuntimeError):
    """The browser could not establish a required shopping fact or action."""


class BrowserBlocked(BrowserError):
    """The observed page requires intervention or falls outside the allowed scope."""


class StaleObservation(BrowserError):
    """A decision referred to a changed document or DOM node."""


class AmbiguousAdd(BrowserError):
    """An attempted add must be reconciled by reading the cart before any retry."""


class AmbiguousClear(BrowserError):
    """An attempted reset requires a fresh read of the cart before continuing."""


@dataclass(frozen=True)
class Target:
    id: str
    role: str
    key: str
    label: str
    action: str
    facts: dict[str, object] = field(default_factory=dict)
    url: str | None = None


@dataclass(frozen=True)
class Observation:
    view: str
    targets: tuple[Target, ...]
    facts: dict[str, object]
    epoch: int

    def model_context(self) -> dict[str, object]:
        return {
            "view": self.view,
            "facts": self.facts,
            "targets": [
                {
                    "id": target.id,
                    "role": target.role,
                    "key": target.key,
                    "label": target.label,
                    "action": target.action,
                    "facts": target.facts,
                }
                for target in self.targets
            ],
        }


@dataclass(frozen=True)
class Offer:
    product_id: str
    option_id: str
    title: str
    label: str
    unit_price: int | None
    pack_count: int | None
    capacity: int | None
    capacity_unit: str | None
    stock: int | None
    delivery: str
    shipping_fee: int | None
    attributes: tuple[str, ...]
    category: str
    url: str
    selected: bool


@dataclass(frozen=True)
class CartLine:
    product_id: str
    option_id: str
    title: str
    label: str
    unit_price: int
    quantity: int
    pack_count: int | None
    delivery: str
    shipping_fee: int | None


@dataclass(frozen=True)
class CoupangApproval:
    """Operator-supplied scope and DOM selectors verified against approved pages."""

    reference: str = field(repr=False)
    home_url: str
    cart_url: str
    read_paths: tuple[str, ...]
    add_path: str | None = None
    dom: dict[str, object] = field(default_factory=dict, repr=False)
    add_fields: dict[str, str] | None = field(default=None, repr=False)
    asset_hosts: tuple[str, ...] = ()
    clear_path: str | None = None
    clear_items_field: str | None = None
    cart_read_paths: tuple[str, ...] = ()
    add_origin: str | None = None
    clear_origin: str | None = None
    desktop_ui: bool = False

    @classmethod
    def from_file(cls, path: str | Path) -> CoupangApproval:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            approval = cls(
                reference=data["reference"],
                home_url=data["home_url"],
                cart_url=data["cart_url"],
                read_paths=tuple(data["read_paths"]),
                add_path=data.get("add_path"),
                dom=data.get("dom", {}),
                add_fields=data.get("add_fields"),
                asset_hosts=tuple(data.get("asset_hosts", ())),
                clear_path=data.get("clear_path"),
                clear_items_field=data.get("clear_items_field"),
                cart_read_paths=tuple(data.get("cart_read_paths", ())),
                add_origin=data.get("add_origin"),
                clear_origin=data.get("clear_origin"),
                desktop_ui=data.get("desktop_ui", False),
            )
            approval.validate()
        except (OSError, ValueError, KeyError, TypeError, re.error) as exc:
            raise BrowserBlocked("승인된 쿠팡 범위와 화면 설정을 확인할 수 없습니다.") from exc
        return approval

    def validate(self) -> None:
        if (
            not isinstance(self.reference, str)
            or not self.reference.strip()
            or type(self.desktop_ui) is not bool
            or not self.read_paths
            or any(
                not isinstance(path, str) or not path.startswith("/") or len(path) > 250
                for path in (*self.read_paths, *self.cart_read_paths)
            )
        ):
            raise ValueError("Invalid approved paths or reference")
        if not self.desktop_ui or self.add_path is not None or self.add_fields is not None:
            if (
                not isinstance(self.add_path, str)
                or not re.fullmatch(r"/[A-Za-z0-9/_\-.]+", self.add_path)
                or BLOCKED_PATH.search(self.add_path)
                or FORBIDDEN_PATH.search(self.add_path)
                or not isinstance(self.add_fields, dict)
                or set(self.add_fields) != {"product", "option", "quantity"}
                or any(
                    not isinstance(name, str) or not name.strip() for name in self.add_fields.values()
                )
                or len(set(self.add_fields.values())) != 3
            ):
                raise ValueError("Incomplete approved add settings")
        if not isinstance(self.home_url, str) or not isinstance(self.cart_url, str):
            raise ValueError("Approved navigation URLs must be strings")
        home, cart = urlsplit(self.home_url), urlsplit(self.cart_url)
        read_scopes = self.read_scopes()
        if (
            home.scheme != "https"
            or cart.scheme != "https"
            or home.hostname not in {"www.coupang.com", "m.coupang.com", "coupang.com"}
            or cart.hostname not in {home.hostname, "cart.coupang.com"}
            or any(
                part.port not in (None, 443)
                or part.username is not None
                or part.password is not None
                or part.fragment
                for part in (home, cart)
            )
            or any(
                not any(re.fullmatch(rule, part.path or "/") for rule in read_scopes[_origin(part)])
                or BLOCKED_PATH.search(part.path)
                or FORBIDDEN_PATH.search(part.path)
                for part in (home, cart)
            )
        ):
            raise ValueError("Invalid approved Coupang origin or navigation")
        for setting in (self.add_origin, self.clear_origin):
            if setting is None:
                continue
            if not isinstance(setting, str):
                raise ValueError("Approved mutation origin must be a string")
            parsed = urlsplit(setting)
            if (
                _origin(parsed) not in {_origin(home), _origin(cart)}
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("Invalid approved cart mutation origin")
        if not isinstance(self.dom, dict):
            raise ValueError("Missing approved DOM settings")
        if not self.desktop_ui or self.dom:
            for member, keys in (
                ("views", ("home", "results", "detail", "cart")),
                ("controls", tuple(role for role in CONTROL_ACTIONS if role != "clear-cart")),
                ("fields", tuple(key for key in FIELDS if key != "capacity")),
            ):
                group = self.dom.get(member)
                if not isinstance(group, dict) or any(
                    not isinstance(group.get(key), str) or not group[key].strip() for key in keys
                ):
                    raise ValueError("Incomplete approved DOM settings")
            for key in (
                "product", "variant", "cart_row", "empty", "selected",
                "key_attr", "option_attr",
            ):
                value = self.dom.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("Incomplete approved DOM settings")
        clear_selector = self.dom.get("controls", {}).get("clear-cart")
        if self.clear_path is not None or self.clear_items_field is not None or clear_selector is not None:
            if (
                not isinstance(self.clear_path, str)
                or not re.fullmatch(r"/[A-Za-z0-9/_\-.]+", self.clear_path)
                or self.clear_path == self.add_path
                or BLOCKED_PATH.search(self.clear_path)
                or FORBIDDEN_PATH.search(self.clear_path)
                or not isinstance(self.clear_items_field, str)
                or not self.clear_items_field.strip()
                or (not self.desktop_ui and (
                    not isinstance(clear_selector, str) or not clear_selector.strip()
                ))
            ):
                raise ValueError("Incomplete approved cart-clear settings")
            row_attr = self.dom.get("clear_row_id_attr")
            if row_attr is not None and (
                not isinstance(row_attr, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", row_attr)
            ):
                raise ValueError("Invalid approved cart row identifier attribute")
        if any(not re.fullmatch(r"[a-z0-9.-]+", host) for host in self.asset_hosts):
            raise ValueError("Invalid approved asset hostname")

    @property
    def can_add(self) -> bool:
        return self.add_path is not None and bool(self.add_fields)

    @property
    def can_clear(self) -> bool:
        return self.clear_path is not None and bool(self.clear_items_field)

    @property
    def can_write(self) -> bool:
        return self.can_add and self.can_clear

    @property
    def native_read_only(self) -> bool:
        return self.desktop_ui and not (self.can_add or self.can_clear)

    def read_scopes(self) -> dict[tuple[str, str | None, int | None], tuple[str, ...]]:
        home = _origin(urlsplit(self.home_url))
        cart_url = urlsplit(self.cart_url)
        cart = _origin(cart_url)
        if home == cart:
            return {home: (*self.read_paths, *self.cart_read_paths)}
        cart_paths = self.cart_read_paths or (re.escape(cart_url.path or "/"),)
        return {home: self.read_paths, cart: cart_paths}


LOCAL_DOM = {
    "views": {
        view: f'[data-zc-view="{view}"]'
        for view in ("home", "results", "detail", "cart", "blocked")
    },
    "controls": {role: f'[data-zc-control="{role}"]' for role in CONTROL_ACTIONS},
    "fields": {name: f'[data-zc-field="{name}"]' for name in FIELDS},
    "product": "[data-zc-product]",
    "variant": "[data-zc-variant]",
    "cart_row": "[data-zc-row]",
    "empty": "[data-zc-empty]",
    "selected": '[aria-pressed="true"], [aria-selected="true"], [data-zc-selected="true"]',
    "key_attr": "data-zc-id",
    "option_attr": "data-zc-option",
    "unit_attr": "data-zc-unit",
}

# Snapshot and node references come from the same document evaluation. The page
# can supply product facts and labels, while code owns selectors and operations.
SNAPSHOT = r"""
(cfg) => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim().slice(0, 240);
  const visible = (el) => Boolean(el && el.isConnected && el.getClientRects().length &&
    !el.closest("[hidden], [inert], [aria-hidden=true]") &&
    !["hidden", "collapse"].includes(getComputedStyle(el).visibility));
  const active = (el) => !el.matches(":disabled") && !el.closest("[aria-disabled=true]");
  const find = (root, selector) => selector ?
    [...root.querySelectorAll(selector)].filter(visible) : [];
  const first = (root, selector) => find(root, selector)[0];
  const label = (el) => clean(el.getAttribute("aria-label") ||
    [...(el.labels || [])].map(node => node.innerText).join(" ") ||
    el.innerText || el.getAttribute("placeholder") || el.getAttribute("title"));
  const key = (el, attr = cfg.key_attr) => clean(el?.getAttribute(attr));
  const href = (el) => el.tagName === "A" ? el.href : null;
  const quantityControl = (el) => el.matches('input[type=number], input[type=text], select');
  const fieldData = (root) => Object.fromEntries(Object.entries(cfg.fields).map(([name, selector]) =>
    [name, find(root, selector).slice(0, 15).map(el => ({
      text: clean(name === "quantity" && quantityControl(el) ? el.value : el.innerText),
      unit: clean(cfg.unit_attr && el.getAttribute(cfg.unit_attr)),
    }))]));
  const blocked = Boolean(first(document, cfg.views.blocked) || first(document,
    'input[type=password], iframe[src*="recaptcha"], iframe[src*="captcha"], [data-sitekey]') ||
    find(document, 'h1, h2, [role=alert]').some(el =>
      /^(?:access denied|forbidden|접근이 제한|접근 권한이 없|비정상적인 접근|본인 인증|로그인이 필요|captcha)/i
        .test(label(el))));
  const view = blocked ? "blocked" :
    (["cart", "detail", "results", "home"].find(name => first(document, cfg.views[name])) || "unknown");
  if (blocked) return {root: document.documentElement, nodes: [], payload: {
    view, controls: [], offers: [], rows: [], quantities: [], empty: false,
  }};
  const nodes = [];
  const controls = [];
  for (const [role, selector] of Object.entries(cfg.controls)) {
    for (const el of find(document, selector).slice(0, role === "card" ? 6 : 24)) {
      if (!active(el)) continue;
      const name = label(el);
      if (/바로\s*구매|주문하기|결제하기|buy\s*now|checkout|place\s*order/i.test(name)) continue;
      if (role === "search-input" && !el.matches('input[type=search], input[type=text], input:not([type])')) continue;
      if (role === "quantity" && (!quantityControl(el) || el.readOnly)) continue;
      if (["card", "cart"].includes(role) && el.tagName !== "A") continue;
      if (["search-submit", "option", "add", "clear-cart"].includes(role) &&
          !el.matches('button, [role=button], input[type=submit]')) continue;
      if (role === "add" && !/장바구니\s*담기|장바구니에\s*담기|add\s*to\s*cart/i.test(name)) continue;
      if (role === "clear-cart" &&
          !/장바구니\s*(?:전체\s*)?비우기|전체\s*삭제|clear\s*(?:the\s*)?cart|empty\s*(?:the\s*)?cart/i.test(name)) continue;
      nodes.push(el);
      controls.push({
        role, key: key(el) || (role === "card" ? "" : role),
        label: name, href: href(el),
        tag: el.tagName.toLowerCase(), aria_role: clean(el.getAttribute("role")),
        value: ["search-input", "quantity"].includes(role) ? clean(el.value) : "",
        selected: role === "option" ? el.matches(cfg.selected) : false,
        form: el.form ? el.form.action : null,
        method: el.form ? el.form.method : null,
        destination: clean(el.getAttribute("target") || el.getAttribute("formtarget") || el.form?.target),
        card_facts: role === "card" ? fieldData(el) : {},
        options: el.tagName === "SELECT" ? [...el.options]
          .filter(option => !option.disabled).map(option => clean(option.value)) : [],
      });
    }
  }
  const product = view === "detail" ? first(document, cfg.product) : null;
  const offers = product ? find(product, cfg.variant).map(variant => ({
    product_id: key(product),
    option_id: key(variant),
    selected: variant.matches(cfg.selected) || Boolean(first(variant, cfg.selected)),
    fields: fieldData(variant),
    product_fields: fieldData(product),
  })) : [];
  const rows = view === "cart" ? find(document, cfg.cart_row).map(row => ({
    product_id: key(row), option_id: key(row, cfg.option_attr),
    clear_id: cfg.clear_row_id_attr ? key(row, cfg.clear_row_id_attr) : "",
    fields: fieldData(row),
  })) : [];
  const quantities = view === "detail" ? find(document, cfg.controls.quantity)
    .filter(quantityControl).map(el => clean(el.value)) : [];
  return {root: document.documentElement, nodes, payload: {
    view, controls, offers, rows, quantities, empty: view === "cart" && Boolean(first(document, cfg.empty)),
  }};
}
"""


def _origin(parsed) -> tuple[str, str | None, int | None]:
    return parsed.scheme, parsed.hostname, parsed.port or {"https": 443, "http": 80}.get(
        parsed.scheme
    )


def _unique_fields(pairs) -> dict:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Repeated write field")
    return fields


def _same_write_field(actual: object, expected: str | list) -> bool:
    if isinstance(expected, str):
        return type(actual) in {str, int} and str(actual) == expected
    if isinstance(actual, str):
        try:
            actual = json.loads(actual)
        except ValueError:
            return False
    # Compare complete row identities regardless of order. JSON encoding also
    # distinguishes quantity 1 from truthy booleans and keeps duplicate rows.
    return isinstance(actual, list) and sorted(map(json.dumps, actual)) == sorted(map(json.dumps, expected))


def _texts(fields: dict, name: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(entry["text"] for entry in fields.get(name, []) if entry["text"]))


def _text(fields: dict, name: str) -> str:
    texts = _texts(fields, name)
    return texts[0] if len(texts) == 1 else ""


def _number(value: str, units: str) -> int | None:
    match = re.fullmatch(rf"({COUNT})\s*(?:{units})?", value, re.I)
    if not match:
        return None
    amount = int(match[1].replace(",", ""))
    return amount if amount <= 1_000_000_000 else None


def _shipping_fee(value: str) -> int | None:
    if value in ("무료", "무료배송", "배송비 무료"):
        return 0
    return _number(value.removeprefix("배송비").strip(), "원")


def _stock(value: str) -> int | None:
    if value == "품절":
        return 0
    match = re.fullmatch(rf"최대\s+({COUNT})\s*(?:개|팩|세트)?\s*구매 가능", value)
    return _number(match[1] if match else value, "개|팩|세트")


def _capacity(fields: dict) -> tuple[int | None, str | None]:
    entries = fields.get("capacity", [])
    if len(entries) != 1:
        return None, None
    entry = entries[0]
    units = {
        "g": "g", "kg": "kg", "ml": "ml", "l": "l", "인분": "serving",
        "serving": "serving", "servings": "serving", "개": "piece", "매": "piece", "piece": "piece",
    }
    match = re.fullmatch(rf"({COUNT})\s*({'|'.join(units)})?", entry["text"], re.I)
    if not match:
        return None, None
    unit = units.get((match[2] or entry["unit"]).lower())
    if match[2] and entry["unit"] and units.get(entry["unit"].lower()) != unit:
        return None, None
    return (int(match[1].replace(",", "")), unit) if unit else (None, None)


class BrowserAdapter:
    def __init__(
        self,
        page: Page,
        store_url: str,
        mode: str = "local",
        approval: CoupangApproval | str | Path | None = None,
        *,
        action_timeout_ms: int = 4_000,
        max_steps: int = 140,
        deadline: float | None = None,
    ):
        self.page = page
        self.mode = mode
        self.action_timeout_ms = action_timeout_ms
        self.max_steps = max_steps
        self.deadline = deadline
        self.steps = 0
        self._active: Observation | None = None
        self._snapshot: dict | None = None
        self._nodes: dict[str, ElementHandle] = {}
        self._epoch = 0
        self._routing = False
        self._network_blocked = False
        self._blocked_status: int | None = None
        self._pending_path: str | None = None
        self._write_dispatched = False
        self._write_blocked = False
        self._expected_write: dict[str, str | list] | None = None
        self._observed_products: set[str] = set()
        self._results_url: str | None = None
        self._asset_hosts: tuple[str, ...] = ()
        if mode == "local":
            parsed = urlsplit(store_url)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username
                or parsed.password
                or (parsed.query and not re.fullmatch(r"fault=[a-z-]{1,50}", parsed.query))
                or parsed.fragment
                or parsed.path.rstrip("/") not in {"", "/store"}
            ):
                raise BrowserBlocked("시연 상점은 로컬 루프백 주소에서만 열 수 있습니다.")
            base = parsed._replace(path="", query="").geturl()
            self.home_url = base + "/store" + (f"?{parsed.query}" if parsed.query else "")
            self.cart_url = base + "/store/cart"
            self._read_paths = (r"/store", r"/store/search", r"/store/cart", r"/store/product/[\w-]+")
            self._add_path = "/store/cart/add"
            self._add_fields = {"product": "product", "option": "option", "quantity": "quantity"}
            self._clear_path = "/store/cart/clear"
            self._clear_items_field = "cart"
            self._read_scopes = {_origin(parsed): self._read_paths}
            self._write_origins = {self._add_path: _origin(parsed), self._clear_path: _origin(parsed)}
            self._dom = LOCAL_DOM
        elif mode == "naver":
            if store_url != NAVER_STORE_URL or approval is not None:
                raise BrowserBlocked("네이버플러스 스토어 실행에는 공식 홈 주소가 필요합니다.")
            self.home_url = NAVER_STORE_URL
            self.cart_url = ""
            self._read_scopes = {
                _origin(urlsplit(NAVER_STORE_URL)): (r"/ns/home",),
                _origin(urlsplit("https://search.shopping.naver.com")): (r"/ns/search",),
            }
            self._add_path = self._add_fields = None
            self._clear_path = self._clear_items_field = None
            self._write_origins = {}
            self._dom = {}
        elif mode == "coupang" and approval:
            if not isinstance(approval, CoupangApproval):
                approval = CoupangApproval.from_file(approval)
            try:
                approval.validate()
            except (ValueError, TypeError, re.error) as exc:
                raise BrowserBlocked("승인된 쿠팡 범위와 화면 설정을 확인할 수 없습니다.") from exc
            if store_url and store_url.rstrip("/") != approval.home_url.rstrip("/"):
                raise BrowserBlocked("요청 상점 주소가 쿠팡 승인 범위와 다릅니다.")
            self.home_url = approval.home_url
            self.cart_url = approval.cart_url
            self._read_paths = approval.read_paths
            self._add_path = approval.add_path
            self._add_fields = approval.add_fields
            self._clear_path = approval.clear_path
            self._clear_items_field = approval.clear_items_field
            self._read_scopes = approval.read_scopes()
            self._write_origins = {}
            if self._add_path:
                self._write_origins[self._add_path] = _origin(
                    urlsplit(approval.add_origin or self.home_url)
                )
            if self._clear_path:
                self._write_origins[self._clear_path] = _origin(
                    urlsplit(approval.clear_origin or self.cart_url)
                )
            self._dom = approval.dom
            self._asset_hosts = approval.asset_hosts
        else:
            raise BrowserBlocked("쿠팡 실사이트 실행에는 쿠팡 승인 범위와 승인된 화면 설정이 필요합니다.")

    @property
    def can_add(self) -> bool:
        return self._add_path is not None and bool(self._add_fields)

    @property
    def can_clear(self) -> bool:
        return self._clear_path is not None and bool(self._clear_items_field)

    @property
    def can_write(self) -> bool:
        return self.can_add and self.can_clear

    def _in_scope(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            path = parsed.path or "/"
            return (
                parsed.username is None
                and parsed.password is None
                and not parsed.fragment
                and "%" not in path
                and not BLOCKED_PATH.search(path)
                and not FORBIDDEN_PATH.search(path)
                and any(re.fullmatch(rule, path) for rule in self._read_scopes.get(_origin(parsed), ()))
            )
        except ValueError:
            return False

    def _is_write_url(self, url: str, path: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                _origin(parsed) == self._write_origins[path]
                and parsed.path == path
                and parsed.username is None
                and parsed.password is None
                and not (parsed.query or parsed.fragment)
            )
        except (ValueError, KeyError):
            return False

    async def _route(self, route: Route) -> None:
        request = route.request
        url = request.url
        if request.method == "OPTIONS" and self._allowed_preflight(url, request.headers):
            await route.continue_()
        elif request.method == "POST" and self._pending_path and self._is_write_url(url, self._pending_path):
            self._pending_path = None
            if not self._correct_write(request.post_data, request.headers.get("content-type", "")):
                self._write_blocked = True
                await route.abort()
                return
            self._write_dispatched = True
            if self.mode == "coupang":
                # Inspect the approved write response without following method-preserving redirects.
                try:
                    response = await route.fetch(
                        max_redirects=0, max_retries=0, timeout=self.action_timeout_ms,
                    )
                except PlaywrightError:
                    await route.abort()
                    return
                location = response.headers.get("location")
                if response.status in {307, 308} or (
                    location and not self._in_scope(urljoin(url, location))
                ):
                    self._network_blocked = True
                    await route.abort()
                else:
                    await route.fulfill(response=response)
            else:
                await route.continue_()
        elif request.method == "GET" and self._in_scope(url):
            await route.continue_()
        elif request.method == "GET" and request.resource_type in {
            "image", "stylesheet", "script", "font",
        } and not request.is_navigation_request():
            parsed = urlsplit(url)
            if (
                _origin(parsed) in self._read_scopes
                or (parsed.scheme == "https" and parsed.hostname in self._asset_hosts)
            ) and not FORBIDDEN_PATH.search(parsed.path):
                await route.continue_()
            else:
                await route.abort()
        else:
            if self._is_main_navigation(request):
                self._network_blocked = True
            await route.abort()

    def _allowed_preflight(self, url: str, headers: dict[str, str]) -> bool:
        try:
            source = _origin(urlsplit(headers.get("origin", "")))
        except ValueError:
            return False
        if source not in self._read_scopes:
            return False
        method = headers.get("access-control-request-method")
        return (
            (method == "GET" and self._in_scope(url))
            or (method == "POST" and self._pending_path is not None and self._is_write_url(url, self._pending_path))
        )

    def _correct_write(self, body: str | None, content_type: str) -> bool:
        if body is None:
            return False
        media_type = content_type.split(";", 1)[0].strip().lower()
        try:
            if media_type == "application/json":
                data = json.loads(body, object_pairs_hook=_unique_fields)
            elif media_type == "application/x-www-form-urlencoded":
                data = _unique_fields(parse_qsl(body, keep_blank_values=True, max_num_fields=100))
            else:
                return False
        except ValueError:
            return False
        return (
            self._expected_write is not None
            and isinstance(data, dict)
            and all(_same_write_field(data.get(key), value) for key, value in self._expected_write.items())
        )

    @property
    def can_retry_unchanged_cart(self) -> bool:
        # The synthetic shop resolves the mutation before any simulated reply delay.
        return self.mode == "local" or not self._write_dispatched

    @staticmethod
    async def _block_other_pages(route: Route) -> None:
        await route.abort()

    async def _initialize(self) -> None:
        if not self._routing:
            # This adapter runs in a new context with service_workers="block".
            # The selected page route takes precedence. Other pages, including
            # the first request from a popup, have no approved action target.
            if self.mode == "coupang":
                await self.page.context.route("**/*", self._block_other_pages)
            await self.page.route("**/*", self._route)
            self.page.on("response", self._on_response)
            self._routing = True

    def _is_main_navigation(self, request: Request) -> bool:
        if not request.is_navigation_request():
            return False
        try:
            return request.frame == self.page.main_frame
        except PlaywrightError:
            # A frame may not be available for a newly issued navigation.
            # The request can still be denied without assigning a page blocker.
            return False

    def _on_response(self, response) -> None:
        if response.status in _ACCESS_REASON and self._is_main_navigation(response.request):
            self._network_blocked = True
            self._blocked_status = response.status

    def _step(self, *, before_add: bool = False, recovery: bool = False) -> None:
        # Leave room for opening the cart and the final verification decision.
        steps_needed = 3 if before_add else 1
        if not recovery and self.steps + steps_needed > self.max_steps:
            raise BrowserBlocked("브라우저 조작 단계 한도에 도달했습니다.")
        reserve = 2 * self.action_timeout_ms / 1_000 if before_add else 0
        if self.deadline is not None and time.monotonic() + reserve >= self.deadline:
            raise BrowserBlocked("장바구니 검증에 필요한 실행 시간이 남아 있지 않습니다.")
        self.steps += 1

    def _require_current_url(self) -> None:
        if self._blocked_status is not None:
            status = self._blocked_status
            raise BrowserBlocked(f"{_ACCESS_REASON[status]} (HTTP {status}).")
        if self._network_blocked or not self._in_scope(self.page.url):
            raise BrowserBlocked("로그인·접근 제한 또는 승인 범위 밖으로 이동이 감지됐습니다.")

    async def _goto(self, url: str, *, recovery: bool = False) -> None:
        if not self._in_scope(url):
            raise BrowserBlocked("요청 주소가 상점 허용 범위를 벗어났습니다.")
        await self._initialize()
        self._active = None
        self._network_blocked = False
        self._blocked_status = None
        for attempt in range(2):
            self._step(recovery=recovery)
            try:
                response = await self.page.goto(
                    url, wait_until="domcontentloaded", timeout=self.action_timeout_ms
                )
                break
            except PlaywrightError as exc:
                if self._network_blocked:
                    self._require_current_url()
                if attempt or self.page.is_closed():
                    raise BrowserError("상점 화면을 열지 못했습니다.") from exc
        if response is not None and response.status in _ACCESS_REASON:
            self._network_blocked = True
            self._blocked_status = response.status
        self._require_current_url()

    async def open_home(self) -> None:
        await self._goto(self.home_url)

    async def open_cart(self) -> None:
        """Use the scoped cart read URL to reconcile a cart mutation."""
        await self._goto(self.cart_url, recovery=self._write_dispatched)

    async def open_product(self, url: str) -> None:
        """Reopen a product URL previously observed on the search surface."""
        if url not in self._observed_products:
            raise BrowserBlocked("검색 화면에서 확인한 상품 주소만 다시 열 수 있습니다.")
        await self._goto(url)

    async def return_to_results(self) -> None:
        if self._results_url is None:
            raise BrowserError("앞서 관찰한 검색 결과를 확인할 수 없습니다.")
        await self._goto(self._results_url)

    async def _capture(self):
        self._require_current_url()
        try:
            handle = await self.page.evaluate_handle(SNAPSHOT, self._dom)
            payload = await handle.get_property("payload")
            data = await payload.json_value()
            await payload.dispose()
        except PlaywrightError as exc:
            self._require_current_url()
            raise StaleObservation("화면이 바뀌어 다시 관찰해야 합니다.") from exc
        if data["view"] == "blocked":
            await handle.dispose()
            raise BrowserBlocked("로그인, CAPTCHA 또는 상점 접근 제한을 확인했습니다.")
        if data["view"] == "unknown":
            await handle.dispose()
            raise BrowserBlocked("상점에서 인식할 수 있는 화면을 확인하지 못했습니다.")
        return handle, data

    async def _read(self) -> dict:
        handle, data = await self._capture()
        await handle.dispose()
        return data

    async def observe(self) -> Observation:
        handle, data = await self._capture()
        await self._clear_handles()
        self._epoch += 1
        self._document = (await handle.get_property("root")).as_element()
        node_list = await handle.get_property("nodes")
        nodes = await node_list.get_properties()
        await node_list.dispose()
        await handle.dispose()
        targets = []
        for index, item in enumerate(data["controls"]):
            node = nodes.get(str(index))
            if node is None:
                continue
            role, url = item["role"], item["href"]
            if (
                item["destination"] not in {"", "_self"}
                or role not in VIEW_ROLES[data["view"]]
                or (role == "add" and not self.can_add)
                or (role == "clear-cart" and not self.can_clear)
                or (url and not self._in_scope(url))
                or (role in {"card", "option"} and not SAFE_KEY.fullmatch(item["key"]))
            ):
                await node.dispose()
                continue
            if role == "card" and url:
                self._observed_products.add(url)
            target_id = f"O{self._epoch}-{index + 1}"
            facts = {
                "tag": item["tag"],
                "accessibility_role": item["aria_role"],
            }
            if role == "quantity":
                facts["value"] = item["value"]
                facts["options"] = item["options"]
            if role == "option":
                facts["selected"] = item["selected"]
            if role == "card":
                facts.update(
                    (key, value)
                    for key in ("name", "price", "shipping", "option", "stock")
                    if (value := _text(item["card_facts"], key))
                )
            targets.append(Target(
                target_id, role, item["key"], item["label"], CONTROL_ACTIONS[role], facts, url,
            ))
            if element := node.as_element():
                self._nodes[target_id] = element
        self._snapshot = data
        self._observation_url = self.page.url
        if data["view"] == "results":
            self._results_url = self._observation_url
        self._active = Observation(
            data["view"], tuple(targets),
            {
                "result_count": sum(target.role == "card" for target in targets),
                "option_count": len(data["offers"]),
                "cart_line_count": len(data["rows"]),
                "cart_empty": data["empty"],
            },
            self._epoch,
        )
        return self._active

    async def _clear_handles(self) -> None:
        for node in (*self._nodes.values(), getattr(self, "_document", None)):
            if node is None:
                continue
            try:
                await node.dispose()
            except PlaywrightError:
                pass
        self._nodes.clear()

    def _offers(self, data: dict) -> tuple[Offer, ...]:
        result = []
        for row in data["offers"]:
            fields, common = row["fields"], row["product_fields"]
            product_id, option_id = row["product_id"], row["option_id"]
            if not (SAFE_KEY.fullmatch(product_id) and SAFE_KEY.fullmatch(option_id)):
                continue
            capacity, unit = _capacity(fields)
            option = _text(fields, "option")
            pack_count, contents = self._pack_evidence(fields, option)
            if capacity is None and contents is not None:
                capacity, unit = contents, "piece"
            result.append(Offer(
                product_id=product_id,
                option_id=option_id,
                title=_text(fields, "name") or _text(common, "name"),
                label=option,
                unit_price=self._price(fields),
                pack_count=pack_count,
                capacity=capacity,
                capacity_unit=unit,
                stock=_stock(_text(fields, "stock")),
                delivery=_text(fields, "shipping"),
                shipping_fee=_shipping_fee(_text(fields, "shipping-price")),
                attributes=_texts(fields, "attributes"),
                category=_text(fields, "category") or _text(common, "category"),
                url=self.page.url,
                selected=row["selected"],
            ))
        return tuple(result)

    def _price(self, fields: dict) -> int | None:
        text = _text(fields, "price")
        if self.mode == "coupang":
            return parse_price(text, current_text=_text(fields, "current-price") or None)
        return _number(text, "원")

    def _pack_evidence(self, fields: dict, option: str) -> tuple[int | None, int | None]:
        pack = _number(_text(fields, "pack"), r"(?:개|팩|매|세트|봉|병)(?:\s*구성)?")
        if self.mode != "coupang":
            return pack, None
        parsed = parse_pack(option, (*_texts(fields, "specs"), *_texts(fields, "attributes")))
        if pack is not None and parsed.packages_per_order not in {None, pack}:
            return None, None
        return pack or parsed.packages_per_order, parsed.total_contents

    async def details(self) -> tuple[Offer, ...]:
        data = await self._read()
        if data["view"] != "detail":
            raise BrowserError("상품 상세 화면을 확인할 수 없습니다.")
        return self._offers(data)

    async def read_cart(self) -> tuple[CartLine, ...]:
        return self._cart_lines(await self._read())

    def _cart_lines(self, data: dict) -> tuple[CartLine, ...]:
        if data["view"] != "cart" or bool(data["rows"]) == data["empty"]:
            raise BrowserError("장바구니 상품과 빈 장바구니 여부를 확인할 수 없습니다.")
        lines: list[CartLine] = []
        for row in data["rows"]:
            fields = row["fields"]
            p_id, o_id = row["product_id"], row["option_id"]
            price = self._price(fields)
            quantity = _text(fields, "quantity")
            count = parse_quantity(quantity) if self.mode == "coupang" else _number(quantity, "개|팩|세트")
            title, label = _text(fields, "name"), _text(fields, "option")
            if not (
                SAFE_KEY.fullmatch(p_id) and SAFE_KEY.fullmatch(o_id)
                and price is not None and count and title and label
            ):
                raise BrowserError("장바구니에서 상품, 옵션, 가격 또는 수량을 확인할 수 없습니다.")
            lines.append(CartLine(
                p_id, o_id, title, label, price, count,
                self._pack_evidence(fields, label)[0],
                _text(fields, "shipping"),
                _shipping_fee(_text(fields, "shipping-price")),
            ))
        return tuple(lines)

    async def execute(
        self,
        observation: Observation,
        action: str,
        target_id: str | None = None,
        value: str | int | None = None,
        *,
        expected_offer: Offer | None = None,
        expected_quantity: int | None = None,
        expected_cart: tuple[CartLine, ...] | None = None,
    ) -> None:
        if observation is not self._active or self.page.url != self._observation_url:
            raise StaleObservation("이 결정은 현재 화면의 관찰 결과에 속하지 않습니다.")
        current = await self._read()
        try:
            same_document = self._document and await self._document.evaluate(
                "(el) => el === document.documentElement"
            )
        except PlaywrightError:
            same_document = False
        self._active = None
        if not same_document or current != self._snapshot:
            raise StaleObservation("화면 내용이 바뀌어 다시 관찰해야 합니다.")
        if action == "BLOCKED":
            raise BrowserBlocked("Zoro-1이 현재 화면에서 진행할 수 없다고 판단했습니다.")
        if action == "DONE":
            self._step()
            return
        if action == "SCROLL":
            self._step()
            await self.page.mouse.wheel(0, -420 if value == "up" else 420)
            return
        if action == "WAIT":
            self._step()
            await asyncio.sleep(0.25)
            return
        target = next((item for item in observation.targets if item.id == target_id), None)
        if not target or target.action != action:
            raise StaleObservation("관찰 ID와 선택한 동작이 일치하지 않습니다.")
        self._step(before_add=target.role in {"add", "clear-cart"})
        node = self._nodes.get(target.id)
        try:
            active = node and await node.evaluate("""(el) => el.isConnected &&
                !el.closest('[hidden], [inert], [aria-hidden=true], [aria-disabled=true]')""")
            if not active or not await node.is_visible() or not await node.is_enabled():
                raise StaleObservation("선택한 요소가 교체되었거나 사용할 수 없습니다.")
        except PlaywrightError as exc:
            raise StaleObservation("선택한 요소가 현재 화면에 없습니다.") from exc

        if target.role == "add":
            self._require_add_evidence(current, expected_offer, expected_quantity)
            await self._add(node, expected_offer, expected_quantity)
            return
        if target.role == "clear-cart":
            await self._clear(node, current, expected_cart)
            return
        try:
            if action == "TYPE_TEXT":
                if (
                    not isinstance(value, str)
                    or value.strip() != value
                    or not 1 <= len(value) <= 100
                    or any(not character.isprintable() for character in value)
                    or re.search(r"(?:https?://|javascript:|<|>)", value, re.I)
                ):
                    raise BrowserError("검색어는 검증된 한 줄의 일반 문자열이어야 합니다.")
                await node.fill(value, timeout=self.action_timeout_ms)
            elif action == "SET_QUANTITY":
                if type(value) is not int or not 1 <= value <= 1_000:
                    raise BrowserError("주문 수량을 확인할 수 없습니다.")
                if target.facts["tag"] == "select":
                    if str(value) not in target.facts["options"]:
                        raise BrowserError("화면에서 선택한 주문 수량을 제공하지 않습니다.")
                    await node.select_option(str(value), timeout=self.action_timeout_ms)
                else:
                    await node.fill(str(value), timeout=self.action_timeout_ms)
                    await node.press("Tab", timeout=self.action_timeout_ms)
            else:
                await node.click(timeout=self.action_timeout_ms)
                await self.page.wait_for_load_state("domcontentloaded", timeout=self.action_timeout_ms)
                self._require_current_url()
        except PlaywrightError as exc:
            self._require_current_url()
            raise StaleObservation("화면 조작 결과를 다시 관찰해야 합니다.") from exc

    def _require_add_evidence(
        self,
        data: dict,
        expected: Offer | None,
        quantity: int | None,
    ) -> None:
        selected = [offer for offer in self._offers(data) if offer.selected]
        quantities = data["quantities"]
        if (
            data["view"] != "detail"
            or expected is None
            or type(quantity) is not int
            or quantity < 1
            or len(selected) != 1
            or asdict(selected[0]) != {**asdict(expected), "selected": True}
            or selected[0].stock is None
            or selected[0].stock < quantity
            or quantities != [str(quantity)]
        ):
            raise StaleObservation("추가 직전 상품·옵션·수량·가격을 다시 확인해야 합니다.")

    async def _add(self, node: ElementHandle, offer: Offer, quantity: int) -> None:
        if self._add_path is None or self._add_fields is None:
            raise BrowserBlocked("승인된 쿠팡 비회원 장바구니 담기 요청 형식을 확인할 수 없습니다.")
        expected = {
            self._add_fields["product"]: offer.product_id,
            self._add_fields["option"]: offer.option_id,
            self._add_fields["quantity"]: str(quantity),
        }
        await self._submit_write(node, self._add_path, expected)

    async def _clear(
        self, node: ElementHandle, data: dict, expected: tuple[CartLine, ...] | None,
    ) -> None:
        if self._clear_path is None or self._clear_items_field is None:
            raise BrowserBlocked("승인 범위에서 장바구니 비우기를 확인할 수 없습니다.")
        lines = self._cart_lines(data)
        identities = {(line.product_id, line.option_id) for line in lines}
        if (
            not expected
            or len(identities) != len(lines)
            or len(expected) != len(lines)
            or frozenset(expected) != frozenset(lines)
        ):
            raise StaleObservation("장바구니가 바뀌어 비우기 대상을 확인할 수 없습니다.")
        if self._dom.get("clear_row_id_attr"):
            payload = [row["clear_id"] for row in data["rows"]]
            if len(payload) != len(set(payload)) or not all(SAFE_KEY.fullmatch(key) for key in payload):
                raise BrowserBlocked("장바구니 비우기 요청에 사용할 행 ID를 확인할 수 없습니다.")
        else:
            payload = [[line.product_id, line.option_id, line.quantity] for line in lines]
        await self._submit_write(
            node, self._clear_path, {self._clear_items_field: payload}, clearing=True,
        )

    async def _submit_write(
        self,
        node: ElementHandle,
        path: str,
        expected: dict[str, str | list],
        *,
        clearing: bool = False,
    ) -> None:
        self._require_current_url()
        form = await node.evaluate(r"""(el) => ({
            action: el.form ? (el.getAttribute("formaction") ? el.formAction : el.form.action) : null,
            method: el.form ? (el.getAttribute("formmethod") || el.form.method) : null,
            target: el.getAttribute("formtarget") || el.form?.target || "",
        })""")
        if (
            form["target"] not in {"", "_self"}
            or (form["action"] and (
                not self._is_write_url(form["action"], path) or form["method"].lower() != "post"
            ))
        ):
            raise BrowserBlocked("장바구니 변경 요청이 승인 범위와 다릅니다.")
        ambiguous = AmbiguousClear if clearing else AmbiguousAdd
        self._write_dispatched = False
        self._write_blocked = False
        self._expected_write = expected
        self._pending_path = path
        try:
            async with self.page.expect_response(
                lambda response: response.request.method == "POST" and self._is_write_url(response.url, path),
                timeout=self.action_timeout_ms,
            ) as response_info:
                await node.click(timeout=self.action_timeout_ms)
            response = await response_info.value
            if response.status in _ACCESS_REASON:
                self._network_blocked = True
                self._blocked_status = response.status
                self._require_current_url()
            if response.status >= 400:
                raise ambiguous("상점 응답을 장바구니에서 다시 확인해야 합니다.")
            await self.page.wait_for_load_state("domcontentloaded", timeout=self.action_timeout_ms)
            if self._is_write_url(self.page.url, path) and not self._network_blocked:
                raise ambiguous("변경 반영 여부를 장바구니에서 먼저 확인해야 합니다.")
            self._require_current_url()
        except PlaywrightError as exc:
            if self._write_blocked:
                message = "장바구니 비우기 요청의 삭제 대상이 확인 결과와 다릅니다." if clearing else (
                    "장바구니 요청의 상품·옵션·수량이 확인 결과와 다릅니다."
                )
                raise BrowserBlocked(message) from exc
            if self._network_blocked:
                self._require_current_url()
            raise ambiguous("변경 반영 여부를 장바구니에서 먼저 확인해야 합니다.") from exc
        finally:
            self._pending_path = None
            self._expected_write = None
