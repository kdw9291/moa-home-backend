"""필터 저장 RPC(public.save_user_filter_settings) 계약 검증 — 실제 PostgreSQL(일회용 DB), 회원 권한(authenticated + JWT sub).

계약: docs/DATABASE_SCHEMA.sql, docs/API_SPEC.md ('필터 저장 RPC'). 실제 Supabase 프로젝트 DB에서는 실행하지 않는다.
"""
import os
import threading
import uuid
from decimal import Decimal

from moahome.notify import cleanup_filter_save_requests

import psycopg
import pytest
from psycopg import errors

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL 미설정: DB 검증 미실행")
U1, U2 = str(uuid.uuid4()), str(uuid.uuid4())
CALL = ("SELECT public.save_user_filter_settings(%(regions)s::text[], %(budget)s::bigint, %(mn)s::numeric, %(mx)s::numeric, "
        "%(fam)s::text[], %(q)s::text[], %(exp)s::integer, %(rid)s::uuid)")


def args(**over):
    base = dict(regions=["410"], budget=600_000_000, mn=Decimal("58.84"), mx=Decimal("84"), fam=["apt"], q=["_ui:min_excl"],
                exp=0, rid=str(uuid.uuid4()))
    base.update(over)
    return base


def save(c, **over):
    return c.execute(CALL, args(**over)).fetchone()[0]


@pytest.fixture()
def db(db_conn):
    db_conn.execute("DELETE FROM auth.users WHERE id IN (%s, %s)", (U1, U2))   # 설정·요청 기록은 ON DELETE CASCADE
    db_conn.execute("INSERT INTO auth.users(id) VALUES (%s), (%s)", (U1, U2))
    yield db_conn
    db_conn.execute("DELETE FROM auth.users WHERE id IN (%s, %s)", (U1, U2))


class as_user:
    """authenticated 역할 + JWT sub로 한 트랜잭션을 실행하고 항상 롤백한다. sub=None이면 로그인하지 않은 authenticated."""

    def __init__(self, c, sub, role="authenticated"):
        self.c, self.sub, self.role = c, sub, role

    def __enter__(self):
        self.c.autocommit = False
        try:
            self.c.execute(f"SET LOCAL ROLE {self.role}")
            if self.sub:
                self.c.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (self.sub,))
        except Exception:
            self.__exit__()
            raise
        return self.c

    def __exit__(self, *exc):
        self.c.rollback()
        self.c.autocommit = True
        return False


def count_requests(c, user):
    """요청 기록은 클라이언트 권한이 없으므로 같은 트랜잭션에서 잠시 소유자 권한으로 센다."""
    c.execute("RESET ROLE")
    try:
        return c.execute("SELECT count(*) FROM public.user_filter_save_requests WHERE user_id=%s", (user,)).fetchone()[0]
    finally:
        c.execute("SET LOCAL ROLE authenticated")


# ---------- 정상 저장 ----------

def test_first_save_inserts_revision_1_and_returns_typed_current(db):
    with as_user(db, U1) as c:
        r = save(c, budget=9_007_199_254_740_993)   # JS 안전 정수 초과: 문자열로 정밀도를 유지해 돌려준다
        assert r["status"] == "saved" and r["applied_revision"] == 1 and r["replayed"] is False
        cur = r["current"]
        assert cur["revision"] == 1 and cur["budget_max_krw"] == "9007199254740993"
        assert cur["preferred_region_codes"] == ["410"] and cur["housing_families"] == ["apt"]
        assert cur["qualification_preferences"] == ["_ui:min_excl"]
        assert float(cur["min_area_sqm"]) == 58.84 and float(cur["max_area_sqm"]) == 84.0
        assert "user_id" not in cur and "updated_at" not in cur
        row = c.execute("SELECT budget_max_krw, revision FROM public.user_filter_settings").fetchone()
        assert row == (9_007_199_254_740_993, 1)
        assert count_requests(c, U1) == 1


def test_update_with_matching_expected_revision_increments_by_one(db):
    with as_user(db, U1) as c:
        save(c, exp=0)
        r = save(c, exp=1, budget=500_000_000, regions=["100", "410"], q=[])
        assert r["status"] == "saved" and r["applied_revision"] == 2
        assert r["current"]["budget_max_krw"] == "500000000" and r["current"]["qualification_preferences"] == []


