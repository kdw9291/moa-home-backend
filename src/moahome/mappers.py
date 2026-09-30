"""원천 응답 -> 내부 정규화 구조. docs/API_SPEC.md 3절의 '확인된 필드'만 사용한다.

원칙: 확인 안 된 값은 NULL, 계약 위반(필수 키 누락·중복 키·날짜 역전·다른 공고의 주택형 혼입)은
MappingError로 해당 공고를 건너뛴다(기존 데이터 보존). 면적은 HOUSE_TY/TP 텍스트에서 추정하지 않는다.
이벤트 코드는 원천 필드 접두어에서 그대로 따온 임시 이름이며 의미를 주장하지 않는다(설계 검증 대기).
"""
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from .contract import to_krw

FAMILIES = ("apt", "remndr", "urbty_ofctl")
OPS = {
    "apt": ("getAPTLttotPblancDetail", "getAPTLttotPblancMdl"),
    "remndr": ("getRemndrLttotPblancDetail", "getRemndrLttotPblancMdl"),
    "urbty_ofctl": ("getUrbtyOfctlLttotPblancDetail", "getUrbtyOfctlLttotPblancMdl"),
}

# (source_event_code, scope_code, 시작 필드, 종료 필드) -- 코드는 필드명에서 파생한 임시 이름
_EVENTS = {
    "apt": [
        ("rcept", "all", "RCEPT_BGNDE", "RCEPT_ENDDE"),
        ("spsply_rcept", "all", "SPSPLY_RCEPT_BGNDE", "SPSPLY_RCEPT_ENDDE"),
        *[(f"gnrl_rnk{n}", scope, f"GNRL_RNK{n}_{fld}_RCPTDE", f"GNRL_RNK{n}_{fld}_ENDDE")
          for n in (1, 2) for scope, fld in (("crsparea", "CRSPAREA"), ("etc_gg", "ETC_GG"), ("etc_area", "ETC_AREA"))],
        ("przwner_presnatn", "all", "PRZWNER_PRESNATN_DE", None),
        ("cntrct_cncls", "all", "CNTRCT_CNCLS_BGNDE", "CNTRCT_CNCLS_ENDDE"),
    ],
    "remndr": [
        ("subscrpt_rcept", "all", "SUBSCRPT_RCEPT_BGNDE", "SUBSCRPT_RCEPT_ENDDE"),
        ("gnrl_rcept", "all", "GNRL_RCEPT_BGNDE", "GNRL_RCEPT_ENDDE"),
        ("spsply_rcept", "all", "SPSPLY_RCEPT_BGNDE", "SPSPLY_RCEPT_ENDDE"),
        ("przwner_presnatn", "all", "PRZWNER_PRESNATN_DE", None),
        ("cntrct_cncls", "all", "CNTRCT_CNCLS_BGNDE", "CNTRCT_CNCLS_ENDDE"),
    ],
    "urbty_ofctl": [
        ("subscrpt_rcept", "all", "SUBSCRPT_RCEPT_BGNDE", "SUBSCRPT_RCEPT_ENDDE"),
        ("przwner_presnatn", "all", "PRZWNER_PRESNATN_DE", None),
        ("cntrct_cncls", "all", "CNTRCT_CNCLS_BGNDE", "CNTRCT_CNCLS_ENDDE"),
    ],
}

# apt 주택형의 특별공급 범주별 필드(확인됨) -> category_code
_SPECIAL_CATEGORIES = {
    "MNYCH_HSHLDCO": "mnych", "NWWDS_HSHLDCO": "nwwds", "LFE_FRST_HSHLDCO": "lfe_frst",
    "OLD_PARNTS_SUPORT_HSHLDCO": "old_parnts_suport", "INSTT_RECOMEND_HSHLDCO": "instt_recomend",
    "ETC_HSHLDCO": "etc", "TRANSR_INSTT_ENFSN_HSHLDCO": "transr_instt_enfsn",
    "YGMN_HSHLDCO": "ygmn", "NWBB_HSHLDCO": "nwbb",
}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class MappingError(Exception):
    pass


@dataclass
class MappedAnnouncement:
    family: str
    house_manage_no: str
    pblanc_no: str
    fields: dict
    housing_types: list
    events: list
    warnings: list = field(default_factory=list)

    @property
    def key(self):
        return (self.family, self.house_manage_no, self.pblanc_no)


def _text(v):
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def _req(rec, name, what):
    v = _text(rec.get(name))
    if v is None:
        raise MappingError(f"{what}: 필수 필드 {name} 누락")
    return v


def _date_or_none(rec, name):
    v = _text(rec.get(name))
    if v is None:
        return None
    if not _DATE.fullmatch(v):
        raise MappingError(f"날짜 형식 오류 {name}")
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise MappingError(f"날짜 값 오류 {name}") from None


def _int_or_none(rec, name):
    v = _text(rec.get(name))
    if v is None:
        return None
    if not re.fullmatch(r"\d+", v):
        raise MappingError(f"정수 형식 오류 {name}")
    return int(v)


