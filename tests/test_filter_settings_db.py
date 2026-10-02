"""로그인 상태 필터 테이블: RLS 격리와 직접 쓰기 차단을 회원 권한으로 검증한다.

저장 경로는 RPC(public.save_user_filter_settings)뿐이다. RPC 계약 자체는 tests/test_filter_save_rpc_db.py 가 검증한다.
"""
import os
import uuid

import pytest
from psycopg import errors

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL 미설정: DB 검증 미실행")
U1, U2 = str(uuid.uuid4()), str(uuid.uuid4())

SEED = """
INSERT INTO public.user_filter_settings(user_id, preferred_region_codes, budget_max_krw, min_area_sqm, max_area_sqm,
                                        housing_families, qualification_preferences, revision)
VALUES (%(u)s, %(regions)s, %(budget)s, %(mn)s, %(mx)s, %(fam)s, %(q)s, %(rev)s)
"""


def row(u, **over):
    base = dict(u=u, regions=["410"], budget=600_000_000, mn=58.84, mx=84, fam=["apt"], q=["_ui:min_excl"], rev=1)
    base.update(over)
    return base


@pytest.fixture()
def db(db_conn):
    db_conn.execute("DELETE FROM public.user_filter_settings")
    db_conn.execute("DELETE FROM auth.users WHERE id IN (%s, %s)", (U1, U2))
    db_conn.execute("INSERT INTO auth.users(id) VALUES (%s), (%s)", (U1, U2))
    yield db_conn
    db_conn.execute("DELETE FROM public.user_filter_settings")
    db_conn.execute("DELETE FROM auth.users WHERE id IN (%s, %s)", (U1, U2))


class as_user:
    """authenticated 역할 + JWT sub로 한 트랜잭션을 실행하고 항상 롤백한다(PostgREST가 요청마다 하는 것과 같다)."""

    def __init__(self, c, sub):
        self.c, self.sub = c, sub

    def __enter__(self):
        self.c.autocommit = False
        try:
            self.c.execute("SET LOCAL ROLE authenticated")
            self.c.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (self.sub,))
        except Exception:
            self.__exit__()
            raise
        return self.c

    def __exit__(self, *exc):
        self.c.rollback()
        self.c.autocommit = True
        return False


def test_owner_reads_back_typed_values_of_its_own_row(db):
    db.execute(SEED, row(U1, q=["_ui:min_excl", "_ui:max_excl", "_ui:unit:pyeong"]))
    with as_user(db, U1) as c:
        r = c.execute("SELECT preferred_region_codes, budget_max_krw, min_area_sqm, max_area_sqm, housing_families, "
                      "qualification_preferences, revision FROM public.user_filter_settings").fetchall()
    assert len(r) == 1
    regions, budget, mn, mx, fam, q, rev = r[0]
    assert regions == ["410"] and budget == 600_000_000 and float(mn) == 58.84 and float(mx) == 84.0
    assert fam == ["apt"] and q == ["_ui:min_excl", "_ui:max_excl", "_ui:unit:pyeong"] and rev == 1


def test_other_user_cannot_read_change_or_delete_the_row(db):
    db.execute(SEED, row(U1))
    with as_user(db, U2) as c:
        assert c.execute("SELECT count(*) FROM public.user_filter_settings").fetchone()[0] == 0     # 읽기 불가(RLS)
    for sql in ("UPDATE public.user_filter_settings SET budget_max_krw=1 WHERE user_id=%s",
                "DELETE FROM public.user_filter_settings WHERE user_id=%s"):
        with as_user(db, U2) as c, pytest.raises(errors.InsufficientPrivilege):                    # 쓰기 권한 자체가 없다
            c.execute(sql, (U1,))
    assert db.execute("SELECT budget_max_krw FROM public.user_filter_settings WHERE user_id=%s", (U1,)).fetchone()[0] == 600_000_000


def test_authenticated_cannot_insert_directly_even_for_itself(db):
    with as_user(db, U1) as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute(SEED, row(U1))
    with as_user(db, U2) as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute(SEED, row(U1, budget=1, rev=9))      # 타인 명의 주입도 권한 단계에서 막힌다


def test_rls_works_with_jwt_claims_json_too(db):
    """새 PostgREST는 request.jwt.claims(JSON)로 claim을 전달한다. 실제 Supabase auth.uid()는 두 방식을 모두 읽는다."""
    db.execute(SEED, row(U1))
    db.autocommit = False
    try:
        db.execute("SET LOCAL ROLE authenticated")
        db.execute("SELECT set_config('request.jwt.claims', %s, true)", ('{"sub": "%s", "role": "authenticated"}' % U1,))
        assert db.execute("SELECT count(*) FROM public.user_filter_settings").fetchone()[0] == 1
        db.execute("SELECT set_config('request.jwt.claims', %s, true)", ('{"sub": "%s"}' % U2,))
        assert db.execute("SELECT count(*) FROM public.user_filter_settings").fetchone()[0] == 0   # 다른 사용자로 보이면 0건
    finally:
        db.rollback()
        db.autocommit = True


def test_anon_cannot_touch_filter_settings(db):
    db.autocommit = False
    try:
        db.execute("SET LOCAL ROLE anon")
        with pytest.raises(errors.InsufficientPrivilege):
            db.execute("SELECT * FROM public.user_filter_settings")
    finally:
        db.rollback()
        db.autocommit = True


@pytest.mark.parametrize("bad,label", [
    (dict(mn=100, mx=50), "최소 면적이 최대보다 큼"),
    (dict(rev=0), "개정 번호 0"),
    (dict(budget=-1), "음수 예산"),
    (dict(mn=0), "0 이하 면적"),
])
def test_invalid_values_are_rejected_by_check_constraints(db, bad, label):
    """RPC 검증과 별개로 테이블 CHECK가 최종 방어선이다(소유자·서비스 키 쓰기도 막는다)."""
    with pytest.raises(errors.CheckViolation):
        db.execute(SEED, row(U1, **bad))