def test_nullable_limits_can_be_cleared(db):
    with as_user(db, U1) as c:
        r = save(c, budget=None, mn=None, mx=None, fam=[], regions=[], q=[])
        cur = r["current"]
        assert cur["budget_max_krw"] is None and cur["min_area_sqm"] is None and cur["max_area_sqm"] is None


def test_area_is_rounded_to_numeric_10_2_before_validation(db):
    with as_user(db, U1) as c:
        r = save(c, mn=Decimal("59.004"), mx=Decimal("84.996"))
        assert float(r["current"]["min_area_sqm"]) == 59.0 and float(r["current"]["max_area_sqm"]) == 85.0


# ---------- 충돌 ----------

def test_stale_expected_revision_returns_conflict_with_full_current_and_does_not_write(db):
    with as_user(db, U1) as c:
        save(c, exp=0)
        save(c, exp=1, budget=500_000_000)                      # revision 2
        before = count_requests(c, U1)
        r = save(c, exp=1, budget=1)                            # 오래된 예상값
        assert r["status"] == "conflict" and "applied_revision" not in r
        assert r["current"]["revision"] == 2 and r["current"]["budget_max_krw"] == "500000000"
        assert set(r["current"]) == {"preferred_region_codes", "budget_max_krw", "min_area_sqm", "max_area_sqm",
                                     "housing_families", "qualification_preferences", "revision"}
        assert c.execute("SELECT budget_max_krw FROM public.user_filter_settings").fetchone()[0] == 500_000_000
        assert count_requests(c, U1) == before                  # 충돌은 요청 기록을 남기지 않는다


def test_first_insert_expectation_conflicts_when_row_already_exists(db):
    with as_user(db, U1) as c:
        save(c, exp=0)
        r = save(c, exp=0)
        assert r["status"] == "conflict" and r["current"]["revision"] == 1


def test_nonzero_expectation_on_missing_row_conflicts_with_default_current_at_revision_0(db):
    with as_user(db, U1) as c:
        r = save(c, exp=5)
        assert r["status"] == "conflict"
        assert r["current"] == {"preferred_region_codes": [], "budget_max_krw": None, "min_area_sqm": None, "max_area_sqm": None,
                                "housing_families": [], "qualification_preferences": [], "revision": 0}
        assert c.execute("SELECT count(*) FROM public.user_filter_settings").fetchone()[0] == 0


# ---------- 멱등 재시도 ----------

def test_same_request_id_and_input_replays_without_second_increment(db):
    rid = str(uuid.uuid4())
    with as_user(db, U1) as c:
        first = save(c, exp=0, rid=rid)
        again = save(c, exp=0, rid=rid)                         # 시간 초과 뒤 같은 입력 재시도
        assert again["status"] == "saved" and again["replayed"] is True and again["applied_revision"] == 1
        assert again["current"] == first["current"]
        assert c.execute("SELECT revision FROM public.user_filter_settings").fetchone()[0] == 1
        assert count_requests(c, U1) == 1


def test_replay_returns_the_original_snapshot_even_after_later_saves(db):
    rid = str(uuid.uuid4())
    with as_user(db, U1) as c:
        save(c, exp=0, rid=rid, budget=111)
        save(c, exp=1, budget=222)
        save(c, exp=2, budget=333)                              # 서버는 revision 3
        again = save(c, exp=0, rid=rid, budget=111)
        assert again["replayed"] is True and again["applied_revision"] == 1 and again["current"]["budget_max_krw"] == "111"
        assert c.execute("SELECT budget_max_krw, revision FROM public.user_filter_settings").fetchone() == (333, 3)   # 서버 값은 그대로


def test_same_request_id_with_different_input_is_rejected(db):
    rid = str(uuid.uuid4())
    with as_user(db, U1) as c:
        save(c, exp=0, rid=rid, budget=100)
    with as_user(db, U1) as c:
        save(c, exp=0, rid=rid, budget=100)                     # 롤백되므로 같은 트랜잭션에서 다시 시도
        with pytest.raises(errors.InvalidParameterValue):       # 22023
            save(c, exp=0, rid=rid, budget=101)


def test_different_expected_revision_with_same_request_id_is_a_different_input(db):
    rid = str(uuid.uuid4())
    with as_user(db, U1) as c:
        save(c, exp=0, rid=rid)
        with pytest.raises(errors.InvalidParameterValue):
            save(c, exp=1, rid=rid)


