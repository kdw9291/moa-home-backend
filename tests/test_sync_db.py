"""실제 PostgreSQL에서 수집 저장을 검증: 멱등 재실행, 중간 실패 후 재실행, 원자성, 기존 데이터 보존.

TEST_DATABASE_URL 없으면 skip(미검증). 안전장치는 tests/conftest.py의 db_conn.
"""
import os
from datetime import date

import pytest

from fakes import FixtureClient
from moahome.mappers import map_announcement
from moahome.store import PgStore
from moahome.sync import run_sync
from test_mappers import cases

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL 미설정: DB 검증 미실행")
TODAY = date(2026, 9, 29)
TABLES = ["cheongyak_announcements", "cheongyak_housing_types", "housing_type_special_supply", "announcement_events"]


@pytest.fixture()
def store(db_conn):
    for t in reversed(TABLES):
        db_conn.execute(f"DELETE FROM public.{t}")
    db_conn.execute("DELETE FROM public.sync_runs")
    db_conn.execute("UPDATE public.data_status SET last_successful_sync_at=NULL")
    s = PgStore(os.environ["TEST_DATABASE_URL"])
    yield s
    s.close()


def counts(c):
    return {t: c.execute(f"SELECT count(*) FROM public.{t}").fetchone()[0] for t in TABLES}


def snapshot(c):
    """ID와 갱신 시각을 제외한 내용 스냅샷."""
    q = {
        "a": "SELECT source_family, house_manage_no, pblanc_no, house_nm, rcrit_pblanc_de, min_price_krw, max_price_krw"
             " FROM public.cheongyak_announcements ORDER BY 1,2,3",
        "h": "SELECT a.house_manage_no, h.source_model_key, h.house_ty, h.exclusive_area_sqm, h.supply_area_sqm,"
             " h.price_max_krw, h.price_raw, h.price_source_unit FROM public.cheongyak_housing_types h"
             " JOIN public.cheongyak_announcements a ON a.id=h.announcement_id ORDER BY 1,2",
        "e": "SELECT a.house_manage_no, e.source_event_code, e.scope_code, e.starts_on, e.ends_on"
             " FROM public.announcement_events e JOIN public.cheongyak_announcements a ON a.id=e.announcement_id ORDER BY 1,2,3",
    }
    return {k: c.execute(v).fetchall() for k, v in q.items()}


def test_first_run_stores_everything_and_marks_status(db_conn, store):
    res = run_sync(FixtureClient(), store, TODAY)
    assert res["status"] == "succeeded"
    c = counts(db_conn)
    assert c["cheongyak_announcements"] == 7 and c["cheongyak_housing_types"] == 16 and c["announcement_events"] > 0
    assert db_conn.execute("SELECT last_successful_sync_at IS NOT NULL FROM public.data_status").fetchone()[0]
    run = db_conn.execute("SELECT status, details->>'failure_count' FROM public.sync_runs").fetchone()
    assert run == ("succeeded", "0")


def test_second_identical_run_is_idempotent(db_conn, store):
    run_sync(FixtureClient(), store, TODAY)
    before_counts, before_snap = counts(db_conn), snapshot(db_conn)
    res = run_sync(FixtureClient(), store, TODAY)
    assert res["status"] == "succeeded"
    assert counts(db_conn) == before_counts and snapshot(db_conn) == before_snap


def test_failure_midway_then_rerun_completes_without_duplicates(db_conn, store):
    c = FixtureClient()
    victim = c.data["getRemndrLttotPblancDetail"][1]["HOUSE_MANAGE_NO"]
    first = run_sync(FixtureClient(fail_models={victim: "network"}), store, TODAY)
    assert first["status"] == "failed"
    assert counts(db_conn)["cheongyak_announcements"] == 6
    assert db_conn.execute("SELECT last_successful_sync_at IS NULL FROM public.data_status").fetchone()[0]
    second = run_sync(FixtureClient(), store, TODAY)
    assert second["status"] == "succeeded"
    assert counts(db_conn)["cheongyak_announcements"] == 7 and counts(db_conn)["cheongyak_housing_types"] == 16


def test_failed_announcement_keeps_previous_data(db_conn, store):
    run_sync(FixtureClient(), store, TODAY)
    before = snapshot(db_conn)
    c = FixtureClient()
    bad = c.data["getAPTLttotPblancDetail"][0]
    bad["RCEPT_BGNDE"], bad["RCEPT_ENDDE"] = "2026-12-31", "2026-01-01"   # 날짜 역전 -> 매핑 오류
    res = run_sync(c, store, TODAY)
    assert res["status"] == "failed"
    assert snapshot(db_conn) == before                                   # 기존 정상 데이터 그대로


