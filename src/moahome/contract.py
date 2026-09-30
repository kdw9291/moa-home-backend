"""데이터 계약 규칙 (원천 필드명과 무관한 순수 로직).

원천 API 필드 매핑은 fixture 검증 전이므로 여기에 두지 않는다.
단위는 호출자가 검증된 매퍼에서 명시적으로 전달해야 한다.
"""
import re
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

KST = timezone(timedelta(hours=9))
BIGINT_MAX = 2**63 - 1
_GROUPED = re.compile(r"\d{1,3}(,\d{3})+(\.\d+)?")
_UNKNOWN_TOKENS ={"", "-", "null", "none", "n/a"}


def to_krw(raw, unit):
    """원 단위 정수 또는 None. unit: 'KRW' | 'MANWON' | 'UNKNOWN'.

    미확인 단위, 빈 값, '-', 음수는 0이 아니라 None.
    """
    if unit not in ("KRW", "MANWON", "UNKNOWN"):
        raise ValueError(f"unknown unit: {unit!r}")
    if unit == "UNKNOWN" or raw is None:
        return None
    text = str(raw).strip()
    if "," in text:
        if not _GROUPED.fullmatch(text):  # '1,2' 같은 잘못된 천 단위 구분은 미확인
            return None
        text = text.replace(",", "")
    if text.lower() in _UNKNOWN_TOKENS:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    if value.adjusted() > 19:  # 지수 폭주값은 곱셈·int 변환 전에 이상치 처리
        return None
    if unit == "MANWON":
        value *= 10000
    if value != value.to_integral_value():
        return None
    krw = int(value)
    return krw if krw <= BIGINT_MAX else None  # price_max_krw BIGINT 범위 초과는 이상치 -> 미확인


def kst_date(instant):
    """절대 시각(aware datetime)의 KST 달력 날짜. naive 입력은 거부."""
    if instant.tzinfo is None:
        raise ValueError("naive datetime is ambiguous")
    return instant.astimezone(KST).date()


def d_day(today_kst, target):
    """양수=남은 일수, 0=당일, 음수=지남."""
    return (target - today_kst).days


def _dec(v):
    """면적 등 숫자값을 Decimal로. 없음/형식 오류/NaN/Infinity는 None(미확인)."""
    if v is None:
        return None
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def housing_type_matches(row, budget_krw=None, min_area=None, max_area=None):
    """한 주택형 행에서 예산과 면적을 함께 판정한다.

    price_max_krw는 주택형 최고금액(설계 계약): 예산 이하면 일치, 초과면 미확인. 전용면적은
    exclusive_area_sqm만 사용하며 supply_area_sqm/HOUSE_TY로 대체하지 않는다.
    반환: True(일치) / False(불일치) / None(미확인: 조건이 있는데 해당 값이 NULL).
    예산·면적은 서로 다른 주택형의 값을 섞지 않는다.
    """
    results = []
    price, area = row.get("price_max_krw"), _dec(row.get("exclusive_area_sqm"))
    if budget_krw is not None:
        if price is None:
            results.append(None)
        elif price <= budget_krw:
            results.append(True)
        else:  # 가격은 주택형 '분양최고금액': 초과여도 더 싼 세대가 있을 수 있어 확정 불일치가 아님
            results.append(None)
    if min_area is not None:
        results.append(None if area is None else area >= Decimal(str(min_area)))
    if max_area is not None:
        results.append(None if area is None else area <= Decimal(str(max_area)))
    if any(r is False for r in results):
        return False
    if any(r is None for r in results):
        return None
    return True


def announcement_matches(rows, **criteria):
    """공고는 어느 한 주택형 행이 모든 조건을 만족할 때 일치. 미확인은 별도 반환."""
    outcomes = [housing_type_matches(r, **criteria) for r in rows]
    if any(o is True for o in outcomes):
        return True
    if any(o is None for o in outcomes):
        return None
    return False


def dedupe_by_key(records, key_fn):
    """자연키 기준 중복 제거(마지막 값 우선), (결과, 중복 수) 반환."""
    seen = {}
    for r in records:
        seen[key_fn(r)] = r
    return list(seen.values()), len(records) - len(seen)


def announcement_key(family, house_manage_no, pblanc_no):
    """공고 자연키. 계열이 다르면 번호가 같아도 다른 공고.

    식별자가 없거나 공백이면 ValueError: 누락 공고끼리 'None' 키로 합쳐지는 것을 막는다.
    """
    for name, v in (("family", family), ("house_manage_no", house_manage_no), ("pblanc_no", pblanc_no)):
        if v is None or not str(v).strip():
            raise ValueError(f"missing announcement identifier: {name}")
    return (family, str(house_manage_no).strip(), str(pblanc_no).strip())