def test_request_ids_are_scoped_per_account(db):
    rid = str(uuid.uuid4())
    db.execute("INSERT INTO public.user_filter_settings(user_id) VALUES (%s)", (U2,))   # U2는 이미 행(revision 1)이 있다
    with as_user(db, U1) as c:
        assert save(c, exp=0, rid=rid)["replayed"] is False
    with as_user(db, U2) as c:
        r = save(c, exp=1, rid=rid, budget=7)                   # 다른 계정의 같은 UUID는 재생으로 취급하지 않는다
        assert r["status"] == "saved" and r["replayed"] is False and r["applied_revision"] == 2


def test_rejected_calls_leave_no_request_record(db):
    with as_user(db, U1) as c:
        for bad in (dict(budget=-1), dict(mn=Decimal("0.001")), dict(fam=None)):
            c.execute("SAVEPOINT s")
            with pytest.raises(psycopg.Error):
                save(c, **bad)
            c.execute("ROLLBACK TO SAVEPOINT s")
        assert count_requests(c, U1) == 0
        assert c.execute("SELECT count(*) FROM public.user_filter_settings").fetchone()[0] == 0


# ---------- 인증·주체 ----------

def test_unauthenticated_call_fails_with_28000(db):
    with as_user(db, None) as c, pytest.raises(errors.InvalidAuthorizationSpecification):   # 28000
        save(c)


def test_anon_and_public_cannot_execute_the_function(db):
    with as_user(db, U1, role="anon") as c, pytest.raises(errors.InsufficientPrivilege):
        save(c)
    acl = db.execute("SELECT proacl::text FROM pg_proc WHERE proname='save_user_filter_settings'").fetchone()[0]
    entries = acl.strip("{}").split(",")
    assert any(e.startswith("authenticated=X") for e in entries)
    assert not any(e.startswith("=") or e.startswith("anon=") for e in entries)   # PUBLIC(=X)·anon 실행 권한 없음


def test_function_has_no_user_id_argument_and_is_hardened(db):
    names, definer, config = db.execute(
        "SELECT proargnames, prosecdef, proconfig FROM pg_proc WHERE proname='save_user_filter_settings'").fetchone()
    assert "user_id" not in " ".join(names) and not any("user" in n for n in names)
    assert definer is True and config and any(x.replace(" ", "") in ("search_path=", 'search_path=""') for x in config)


def test_other_user_cannot_change_or_see_someone_elses_row_through_the_rpc(db):
    with as_user(db, U1) as c:
        save(c, exp=0, budget=100)
    db.execute("INSERT INTO public.user_filter_settings(user_id, budget_max_krw, revision) VALUES (%s, 100, 1)", (U1,))
    with as_user(db, U2) as c:
        r = save(c, exp=0, budget=999)                          # U2의 첫 저장: U2의 행만 만든다
        assert r["applied_revision"] == 1
        assert c.execute("SELECT count(*) FROM public.user_filter_settings").fetchone()[0] == 1   # 본인 행만 보인다
    assert db.execute("SELECT budget_max_krw FROM public.user_filter_settings WHERE user_id=%s", (U1,)).fetchone()[0] == 100


def test_jwt_claims_json_also_identifies_the_caller(db):
    db.autocommit = False
    try:
        db.execute("SET LOCAL ROLE authenticated")
        db.execute("SELECT set_config('request.jwt.claims', %s, true)", ('{"sub": "%s", "role": "authenticated"}' % U1,))
        assert save(db, exp=0)["status"] == "saved"
        assert db.execute("SELECT user_id::text FROM public.user_filter_settings").fetchone()[0] == U1
    finally:
        db.rollback()
        db.autocommit = True


# ---------- 입력 검증 ----------

@pytest.mark.parametrize("over,exc", [
    (dict(rid=None), errors.InvalidParameterValue),
    (dict(exp=None), errors.InvalidParameterValue),
    (dict(exp=-1), errors.InvalidParameterValue),
    (dict(regions=None), errors.InvalidParameterValue),
    (dict(fam=None), errors.InvalidParameterValue),
    (dict(q=None), errors.InvalidParameterValue),
    (dict(budget=-1), errors.InvalidParameterValue),
    (dict(mn=Decimal("0")), errors.InvalidParameterValue),
    (dict(mn=Decimal("0.004")), errors.InvalidParameterValue),         # 반올림 뒤 0.00
    (dict(mx=Decimal("-5")), errors.InvalidParameterValue),
    (dict(mn=Decimal("100"), mx=Decimal("50")), errors.InvalidParameterValue),
    (dict(mn=Decimal("NaN")), errors.InvalidParameterValue),
    (dict(mn=Decimal("100000000")), errors.NumericValueOutOfRange),     # numeric(10,2) 범위 초과
])
def test_invalid_inputs_are_rejected_with_documented_sqlstate(db, over, exc):
    with as_user(db, U1) as c, pytest.raises(exc):
        save(c, **over)


