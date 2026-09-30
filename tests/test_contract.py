from datetime import datetime, timezone, date
import pytest

from moahome.contract import (
    to_krw, kst_date, d_day, housing_type_matches, announcement_matches,
    dedupe_by_key, announcement_key,
)


@pytest.mark.parametrize("raw,unit,expected", [
    ("59000", "MANWON", 590_000_000),
    ("59,000", "MANWON", 590_000_000),
    ("590000000", "KRW", 590_000_000),
    ("0", "KRW", 0),           # 실제 0은 그대로 (미공개와 구분)
    ("", "MANWON", None),
    ("-", "KRW", None),
    (None, "KRW", None),
    ("abc", "KRW", None),
    ("-100", "KRW", None),
    ("59000", "UNKNOWN", None),  # 단위 미확인이면 환산하지 않음
    ("1.5", "KRW", None),        # 소수 원 금액은 신뢰하지 않음
    ("1.5", "MANWON", 15000),
    ("1e999999", "MANWON", None),
    ("1e999999", "KRW", None),
    ("1,2", "KRW", None),
    ("1,23", "KRW", None),
    ("1,234", "KRW", 1234),
    ("NaN", "KRW", None),
    ("Infinity", "MANWON", None),
    ("9223372036854775807", "KRW", 9223372036854775807),
    ("9223372036854775808", "KRW", None),   # BIGINT 초과
    ("922337203685478", "MANWON", None),    # 환산 후 BIGINT 초과
])
def test_to_krw(raw, unit, expected):
    assert to_krw(raw, unit) == expected


def test_to_krw_rejects_unknown_unit_name():
    with pytest.raises(ValueError):
        to_krw("1", "만원")


def test_kst_midnight_boundary():
    # 2026-09-29 15:00 UTC == 2026-09-30 00:00 KST
    assert kst_date(datetime(2026, 9, 29, 14, 59, 59, tzinfo=timezone.utc)) == date(2026, 9, 29)
    assert kst_date(datetime(2026, 9, 29, 15, 0, 0, tzinfo=timezone.utc)) == date(2026, 9, 30)
    with pytest.raises(ValueError):
        kst_date(datetime(2026, 9, 29, 15, 0, 0))


def test_d_day():
    assert d_day(date(2026, 9, 29), date(2026, 9, 29)) == 0
    assert d_day(date(2026, 9, 29), date(2026, 10, 2)) == 3
    assert d_day(date(2026, 9, 29), date(2026, 9, 28)) == -1


def test_same_row_match_does_not_mix_types():
    # 59형은 예산 이하지만 면적 미달(확정 불일치), 84형은 면적 충족이나 최고가 초과(미확인)
    # -> 일치 행 없음. 한 행의 조건이 다른 행 값으로 채워지지 않는다.
    rows = [
        {"price_max_krw": 500_000_000, "exclusive_area_sqm": "59.0"},
        {"price_max_krw": 700_000_000, "exclusive_area_sqm": "84.0"},
    ]
    assert announcement_matches(rows, budget_krw=600_000_000, min_area=80) is None
    assert housing_type_matches(rows[0], budget_krw=600_000_000, min_area=80) is False


def test_same_row_match_positive_and_unknown():
    rows = [{"price_max_krw": 500_000_000, "exclusive_area_sqm": "84.9"}]
    assert announcement_matches(rows, budget_krw=600_000_000, min_area=80) is True
    unknown = [{"price_max_krw": None, "exclusive_area_sqm": "84.9"}]
    assert announcement_matches(unknown, budget_krw=600_000_000) is None  # 0원으로 취급 금지
    # 확인된 불일치는 미확인보다 우선
    assert housing_type_matches({"price_max_krw": None, "exclusive_area_sqm": "50"}, budget_krw=1, min_area=80) is False


def test_area_boundary_is_canonical_sqm():
    row = {"price_max_krw": 1, "exclusive_area_sqm": "59.0000"}
    assert housing_type_matches(row, min_area=59) is True
    assert housing_type_matches(row, max_area=58.99) is False


@pytest.mark.parametrize("args", [("apt", None, "1"), ("apt", "H1", None), ("apt", " ", "1"), (None, "H1", "1")])
def test_missing_identifier_rejected(args):
    with pytest.raises(ValueError):
        announcement_key(*args)


def test_dedupe_and_cross_family_key():
    a = announcement_key("apt", "2026000001", 1)
    b = announcement_key("remndr", "2026000001", 1)
    assert a != b
    recs = [{"k": a, "v": 1}, {"k": a, "v": 2}, {"k": b, "v": 3}]
    out, dup = dedupe_by_key(recs, lambda r: r["k"])
    assert dup == 1 and {r["v"] for r in out} == {2, 3}


def test_max_price_over_budget_is_unknown_not_mismatch():
    # 원천이 '분양최고금액'이므로 최고가가 예산 초과여도 더 싼 세대가 있을 수 있음
    row = {"price_max_krw": 700_000_000, "exclusive_area_sqm": "84"}
    assert housing_type_matches(row, budget_krw=600_000_000) is None
    assert housing_type_matches(row, budget_krw=800_000_000) is True
    assert housing_type_matches(row, budget_krw=600_000_000, min_area=100) is False  # 확정 불일치 우선


def test_missing_exclusive_area_is_unknown_never_supply_area():
    # apt/remndr: 전용면적 없음, 공급면적만 있음 -> 면적 조건은 미확인
    row = {"price_max_krw": 500_000_000, "exclusive_area_sqm": None, "supply_area_sqm": "84.0121"}
    assert housing_type_matches(row, min_area=59, max_area=85) is None
    assert housing_type_matches(row, budget_krw=600_000_000) is True   # 면적 조건 없으면 가격만 판정


@pytest.mark.parametrize("bad", ["NaN", "abc", "", "Infinity"])
def test_malformed_area_is_unknown_not_crash(bad):
    assert housing_type_matches({"price_max_krw": 1, "exclusive_area_sqm": bad}, min_area=59) is None


def test_unknown_price_and_bad_area_stay_unknown():
    assert housing_type_matches({"price_max_krw": None, "exclusive_area_sqm": "NaN"}, budget_krw=1, min_area=1) is None