def test_api_failure_deletes_nothing(db_conn, store):
    run_sync(FixtureClient(), store, TODAY)
    before = counts(db_conn)
    res = run_sync(FixtureClient(fail_detail={"getAPTLttotPblancDetail": "network"}), store, TODAY)
    assert res["status"] == "failed" and counts(db_conn) == before


def test_vanished_model_pruned_only_when_models_returned(db_conn, store):
    run_sync(FixtureClient(), store, TODAY)
    c = FixtureClient()
    apt = c.data["getAPTLttotPblancDetail"][0]
    models = [m for m in c.data["getAPTLttotPblancMdl"] if m["HOUSE_MANAGE_NO"] == apt["HOUSE_MANAGE_NO"]]
    assert len(models) >= 2
    before = counts(db_conn)["cheongyak_housing_types"]
    c.data["getAPTLttotPblancMdl"] = [m for m in c.data["getAPTLttotPblancMdl"] if m is not models[-1]
                                      and not (m["HOUSE_MANAGE_NO"] == apt["HOUSE_MANAGE_NO"] and m["MODEL_NO"] == models[-1]["MODEL_NO"])]
    run_sync(c, store, TODAY)
    assert counts(db_conn)["cheongyak_housing_types"] == before - 1   # 응답에 없는 주택형만 삭제
    empty = FixtureClient()
    empty.data["getAPTLttotPblancMdl"] = []
    mid = counts(db_conn)["cheongyak_housing_types"]
    run_sync(empty, store, TODAY)
    assert counts(db_conn)["cheongyak_housing_types"] == mid            # 빈 응답에서는 삭제하지 않음


def test_upsert_is_atomic_when_a_type_violates_a_db_constraint(db_conn, store):
    run_sync(FixtureClient(), store, TODAY)
    before = snapshot(db_conn)
    d, ms = next(cases("apt"))
    mapped = map_announcement("apt", d, ms)
    mapped.fields["house_nm"] = "변경된 이름(롤백되어야 함)"
    mapped.events[0]["starts_on"], mapped.events[0]["ends_on"] = date(2026, 12, 1), date(2026, 1, 1)  # DB CHECK 위반
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        store.upsert_announcement(mapped, prune_models=True)
    assert snapshot(db_conn) == before                                   # 마스터·주택형·일정 모두 그대로


def test_price_unknown_is_stored_as_null_with_unknown_unit(db_conn, store):
    c = FixtureClient()
    c.data["getAPTLttotPblancMdl"][0]["LTTOT_TOP_AMOUNT"] = ""
    victim = c.data["getAPTLttotPblancMdl"][0]
    res = run_sync(c, store, TODAY)
    assert res["status"] == "succeeded"
    row = db_conn.execute(
        "SELECT h.price_max_krw, h.price_raw, h.price_source_unit FROM public.cheongyak_housing_types h"
        " JOIN public.cheongyak_announcements a ON a.id=h.announcement_id"
        " WHERE a.house_manage_no=%s AND h.source_model_key=%s", (victim["HOUSE_MANAGE_NO"], victim["MODEL_NO"])).fetchone()
    assert row == (None, None, "UNKNOWN")


def test_run_details_contain_no_secrets_or_urls(db_conn, store):
    c = FixtureClient()
    run_sync(FixtureClient(fail_models={c.data["getAPTLttotPblancDetail"][0]["HOUSE_MANAGE_NO"]: "network"}), store, TODAY)
    text = db_conn.execute("SELECT details::text FROM public.sync_runs").fetchone()[0]
    assert "serviceKey" not in text and "http" not in text.lower().replace('"stage"', "")


def test_empty_model_response_keeps_prices_events_and_summary(db_conn, store):
    run_sync(FixtureClient(), store, TODAY)
    before = snapshot(db_conn)
    empty = FixtureClient()
    empty.data["getAPTLttotPblancMdl"] = []
    res = run_sync(empty, store, TODAY)
    assert res["status"] == "failed" and res["families"]["apt"]["empty_models_preserved"] == 2
    assert snapshot(db_conn) == before          # 가격 요약·주택형·일정 모두 보존


def test_success_record_and_data_status_are_updated_together(db_conn, store):
    res = run_sync(FixtureClient(), store, TODAY)
    row = db_conn.execute(
        "SELECT r.status, d.last_successful_sync_at >= r.started_at FROM public.sync_runs r, public.data_status d").fetchone()
    assert res["status"] == "succeeded" and row == ("succeeded", True)