def test_bigint_overflow_is_rejected_with_22003(db):
    with as_user(db, U1) as c, pytest.raises(errors.NumericValueOutOfRange):
        c.execute(CALL.replace("%(budget)s::bigint", "'9223372036854775808'::bigint"), args())


def test_revision_integer_overflow_is_rejected_with_22003(db):
    db.execute("INSERT INTO public.user_filter_settings(user_id, revision) VALUES (%s, 2147483647)", (U1,))
    with as_user(db, U1) as c, pytest.raises(errors.NumericValueOutOfRange):
        save(c, exp=2147483647)


def test_bigint_max_budget_is_accepted_and_round_trips_as_string(db):
    with as_user(db, U1) as c:
        r = save(c, budget=9_223_372_036_854_775_807)
        assert r["current"]["budget_max_krw"] == "9223372036854775807"


# ---------- 권한(직접 쓰기·요청 기록) ----------

@pytest.mark.parametrize("sql", [
    "INSERT INTO public.user_filter_settings(user_id) VALUES (auth.uid())",
    "UPDATE public.user_filter_settings SET budget_max_krw = 1",
    "DELETE FROM public.user_filter_settings",
])
def test_authenticated_cannot_write_filter_settings_directly(db, sql):
    with as_user(db, U1) as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM public.user_filter_save_requests",
    "INSERT INTO public.user_filter_save_requests(user_id, request_id, request_input, applied_revision, result_current) "
    "VALUES (auth.uid(), gen_random_uuid(), '{}', 1, '{}')",
    "DELETE FROM public.user_filter_save_requests",
])
def test_authenticated_has_no_access_to_the_request_ledger(db, sql):
    with as_user(db, U1) as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute(sql)


def test_anon_has_no_access_to_ledger_or_filter_settings(db):
    for t in ("public.user_filter_save_requests", "public.user_filter_settings"):
        with as_user(db, None, role="anon") as c, pytest.raises(errors.InsufficientPrivilege):
            c.execute(f"SELECT * FROM {t}")


def test_authenticated_reads_only_its_own_settings_row(db):
    db.execute("INSERT INTO public.user_filter_settings(user_id, budget_max_krw) VALUES (%s, 1), (%s, 2)", (U1, U2))
    with as_user(db, U1) as c:
        assert c.execute("SELECT budget_max_krw FROM public.user_filter_settings").fetchall() == [(1,)]


@pytest.mark.parametrize("cols,vals", [
    ("min_area_sqm, max_area_sqm", "100, 50"),
    ("revision", "0"),
    ("budget_max_krw", "-1"),
    ("min_area_sqm", "0"),
])
def test_table_check_constraints_still_protect_owner_writes(db, cols, vals):
    """RPC 검증과 별개로 테이블 CHECK가 최종 방어선으로 남아 있다(소유자·서비스 키 쓰기도 막는다)."""
    with pytest.raises(errors.CheckViolation):
        db.execute(f"INSERT INTO public.user_filter_settings(user_id, {cols}) VALUES (%s, {vals})", (U1,))


# ---------- 동시성 ----------

def _worker(url, user, rid, exp, budget, out, i, barrier):
    with psycopg.connect(url) as c:                              # 트랜잭션 연결(커밋해야 다른 연결에 보인다)
        c.execute("SET LOCAL ROLE authenticated")
        c.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (user,))
        barrier.wait(timeout=20)
        try:
            out[i] = c.execute(CALL, args(rid=rid, exp=exp, budget=budget)).fetchone()[0]
            c.commit()
        except Exception as e:  # 테스트 단언에서 드러내기 위해 보관
            out[i] = e
            c.rollback()


def _race(url, user, jobs):
    out, barrier = [None] * len(jobs), threading.Barrier(len(jobs))
    ts = [threading.Thread(target=_worker, args=(url, user, rid, exp, budget, out, i, barrier)) for i, (rid, exp, budget) in enumerate(jobs)]
    [t.start() for t in ts]
    [t.join(timeout=40) for t in ts]
    return out


