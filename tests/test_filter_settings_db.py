"""로그인 상태 필터 저장: 프런트가 보내는 upsert와 같은 SQL을 회원 권한(RLS)으로 실행해 저장·갱신·격리·제약을 검증한다."""
import os
import uuid

import pytest
from psycopg import errors

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL 미설정: DB 검증 미실행")
U1, U2 = str(uuid.uuid4()), str(uuid.uuid4())

UPSERT = """
INSERT INTO public.user_filter_settings(user_id, preferred_region_codes, budget_max_krw, min_area_sqm, max_area_sqm,
                                        housing_families, qualification_preferences, revision)
VALUES (%(u)s, %(regions)s, %(budget)s, %(mn)s, %(mx)s, %(fam)s, %(q)s, %(rev)s)
ON CONFLICT (user_id) DO UPDATE SET preferred_region_codes=EXCLUDED.preferred_region_codes, budget_max_krw=EXCLUDED.budget_max_krw,
    min_area_sqm=EXCLUDED.min_area_sqm, max_area_sqm=EXCLUDED.max_area_sqm, housing_families=EXCLUDED.housing_families,
    qualification_preferences=EXCLUDED.qualification_preferences, revision=EXCLUDED.revision
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


def test_owner_can_insert_then_update_the_same_row_with_typed_values(db):
    with as_user(db, U1) as c:
        c.execute(UPSERT, row(U1))
        c.execute(UPSERT, row(U1, budget=500_000_000, mn=59, rev=2, regions=["100", "410"], q=[]))   # 같은 키로 갱신
        r = c.execute("SELECT preferred_region_codes, budget_max_krw, min_area_sqm, max_area_sqm, housing_families, "
                      "qualification_preferences, revision FROM public.user_filter_settings").fetchall()
    assert len(r) == 1
    regions, budget, mn, mx, fam, q, rev = r[0]
    assert regions == ["100", "410"] and budget == 500_000_000 and float(mn) == 59.0 and float(mx) == 84.0
    assert fam == ["apt"] and q == [] and rev == 2


def test_area_boundary_tokens_round_trip_in_qualification_preferences(db):
    with as_user(db, U1) as c:
        c.execute(UPSERT, row(U1, q=["_ui:min_excl", "_ui:max_excl", "_ui:unit:pyeong"]))
        (q,) = c.execute("SELECT qualification_preferences FROM public.user_filter_settings").fetchone()
    assert q == ["_ui:min_excl", "_ui:max_excl", "_ui:unit:pyeong"]


def test_other_user_cannot_read_change_delete_or_take_over_the_row(db):
    db.execute(UPSERT, row(U1))                      # 소유자 행을 관리자 권한으로 시드
    with as_user(db, U2) as c:
        assert c.execute("SELECT count(*) FROM public.user_filter_settings").fetchone()[0] == 0     # 읽기 불가
        assert c.execute("UPDATE public.user_filter_settings SET budget_max_krw=1 WHERE user_id=%s", (U1,)).rowcount == 0
        assert c.execute("DELETE FROM public.user_filter_settings WHERE user_id=%s", (U1,)).rowcount == 0
    with as_user(db, U2) as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute(UPSERT, row(U1, budget=1, rev=9))  # 타인 명의로 upsert(WITH CHECK 위반)
    assert db.execute("SELECT budget_max_krw FROM public.user_filter_settings WHERE user_id=%s", (U1,)).fetchone()[0] == 600_000_000


def test_rls_works_with_jwt_claims_json_too(db):
    """새 PostgREST는 request.jwt.claims(JSON)로 claim을 전달한다. 실제 Supabase auth.uid()는 두 방식을 모두 읽는다."""
    db.autocommit = False
    try:
        db.execute("SET LOCAL ROLE authenticated")
        db.execute("SELECT set_config('request.jwt.claims', %s, true)", ('{"sub": "%s", "role": "authenticated"}' % U1,))
        db.execute(UPSERT, row(U1))
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
    """프런트가 이런 값을 보내면 저장이 실패한다: 화면은 저장 실패를 알리거나 이런 값을 보내지 않아야 한다."""
    with as_user(db, U1) as c, pytest.raises(errors.CheckViolation):
        c.execute(UPSERT, row(U1, **bad))
