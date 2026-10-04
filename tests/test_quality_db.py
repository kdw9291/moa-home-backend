"""수집 데이터 품질 점검(moahome.quality): 항목별로 의도한 데이터에서만 걸리고, 정상 데이터에서는 걸리지 않으며, DB를 바꾸지 않는다."""
import os
import uuid
from datetime import date

import psycopg
import pytest

from moahome.quality import format_report, overview, run_checks

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL 미설정: DB 검증 미실행")
TODAY = date(2026, 10, 4)


@pytest.fixture()
def db(db_conn):
    for t in ("announcement_events", "housing_type_special_supply", "cheongyak_housing_types", "cheongyak_announcements"):
        db_conn.execute(f"DELETE FROM public.{t}")
    yield db_conn
    for t in ("announcement_events", "housing_type_special_supply", "cheongyak_housing_types", "cheongyak_announcements"):
        db_conn.execute(f"DELETE FROM public.{t}")


def ann(c, family="apt", no=None, name="테스트 단지", rcrit=date(2026, 9, 20), tot=10, url="https://www.applyhome.co.kr/x",
        region="410", seen="now()", minp=None, maxp=None):
    no = no or str(uuid.uuid4().int)[:10]
    return c.execute(
        "INSERT INTO public.cheongyak_announcements(source_family, house_manage_no, pblanc_no, house_nm, rcrit_pblanc_de, tot_suply_hshldco, "
        f"pblanc_url, source_region_code, last_seen_at, min_price_krw, max_price_krw) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,{seen},%s,%s) RETURNING id",
        (family, no, no, name, rcrit, tot, url, region, minp, maxp)).fetchone()[0]


