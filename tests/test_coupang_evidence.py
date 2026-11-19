import pytest

from zorocart.coupang_evidence import PackEvidence, parse_pack, parse_price, parse_quantity


def test_current_price_is_read_from_its_own_visible_field():
    price_box = "40%\n12,500\n7,500원"
    assert parse_price(price_box, current_text="7,500원") == 7500
    assert parse_price(price_box) is None
    assert parse_price("정가 12,500원\n쿠팡판매가 7,500원") == 7500
    assert parse_price("와우 회원 할인 7,500원") is None
    assert parse_price("판매가 7,500원부터") is None
    assert parse_price("판매가 7,500원\n~ 12,500원") is None
    assert parse_price("12,500원", current_text="판매가 확인 필요") is None


@pytest.mark.parametrize("value", ["1개/110개입", "110개입 × 399g × 1개"])
def test_wipes_option_keeps_pack_count_distinct_from_inner_wipe_count(value):
    assert parse_pack(value) == PackEvidence(1, 110, 110)


def test_bag_pack_is_verified_by_matching_total_and_per_pack_piece_counts():
    assert parse_pack("30L/100개", {
        "개당 수량": "100매입", "총 수량": "100개",
    }) == PackEvidence(1, 100, 100)
    assert parse_pack("30L/100개").packages_per_order is None
    assert parse_pack("30L, 100매입 × 1개", ["총 수량: 100매"]) == PackEvidence(1, 100, 100)


def test_conflicting_packs_or_sheet_counts_provide_no_pack_evidence():
    assert parse_pack("2개/110개입", ["총 수량: 110개"]) == PackEvidence()
    assert parse_pack("30L/100개", ["개당 수량: 90매입", "총 수량: 100매"]) == PackEvidence()
    assert parse_pack("1팩/100매입", ["개당 수량: 50매입"]) == PackEvidence()


def test_quantity_parser_refuses_to_pull_a_count_from_labels_or_option_text():
    assert parse_quantity("１") == 1
    assert parse_quantity("2개") == 2
    assert parse_quantity("1개/110개입") is None
    assert parse_quantity("개당 수량: 100매입") is None
    assert parse_quantity(True) is None
