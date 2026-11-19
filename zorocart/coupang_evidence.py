"""Parse facts from visible Coupang price, option, and quantity controls."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
import unicodedata


_COUNT = r"(?:0|[1-9][0-9]{0,2}(?:,[0-9]{3})+|[1-9][0-9]*)"
_AMOUNT = rf"(?:₩\s*)?({_COUNT})\s*(?:원)?"
_PRICE = re.compile(_AMOUNT)
_CURRENT_LABEL = re.compile(rf"^\s*(?:즉시할인가|할인가|(?:쿠팡\s*)?판매가)\s*[:：]?\s*{_AMOUNT}[ \t]*$", re.MULTILINE)
_CONDITIONAL = re.compile(r"와우\s*(?:회원|혜택|할인)|멤버십|쿠폰\s*(?:적용|할인|다운로드)|카드\s*할인")
_PRICE_RANGE = re.compile(r"[~∼〜]|원\s*부터")
_QUANTITY = re.compile(rf"({_COUNT})\s*(?:개|팩|세트)?")
_COUNT_WITH_UNIT = re.compile(rf"(?<![0-9,])({_COUNT})\s*(개입|매입|장입|매|장|개|팩|세트|묶음|봉지|봉)(?![가-힣0-9])")
_SPEC = re.compile(r"(?P<kind>개당\s*수량|팩당\s*수량|총\s*수량)\s*[:：]\s*(?P<value>\S[^\n]*)")
_MAX_NUMBER = 1_000_000_000
_CONTENTS_MARKERS = {"개입", "매입", "장입"}
_PACK_MARKERS = {"팩", "세트", "묶음", "봉", "봉지"}


@dataclass(frozen=True)
class PackEvidence:
    """The contents of one retail option, independent of the order control."""

    packages_per_order: int | None = None
    contents_per_package: int | None = None
    total_contents: int | None = None


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _numeric(value: str) -> int | None:
    digits = value.replace(",", "")
    if len(digits) > 10:
        return None
    number = int(digits)
    return number if 0 < number <= _MAX_NUMBER else None


def parse_price(text: str | None, *, current_text: str | None = None) -> int | None:
    """Return a single confirmed unit price from a narrow price scope.

    Pass ``current_text`` only when it comes from the visible current-price
    element. Never guess which of two unlabeled amounts is payable.
    """
    value = _normalized(current_text if current_text is not None else text or "")
    if _CONDITIONAL.search(value) or _PRICE_RANGE.search(value):
        return None
    match = _PRICE.fullmatch(value)
    if match:
        return _numeric(match[1])
    labelled = list(_CURRENT_LABEL.finditer(value))
    if len(labelled) == 1:
        return _numeric(labelled[0][1])
    return None


def parse_quantity(text: str | int | None) -> int | None:
    """Parse a quantity read directly from the current order input/control."""
    if type(text) is int:
        return text if 0 < text <= _MAX_NUMBER else None
    if not isinstance(text, str):
        return None
    match = _QUANTITY.fullmatch(_normalized(text))
    return _numeric(match[1]) if match else None


def _spec_counts(specs: Sequence[str] | Mapping[str, str]) -> tuple[set[int], set[int], bool]:
    per_pack: set[int] = set()
    totals: set[int] = set()
    lines = (f"{key}: {value}" for key, value in specs.items()) if isinstance(specs, Mapping) else specs
    for line in lines:
        for part in _normalized(line).splitlines():
            match = _SPEC.fullmatch(part.strip())
            if not match:
                continue
            count = _COUNT_WITH_UNIT.fullmatch(match["value"].strip())
            value = _numeric(count[1]) if count and count[2] not in _PACK_MARKERS else None
            if value is None:
                return per_pack, totals, False
            target = totals if match["kind"].startswith("총") else per_pack
            target.add(value)
    return per_pack, totals, True


def parse_pack(option: str | None, specs: Sequence[str] | Mapping[str, str] = ()) -> PackEvidence:
    """Read one selected retail option's outer packs and inner piece counts."""
    text = _normalized(option or "")
    per_pack, totals, valid = _spec_counts(specs)
    if not valid:
        return PackEvidence()

    packages, pieces, sheets = set(), set(), set()
    for match in _COUNT_WITH_UNIT.finditer(text):
        count = _numeric(match[1])
        if count is None:
            return PackEvidence()
        unit = match[2]
        if unit in _CONTENTS_MARKERS:
            per_pack.add(count)
        elif unit in _PACK_MARKERS:
            packages.add(count)
        elif unit == "개":
            pieces.add(count)
        else:
            sheets.add(count)

    if any(len(group) > 1 for group in (per_pack, totals, sheets)):
        return PackEvidence()
    per = next(iter(per_pack), None)
    total = next(iter(totals), None)

    # A separate plain count may give either the retail-pack count or the total
    # pieces. Only treat it as a pack count when the other evidence resolves it.
    if per is not None:
        if total is not None:
            if total % per != 0:
                return PackEvidence()
            packages.add(total // per)
            pieces.discard(total)
        if sheets and not sheets <= {per, total}:
            return PackEvidence()
        if len(pieces) == 1:
            outer = next(iter(pieces))
            if outer != per or total is not None or outer == 1:
                packages.add(outer)
        elif pieces:
            return PackEvidence()

    if len(packages) > 1:
        return PackEvidence()
    packs = next(iter(packages), None)
    if packs is not None and per is not None:
        computed = packs * per
        if computed > _MAX_NUMBER or (total is not None and total != computed):
            return PackEvidence()
        total = computed
    if not packages and total is None and len(pieces | sheets) == 1:
        total = next(iter(pieces | sheets))
    return PackEvidence(packs, per, total)