def ht(c, aid, key="01", excl=None, supply=None, price=None, general=5, special=5):
    return c.execute(
        "INSERT INTO public.cheongyak_housing_types(announcement_id, source_model_key, house_ty, exclusive_area_sqm, supply_area_sqm, "
        "general_supply_count, special_supply_count, price_max_krw, price_raw, price_source_unit) VALUES (%s,%s,'84A',%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (aid, key, excl, supply, general, special, price, None if price is None else str(price), "KRW" if price is not None else "UNKNOWN")).fetchone()[0]


def ev(c, aid, code="rcept", start=date(2026, 10, 1), end=date(2026, 10, 10)):
    c.execute("INSERT INTO public.announcement_events(announcement_id, source_event_code, starts_on, ends_on) VALUES (%s,%s,%s,%s)", (aid, code, start, end))


def good_apt(c, **kw):
    """모든 점검을 통과하는 정상 APT 공고."""
    a = ann(c, family="apt", minp=500_000_000, maxp=500_000_000, tot=10, **kw)
    ht(c, a, supply=110.5, price=500_000_000, general=5, special=5)
    ev(c, a)
    return a


def hits(c, key):
    return {r.check.key: r for r in run_checks(c, TODAY)}[key]


def test_a_clean_dataset_has_no_warnings(db):
    good_apt(db)
    o = ann(db, family="urbty_ofctl", minp=200_000_000, maxp=200_000_000, tot=10)
    ht(db, o, excl=24.5, price=200_000_000)
    ev(db, o)
    res = run_checks(db, TODAY)
    assert [r.check.key for r in res if r.check.level == "warn" and r.hits] == []


def test_announcement_without_housing_types_is_flagged(db):
    good_apt(db)
    ann(db, name="주택형 없는 공고")
    r = hits(db, "no_housing_types")
    assert r.hits == 1 and "주택형 없는 공고" in r.samples[0]


def test_urbty_without_exclusive_area_and_apt_without_supply_area(db):
    o = ann(db, family="urbty_ofctl"); ht(db, o, excl=None, price=1_000_000)
    a = ann(db, family="apt"); ht(db, a, supply=None, price=500_000_000)
    r = ann(db, family="remndr"); ht(db, r, supply=None, price=500_000_000)
    assert hits(db, "urbty_exclusive_missing").hits == 1
    assert hits(db, "apt_supply_missing").hits == 1
    rr = hits(db, "remndr_supply_missing")
    assert rr.hits == 1 and rr.check.level == "info"             # 잔여세대는 원천이 안 주는 경우가 있어 참고 항목


@pytest.mark.parametrize("price,flag", [(9_999_999, True), (10_000_000, False), (100_000_000_000, False), (100_000_000_001, True), (0, True)])
def test_price_unit_boundaries(db, price, flag):
    a = ann(db); ht(db, a, supply=100, price=price)
    assert (hits(db, "price_unit_suspect").hits == 1) is flag


@pytest.mark.parametrize("excl,supply,flag", [(9.99, None, True), (10, None, False), (300, None, False), (300.01, None, True), (None, 400.01, True), (None, 400, False)])
def test_area_range_boundaries(db, excl, supply, flag):
    a = ann(db); ht(db, a, excl=excl, supply=supply, price=500_000_000)
    assert (hits(db, "area_out_of_range").hits == 1) is flag


def test_supply_smaller_than_exclusive(db):
    a = ann(db); ht(db, a, excl=84, supply=59, price=500_000_000)
    assert hits(db, "supply_less_than_exclusive").hits == 1


def test_announcements_without_usable_events(db):
    a = ann(db); ht(db, a, supply=100, price=500_000_000)                       # 일정 없음
    b = ann(db); ht(db, b, supply=100, price=500_000_000); ev(db, b, start=None, end=None)   # 날짜 모두 비어 있음
    c = good_apt(db)
    r = hits(db, "no_events")
    assert r.hits == 2 and r.total == 3


@pytest.mark.parametrize("start,end,flag", [(date(1999, 12, 31), None, True), (date(2000, 1, 1), None, False), (None, date(2028, 10, 5), True), (None, date(2028, 10, 4), False)])
def test_event_date_range(db, start, end, flag):
    a = ann(db); ev(db, a, start=start, end=end)
    assert (hits(db, "event_date_range").hits == 1) is flag


def test_special_supply_sum_mismatch(db):
    a = ann(db)
    h1 = ht(db, a, key="01", supply=100, price=500_000_000, special=10)
    h2 = ht(db, a, key="02", supply=100, price=500_000_000, special=10)
    db.execute("INSERT INTO public.housing_type_special_supply VALUES (%s,'신혼',6),(%s,'생애최초',4)", (h1, h1))      # 합 10 = 일치
    db.execute("INSERT INTO public.housing_type_special_supply VALUES (%s,'신혼',6),(%s,'생애최초',3)", (h2, h2))      # 합 9 != 10
    r = hits(db, "special_sum_mismatch")
    assert r.hits == 1 and r.total == 2


def test_stale_announcement_with_remaining_schedule(db):
    stale = ann(db, name="오래 안 본 공고", seen="now() - interval '8 days'"); ev(db, stale, end=date(2026, 10, 10))
    old_done = ann(db, name="오래됐지만 끝난 공고", seen="now() - interval '30 days'"); ev(db, old_done, start=date(2026, 8, 1), end=date(2026, 8, 5))
    fresh = ann(db, name="최근 수집 공고"); ev(db, fresh, end=date(2026, 10, 10))
    r = hits(db, "stale_but_active")
    assert r.hits == 1 and "오래 안 본 공고" in r.samples[0]


def test_summary_price_mismatch(db):
    a = ann(db, minp=1, maxp=2); ht(db, a, supply=100, price=500_000_000)       # 요약이 주택형과 다름
    b = ann(db, minp=500_000_000, maxp=500_000_000); ht(db, b, supply=100, price=500_000_000)
    c = ann(db); ht(db, c, supply=100, price=None)                               # 가격 모두 미확인이면 요약도 NULL
    assert hits(db, "summary_price_mismatch").hits == 1


def test_info_checks(db):
    ann(db, rcrit=None, tot=None, region=None, url=None, name="정보 항목")
    d1 = ann(db, family="remndr", name="더샵 중복", rcrit=date(2026, 9, 1)); ann(db, family="remndr", name="더샵 중복", rcrit=date(2026, 9, 1))
    a = ann(db, tot=100); ht(db, a, supply=100, price=500_000_000, general=3, special=4)       # 합 7 != 100
    for key, n in (("rcrit_date_odd", 1), ("total_supply_missing", 1), ("region_missing", 1), ("url_odd", 1), ("duplicate_suspect", 2), ("total_supply_mismatch", 1)):
        r = hits(db, key)
        assert r.hits == n and r.check.level == "info", key


def test_report_text_and_read_only(db):
    good_apt(db)
    ann(db, name="주택형 없는 공고")
    before = db.execute("SELECT count(*), md5(string_agg(t::text, '|' ORDER BY id)) FROM public.cheongyak_announcements t").fetchone()
    text = format_report(TODAY, overview(db), run_checks(db, TODAY))
    assert "수집 데이터 품질 점검" in text and "주택형이 하나도 없는 공고: 1/2" in text and "대조 방법" in text
    assert "요약: 이상 의심 항목 2개" in text            # 주택형이 없는 공고는 일정도 없어 두 항목에 걸린다
    assert db.execute("SELECT count(*), md5(string_agg(t::text, '|' ORDER BY id)) FROM public.cheongyak_announcements t").fetchone() == before


def test_cli_transaction_is_read_only(db):
    """진입점과 같은 방식(READ ONLY 트랜잭션)으로 실행하면 쓰기 시도가 거부된다."""
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as c:
        c.execute("SET TRANSACTION READ ONLY")
        run_checks(c, TODAY)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("DELETE FROM public.cheongyak_announcements")
        c.rollback()
