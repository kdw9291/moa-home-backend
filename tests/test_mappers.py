import json
import os
from datetime import date
from decimal import Decimal

import pytest

from moahome.mappers import FAMILIES, MappingError, map_announcement

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)["data"]


def cases(fam):
    details, models = load(f"{fam}_detail_cases.json"), load(f"{fam}_model_cases.json")
    for d in details:
        key = (d["HOUSE_MANAGE_NO"], d["PBLANC_NO"])
        yield d, [m for m in models if (m["HOUSE_MANAGE_NO"], m["PBLANC_NO"]) == key]


@pytest.mark.parametrize("fam", FAMILIES)
def test_every_fixture_case_maps(fam):
    n = 0
    for d, ms in cases(fam):
        m = map_announcement(fam, d, ms)
        assert m.key == (fam, d["HOUSE_MANAGE_NO"], d["PBLANC_NO"]) and len(m.housing_types) == len(ms)
        for e in m.events:
            assert e["starts_on"] is None or e["ends_on"] is None or e["starts_on"] <= e["ends_on"]
        n += 1
    assert n >= 2


def test_apt_price_manwon_to_won_once_and_area_columns():
    d, ms = next(cases("apt"))
    t = map_announcement("apt", d, ms).housing_types[0]
    raw = next(x for x in ms if x["MODEL_NO"] == t["source_model_key"])
    assert t["price_max_krw"] == int(raw["LTTOT_TOP_AMOUNT"]) * 10000
    assert t["price_raw"] == raw["LTTOT_TOP_AMOUNT"] and t["price_source_unit"] == "MANWON"
    assert t["supply_area_sqm"] == Decimal(raw["SUPLY_AR"]).quantize(Decimal("0.0001"))
    assert t["exclusive_area_sqm"] is None          # apt에는 전용면적 필드가 없다
    assert t["house_ty"] == raw["HOUSE_TY"].strip()  # 표시용, 면적 근거 아님


def test_urbty_uses_exclusive_area_and_tp():
    d, ms = next(cases("urbty_ofctl"))
    t = map_announcement("urbty_ofctl", d, ms).housing_types[0]
    raw = next(x for x in ms if x["MODEL_NO"] == t["source_model_key"])
    assert t["exclusive_area_sqm"] == Decimal(raw["EXCLUSE_AR"]).quantize(Decimal("0.0001"))
    assert t["supply_area_sqm"] is None and t["house_ty"] == raw["TP"].strip()
    assert t["price_max_krw"] == int(raw["SUPLY_AMOUNT"]) * 10000


def test_summary_prices_are_min_max_of_known_maxima():
    for d, ms in cases("apt"):
        m = map_announcement("apt", d, ms)
        known = [t["price_max_krw"] for t in m.housing_types if t["price_max_krw"] is not None]
        assert m.fields["min_price_krw"] == min(known) and m.fields["max_price_krw"] == max(known)


def test_remndr_subtype_from_source_code_only():
    seen = {map_announcement("remndr", d, ms).fields["source_subtype"] for d, ms in cases("remndr")}
    assert seen <= {"04", "06"} and "06" in seen
    d, ms = next(cases("apt"))
    assert map_announcement("apt", d, ms).fields["source_subtype"] is None


def test_apt_special_supply_categories_only_when_field_present():
    d, ms = next(cases("apt"))
    t = map_announcement("apt", d, ms).housing_types[0]
    assert {s["category_code"] for s in t["special_supply"]} <= {
        "mnych", "nwwds", "lfe_frst", "old_parnts_suport", "instt_recomend", "etc",
        "transr_instt_enfsn", "ygmn", "nwbb"}
    d, ms = next(cases("remndr"))
    assert all(t["special_supply"] == [] for t in map_announcement("remndr", d, ms).housing_types)


def test_missing_dates_make_no_events_and_single_date_events_are_points():
    d, ms = next(cases("remndr"))
    d = {**d, "GNRL_RCEPT_BGNDE": None, "GNRL_RCEPT_ENDDE": None,
         "SPSPLY_RCEPT_BGNDE": None, "SPSPLY_RCEPT_ENDDE": None}
    m = map_announcement("remndr", d, ms)
    codes = {e["source_event_code"] for e in m.events}
    assert "gnrl_rcept" not in codes and "spsply_rcept" not in codes
    p = next(e for e in m.events if e["source_event_code"] == "przwner_presnatn")
    assert p["starts_on"] == p["ends_on"] == date.fromisoformat(d["PRZWNER_PRESNATN_DE"])


def base():
    d, ms = next(cases("apt"))
    return dict(d), [dict(m) for m in ms]


@pytest.mark.parametrize("mutate", [
    lambda d, ms: d.update(HOUSE_MANAGE_NO=None),
    lambda d, ms: d.update(HOUSE_NM=" "),
    lambda d, ms: d.update(RCEPT_BGNDE="2026-10-05", RCEPT_ENDDE="2026-10-01"),      # 날짜 역전
    lambda d, ms: d.update(RCEPT_BGNDE="2026/10/01"),                                # 형식 오류
    lambda d, ms: d.update(TOT_SUPLY_HSHLDCO="abc"),
    lambda d, ms: ms[0].update(MODEL_NO=None),
    lambda d, ms: ms[0].update(HOUSE_TY=""),
    lambda d, ms: ms.append(dict(ms[0])),                                            # MODEL_NO 중복
    lambda d, ms: ms[0].update(PBLANC_NO="other"),                                   # 다른 공고 혼입
])
def test_contract_violations_raise_mapping_error(mutate):
    d, ms = base()
    mutate(d, ms)
    with pytest.raises(MappingError):
        map_announcement("apt", d, ms)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_blank_price_is_null_unknown_never_zero(raw):
    d, ms = base()
    ms[0]["LTTOT_TOP_AMOUNT"] = raw
    t = map_announcement("apt", d, ms).housing_types[0]
    assert t["price_max_krw"] is None and t["price_source_unit"] == "UNKNOWN" and t["price_raw"] is None


@pytest.mark.parametrize("raw", ["-", "n/a", "abc", "1e999999", "-5"])
def test_unparsable_nonblank_price_is_anomaly_and_skips_announcement(raw):
    # 미공개 표현이 확인되기 전까지 비어 있지 않은 해석 불가 값은 이상치: 공고를 건너뛰어 기존 가격을 보존
    d, ms = base()
    ms[0]["LTTOT_TOP_AMOUNT"] = raw
    with pytest.raises(MappingError):
        map_announcement("apt", d, ms)


def test_urbty_subtype_keeps_search_house_secd():
    d, ms = next(cases("urbty_ofctl"))
    assert map_announcement("urbty_ofctl", d, ms).fields["source_subtype"] == d["SEARCH_HOUSE_SECD"]


def test_invalid_area_is_null_with_warning():
    d, ms = base()
    ms[0]["SUPLY_AR"] = "0"
    m = map_announcement("apt", d, ms)
    assert m.housing_types[0]["supply_area_sqm"] is None and "area_invalid:SUPLY_AR" in m.warnings


def test_provenance_invariant_holds_for_all_mapped_types():
    for fam in FAMILIES:
        for d, ms in cases(fam):
            for t in map_announcement(fam, d, ms).housing_types:
                if t["price_max_krw"] is not None:
                    assert t["price_raw"] and t["price_source_unit"] == "MANWON"
                else:
                    assert t["price_source_unit"] == "UNKNOWN"