def _area(rec, name, warnings):
    v = _text(rec.get(name))
    if v is None:
        return None
    try:
        d = Decimal(v)
    except InvalidOperation:
        d = None
    if d is None or not d.is_finite() or d <= 0:
        warnings.append(f"area_invalid:{name}")  # 값 자체는 로그에 남기지 않는다
        return None
    return d.quantize(Decimal("0.0001"))


def _map_events(family, rec):
    out = []
    for code, scope, f_start, f_end in _EVENTS[family]:
        start = _date_or_none(rec, f_start)
        end = _date_or_none(rec, f_end) if f_end else start
        if start is None and end is None:
            continue
        if f_end is None:
            end = start
        if start and end and start > end:
            raise MappingError(f"날짜 역전 {code}/{scope}")
        out.append({"source_event_code": code, "scope_code": scope, "starts_on": start, "ends_on": end})
    return out


def _map_model(family, m, warnings):
    key = _req(m, "MODEL_NO", "주택형")
    if family == "urbty_ofctl":
        house_ty, price_field = _req(m, "TP", "주택형"), "SUPLY_AMOUNT"
        excl, supply = _area(m, "EXCLUSE_AR", warnings), None
        special = None
    else:
        house_ty, price_field = _req(m, "HOUSE_TY", "주택형"), "LTTOT_TOP_AMOUNT"
        excl, supply = None, _area(m, "SUPLY_AR", warnings)
        special = _int_or_none(m, "SPSPLY_HSHLDCO")
    raw = _text(m.get(price_field))
    price = to_krw(raw, "MANWON")  # 세 계열 모두 만원(Swagger 확인)
    if raw is not None and price is None:
        # 미공개 가격의 실제 표현은 미확인이다. 빈 값만 '미확인'으로 두고, 그 밖의 해석 불가 값은 이상치로 보고
        # 공고를 건너뛴다(기존 정상 가격 보존, 실행 실패로 기록). 실제 표현이 확인되면 여기에 허용 목록을 추가한다.
        raise MappingError(f"가격 이상치 {price_field}")
    categories = []
    if family == "apt":
        categories = [{"category_code": code, "supply_count": _int_or_none(m, f)}
                      for f, code in _SPECIAL_CATEGORIES.items() if _int_or_none(m, f) is not None]
    return {
        "source_model_key": key, "house_ty": house_ty,
        "exclusive_area_sqm": excl, "supply_area_sqm": supply,
        "general_supply_count": _int_or_none(m, "SUPLY_HSHLDCO"), "special_supply_count": special,
        "price_max_krw": price, "price_raw": raw,
        "price_source_unit": "MANWON" if price is not None else "UNKNOWN",
        "special_supply": categories,
    }


def map_announcement(family, detail, models):
    if family not in FAMILIES:
        raise MappingError(f"알 수 없는 계열 {family}")
    hmn, pno = _req(detail, "HOUSE_MANAGE_NO", "공고"), _req(detail, "PBLANC_NO", "공고")
    warnings, types, seen = [], [], set()
    for m in models:
        if (_text(m.get("HOUSE_MANAGE_NO")), _text(m.get("PBLANC_NO"))) != (hmn, pno):
            raise MappingError("다른 공고의 주택형이 섞임")
        t = _map_model(family, m, warnings)
        if t["source_model_key"] in seen:
            raise MappingError("주택형 키(MODEL_NO) 중복")
        seen.add(t["source_model_key"])
        types.append(t)
    prices = [t["price_max_krw"] for t in types if t["price_max_krw"] is not None]
    fields = {
        "house_nm": _req(detail, "HOUSE_NM", "공고"),
        "source_subtype": (_text(detail.get("HOUSE_SECD")) if family == "remndr"
                           else _text(detail.get("SEARCH_HOUSE_SECD")) if family == "urbty_ofctl" else None),
        "house_secd": _text(detail.get("HOUSE_SECD")),
        "house_dtl_secd": _text(detail.get("HOUSE_DTL_SECD")),
        "rent_secd": _text(detail.get("RENT_SECD")),
        "rcrit_pblanc_de": _date_or_none(detail, "RCRIT_PBLANC_DE"),
        "hssply_adres": _text(detail.get("HSSPLY_ADRES")),
        "source_region_code": _text(detail.get("SUBSCRPT_AREA_CODE")),
        "source_region_name": _text(detail.get("SUBSCRPT_AREA_CODE_NM")),
        "tot_suply_hshldco": _int_or_none(detail, "TOT_SUPLY_HSHLDCO"),
        "bsns_mby_nm": _text(detail.get("BSNS_MBY_NM")),
        "mdhs_telno": _text(detail.get("MDHS_TELNO")),
        "pblanc_url": _text(detail.get("PBLANC_URL")),
        "min_price_krw": min(prices) if prices else None,   # 표시 요약(확정가 아님)
        "max_price_krw": max(prices) if prices else None,
    }
    return MappedAnnouncement(family, hmn, pno, fields, types, _map_events(family, detail), warnings)
