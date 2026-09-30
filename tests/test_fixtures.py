"""실제 익명화 fixture로 확인한 원천 계약(2026-09-29 수집). 응답이 바뀌면 여기서 먼저 깨진다."""
import json
import os
import re

import pytest

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures")
FAM = {"apt": "LTTOT_TOP_AMOUNT", "remndr": "LTTOT_TOP_AMOUNT", "urbty_ofctl": "SUPLY_AMOUNT"}


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("fam", FAM)
def test_natural_keys_present_and_unique(fam):
    rows = load(f"{fam}_detail_cases.json")["data"]
    assert rows
    keys = [(r["HOUSE_MANAGE_NO"], r["PBLANC_NO"]) for r in rows]
    assert all(all(k) for k in keys) and len(set(keys)) == len(keys)


@pytest.mark.parametrize("fam,price", FAM.items())
def test_model_key_unique_per_announcement_and_price_is_manwon_string(fam, price):
    models = load(f"{fam}_model_cases.json")["data"]
    assert models
    keys = [(m["HOUSE_MANAGE_NO"], m["PBLANC_NO"], m["MODEL_NO"]) for m in models]
    assert len(set(keys)) == len(keys)
    assert all(isinstance(m[price], str) and re.fullmatch(r"\d+", m[price]) for m in models)


def test_area_fields_differ_by_family():
    # APT/잔여세대 주택형은 공급면적(SUPLY_AR)만 제공. 전용면적 필드 없음 -> 설계 차이로 보고.
    for fam in ("apt", "remndr"):
        m = load(f"{fam}_model_cases.json")["data"][0]
        assert "SUPLY_AR" in m and "EXCLUSE_AR" not in m
    assert "EXCLUSE_AR" in load("urbty_ofctl_model_cases.json")["data"][0]


def test_remndr_subtype_codes_present():
    codes = {r["HOUSE_SECD"] for r in load("remndr_detail_cases.json")["data"]}
    assert codes and codes <= {"04", "06"} and "06" in codes


def test_empty_page_is_http200_with_zero_current_count():
    r = load("apt_detail_empty_page.json")
    assert r["http"] == 200 and r["body"]["currentCount"] == 0 and r["body"]["totalCount"] > 0
    assert r["body"]["data"] == []


def test_error_body_is_json_with_negative_code():
    r = load("error_unregistered_key.json")
    assert r["http"] == 401 and r["body"]["code"] < 0


OPS = {"apt": ("getAPTLttotPblancDetail", "getAPTLttotPblancMdl"),
       "remndr": ("getRemndrLttotPblancDetail", "getRemndrLttotPblancMdl"),
       "urbty_ofctl": ("getUrbtyOfctlLttotPblancDetail", "getUrbtyOfctlLttotPblancMdl")}
MASKED = {"BSNS_MBY_NM": "사업주체(익명)", "CNSTRCT_ENTRPS_NM": "시공사(익명)", "NSPRC_NM": "신문사(익명)",
          "MDHS_TELNO": "00000000", "HMPG_ADRES": "https://example.invalid"}


@pytest.mark.parametrize("fam", OPS)
def test_all_fields_known_and_pii_fields_masked(fam):
    known = load("../tools/known_fields.json")
    for kind, op in zip(("detail", "model"), OPS[fam]):
        for rec in load(f"{fam}_{kind}_cases.json")["data"]:
            assert set(rec) <= set(known[op])
            for k, v in MASKED.items():
                assert rec.get(k) in (None, "", v), f"{fam} {k} 익명화 누락"


def test_xml_pii_fields_masked():
    import xml.etree.ElementTree as ET
    root = ET.parse(os.path.join(FIX, "apt_detail_sample.xml")).getroot()
    cols = {c.get("name"): (c.text or "") for c in root.iter("col")}
    assert cols
    for k, v in MASKED.items():
        assert cols.get(k, v) in ("", v)


def test_fixtures_have_no_identifying_strings():
    blob = "".join(open(os.path.join(FIX, f), encoding="utf-8").read() for f in os.listdir(FIX) if f.endswith((".json", ".xml")))
    assert "serviceKey" not in blob and "api.odcloud" not in blob
    assert not re.search(r'"MDHS_TELNO": "(?!00000000)\d', blob)