def test_concurrent_first_inserts_yield_exactly_one_success(db):
    url = os.environ["TEST_DATABASE_URL"]
    jobs = [(str(uuid.uuid4()), 0, 1000 + i) for i in range(8)]
    out = _race(url, U1, jobs)
    assert all(isinstance(o, dict) for o in out), out
    saved = [o for o in out if o["status"] == "saved"]
    assert len(saved) == 1 and saved[0]["applied_revision"] == 1
    assert all(o["status"] == "conflict" and o["current"]["revision"] == 1 for o in out if o["status"] != "saved")
    assert db.execute("SELECT revision FROM public.user_filter_settings WHERE user_id=%s", (U1,)).fetchone()[0] == 1
    assert count_requests(db, U1) == 1


def test_concurrent_updates_with_same_expected_revision_yield_one_success(db):
    db.execute("INSERT INTO public.user_filter_settings(user_id, revision) VALUES (%s, 3)", (U1,))
    out = _race(os.environ["TEST_DATABASE_URL"], U1, [(str(uuid.uuid4()), 3, 5000 + i) for i in range(8)])
    assert all(isinstance(o, dict) for o in out), out
    assert sum(o["status"] == "saved" for o in out) == 1
    assert db.execute("SELECT revision FROM public.user_filter_settings WHERE user_id=%s", (U1,)).fetchone()[0] == 4


def test_concurrent_retries_of_the_same_request_increment_only_once(db):
    rid = str(uuid.uuid4())
    out = _race(os.environ["TEST_DATABASE_URL"], U1, [(rid, 0, 42)] * 6)       # 원 요청과 재시도가 동시에 도착
    assert all(isinstance(o, dict) and o["status"] == "saved" for o in out), out
    assert sorted(o["replayed"] for o in out) == [False] + [True] * 5
    assert db.execute("SELECT revision FROM public.user_filter_settings WHERE user_id=%s", (U1,)).fetchone()[0] == 1
    assert count_requests(db, U1) == 1


# ---------- 요청 기록 정리(E3) ----------

def test_cleanup_removes_only_requests_older_than_the_retention_period(db):
    db.execute("INSERT INTO public.user_filter_settings(user_id, revision) VALUES (%s, 1)", (U1,))
    old, recent = str(uuid.uuid4()), str(uuid.uuid4())
    for rid, age in ((old, "8 days"), (recent, "6 days")):
        db.execute("INSERT INTO public.user_filter_save_requests(user_id, request_id, request_input, applied_revision, result_current, created_at) "
                   "VALUES (%s, %s, '{}', 1, '{}', now() - %s::interval)", (U1, rid, age))
    assert cleanup_filter_save_requests(db) == 1                         # 기본 7일
    left = [r[0] for r in db.execute("SELECT request_id::text FROM public.user_filter_save_requests WHERE user_id=%s", (U1,)).fetchall()]
    assert left == [recent]
    assert db.execute("SELECT revision FROM public.user_filter_settings WHERE user_id=%s", (U1,)).fetchone()[0] == 1   # 설정은 건드리지 않는다
    assert cleanup_filter_save_requests(db) == 0                         # 다시 실행해도 안전


def test_an_expired_request_id_is_no_longer_replayed_but_cannot_double_apply(db):
    """보존 기간이 지난 ID로 재시도하면 재생되지 않는다: 같은 입력이면 예상 revision이 오래돼 conflict가 되어 이중 반영은 일어나지 않는다."""
    rid = str(uuid.uuid4())
    # as_user 는 항상 롤백하므로, 커밋된 상태를 만들려고 트랜잭션을 직접 연다
    db.autocommit = False
    try:
        db.execute("SET LOCAL ROLE authenticated")
        db.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (U1,))
        save(db, exp=0, rid=rid, budget=100)
        db.commit()
    finally:
        db.autocommit = True
    db.execute("UPDATE public.user_filter_save_requests SET created_at = now() - interval '8 days' WHERE user_id=%s", (U1,))
    assert cleanup_filter_save_requests(db) == 1
    with as_user(db, U1) as c:
        r = save(c, exp=0, rid=rid, budget=100)                          # 만료된 ID 재시도
        assert r["status"] == "conflict" and r["current"]["revision"] == 1
        assert c.execute("SELECT revision FROM public.user_filter_settings").fetchone()[0] == 1
