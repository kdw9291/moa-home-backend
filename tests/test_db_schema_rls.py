"""신규 테스트 DB에 DATABASE_SCHEMA.sql을 적용하고 제약·RLS·GRANT를 검증한다.

실행 조건과 안전장치(일회용 DB 이름·확인 변수, 스키마 초기화, auth shim)는 tests/conftest.py의 db_conn 참조.
TEST_DATABASE_URL이 없으면 전체 skip이며 검증된 것으로 간주하지 말 것.
"""
import os
import re
import uuid

import pytest
from psycopg import errors

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL 미설정: DB 검증 미실행")

USER_A, USER_B = str(uuid.uuid4()), str(uuid.uuid4())


@pytest.fixture(scope="module")
def conn(db_conn):
    db_conn.execute("INSERT INTO auth.users(id) VALUES (%s), (%s)", (USER_A, USER_B))
    db_conn.execute(
        "INSERT INTO public.cheongyak_announcements(source_family, house_manage_no, pblanc_no, house_nm)"
        " VALUES ('apt','H1','1','시드 단지')"
    )
    yield db_conn
    db_conn.execute("DELETE FROM auth.users WHERE id IN (%s, %s)", (USER_A, USER_B))  # 실제 Supabase에 테스트 사용자 잔류 방지


class as_role:
    """트랜잭션 안에서 role/JWT sub를 설정하고 항상 롤백."""

    def __init__(self, conn, role, sub=None):
        self.c, self.role, self.sub = conn, role, sub

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

    def __exit__(self, *exc):  # 예외 여부와 무관하게 롤백 후 autocommit 복원
        self.c.rollback()
        self.c.autocommit = True
        return False


def ann_id(c):
    return c.execute("SELECT id FROM public.cheongyak_announcements WHERE house_manage_no='H1'").fetchone()[0]


# ---------- anon ----------
def test_anon_reads_public_tables_only(conn):
    with as_role(conn, "anon") as c:
        assert c.execute("SELECT count(*) FROM public.cheongyak_announcements").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM public.data_status").fetchone()[0] == 1


@pytest.mark.parametrize("table", ["user_bookmarks", "user_filter_settings", "push_subscriptions",
                                   "notification_deliveries", "sync_runs"])
def test_anon_cannot_read_private_tables(conn, table):
    with as_role(conn, "anon") as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute(f"SELECT * FROM public.{table}")


def test_anon_cannot_write_announcements(conn):
    with as_role(conn, "anon") as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute("INSERT INTO public.cheongyak_announcements(source_family, house_manage_no, pblanc_no, house_nm)"
                  " VALUES ('apt','X','1','x')")


# ---------- authenticated: 본인 / 타인 ----------
def test_own_bookmark_crud_and_isolation(conn):
    aid = ann_id(conn)
    with as_role(conn, "authenticated", USER_A) as c:
        c.execute("INSERT INTO public.user_bookmarks(user_id, announcement_id) VALUES (%s,%s)", (USER_A, aid))
        assert c.execute("SELECT count(*) FROM public.user_bookmarks").fetchone()[0] == 1
        # 다른 회원 명의로 쓰기 불가 (RLS WITH CHECK 위반)
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO public.user_bookmarks(user_id, announcement_id) VALUES (%s,%s)", (USER_B, aid))


def test_other_user_cannot_see_or_change_bookmark(conn):
    aid = ann_id(conn)
    conn.execute("INSERT INTO public.user_bookmarks(user_id, announcement_id) VALUES (%s,%s)", (USER_A, aid))
    try:
        with as_role(conn, "authenticated", USER_B) as c:
            assert c.execute("SELECT count(*) FROM public.user_bookmarks").fetchone()[0] == 0
            assert c.execute("DELETE FROM public.user_bookmarks WHERE user_id=%s", (USER_A,)).rowcount == 0
            assert c.execute("UPDATE public.user_bookmarks SET created_at=now() WHERE user_id=%s",
                             (USER_A,)).rowcount == 0
    finally:
        conn.execute("DELETE FROM public.user_bookmarks")


def test_push_subscription_and_filter_isolation(conn):
    conn.execute("INSERT INTO public.push_subscriptions(user_id, endpoint, p256dh, auth_secret)"
                 " VALUES (%s,'https://push.example/test','k','s')", (USER_A,))
    conn.execute("INSERT INTO public.user_filter_settings(user_id) VALUES (%s)", (USER_A,))
    try:
        with as_role(conn, "authenticated", USER_B) as c:
            assert c.execute("SELECT count(*) FROM public.push_subscriptions").fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM public.user_filter_settings").fetchone()[0] == 0
    finally:
        conn.execute("DELETE FROM public.push_subscriptions")
        conn.execute("DELETE FROM public.user_filter_settings")


@pytest.mark.parametrize("table", ["notification_deliveries", "sync_runs"])
def test_authenticated_cannot_touch_batch_only_tables(conn, table):
    with as_role(conn, "authenticated", USER_A) as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute(f"SELECT * FROM public.{table}")


def test_authenticated_cannot_write_public_data(conn):
    with as_role(conn, "authenticated", USER_A) as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute("DELETE FROM public.cheongyak_announcements")


# ---------- service_role ----------
def test_service_role_can_write_batch_tables(conn):
    with as_role(conn, "service_role") as c:
        c.execute("INSERT INTO public.sync_runs(status) VALUES ('running')")
        assert c.execute("SELECT count(*) FROM public.sync_runs").fetchone()[0] == 1


# ---------- 제약: 고유키·NULL 가격·자연키 upsert ----------
def test_announcement_natural_key_unique_across_family(conn):
    conn.autocommit = False
    try:
        ins = ("INSERT INTO public.cheongyak_announcements(source_family, house_manage_no, pblanc_no, house_nm)"
               " VALUES (%s,'H9','1','n')")
        conn.execute(ins, ("apt",))
        conn.execute(ins, ("remndr",))  # 다른 계열은 같은 번호여도 허용
        conn.execute("SAVEPOINT s")
        with pytest.raises(errors.UniqueViolation):
            conn.execute(ins, ("apt",))
    finally:
        conn.rollback()
        conn.autocommit = True


def test_housing_type_key_price_null_and_upsert_idempotent(conn):
    aid = ann_id(conn)
    conn.autocommit = False
    try:
        # 가격이 있으면 출처 원값·단위도 함께 저장(테스트 값은 스키마 동작 확인용 가상값, 실제 단위 아님)
        up = ("INSERT INTO public.cheongyak_housing_types(announcement_id, source_model_key, house_ty,"
              " price_max_krw, price_raw, price_source_unit)"
              " VALUES (%s,'M1','084',%s,%s,%s) ON CONFLICT (announcement_id, source_model_key)"
              " DO UPDATE SET price_max_krw = EXCLUDED.price_max_krw, price_raw = EXCLUDED.price_raw,"
              " price_source_unit = EXCLUDED.price_source_unit")
        conn.execute(up, (aid, None, None, "UNKNOWN"))  # 미공개 가격은 NULL 허용
        conn.execute(up, (aid, 590_000_000, "590000000", "KRW"))
        conn.execute(up, (aid, 590_000_000, "590000000", "KRW"))   # 재실행 멱등
        n, p, raw, unit = conn.execute(
            "SELECT count(*), max(price_max_krw), max(price_raw), max(price_source_unit)"
            " FROM public.cheongyak_housing_types").fetchone()
        assert (n, p, raw, unit) == (1, 590_000_000, "590000000", "KRW")
        conn.execute("SAVEPOINT s")
        with pytest.raises(errors.CheckViolation):
            conn.execute("UPDATE public.cheongyak_housing_types SET price_max_krw=-1")
    finally:
        conn.rollback()
        conn.autocommit = True


def test_event_scope_never_null_and_date_order(conn):
    aid = ann_id(conn)
    conn.autocommit = False
    try:
        conn.execute("SAVEPOINT s1")
        with pytest.raises(errors.NotNullViolation):
            conn.execute("INSERT INTO public.announcement_events(announcement_id, source_event_code, scope_code)"
                         " VALUES (%s,'rank1',NULL)", (aid,))
        conn.execute("ROLLBACK TO s1")
        with pytest.raises(errors.CheckViolation):
            conn.execute("INSERT INTO public.announcement_events(announcement_id, source_event_code, starts_on, ends_on)"
                         " VALUES (%s,'rank1','2026-10-02','2026-10-01')", (aid,))
    finally:
        conn.rollback()
        conn.autocommit = True


def test_per_announcement_atomic_rollback(conn):
    """공고 단위 트랜잭션: 중간 실패 시 마스터·주택형·일정 모두 되돌아간다."""
    conn.autocommit = False
    try:
        conn.execute("INSERT INTO public.cheongyak_announcements(source_family, house_manage_no, pblanc_no, house_nm)"
                     " VALUES ('apt','ATOM','1','n')")
        aid = conn.execute("SELECT id FROM public.cheongyak_announcements WHERE house_manage_no='ATOM'").fetchone()[0]
        conn.execute("INSERT INTO public.cheongyak_housing_types(announcement_id, source_model_key, house_ty)"
                     " VALUES (%s,'M1','084')", (aid,))
        with pytest.raises(errors.UniqueViolation):  # 의도된 중간 실패
            conn.execute("INSERT INTO public.cheongyak_housing_types(announcement_id, source_model_key, house_ty)"
                         " VALUES (%s,'M1','084')", (aid,))
    finally:
        conn.rollback()
        conn.autocommit = True
    assert conn.execute("SELECT count(*) FROM public.cheongyak_announcements WHERE house_manage_no='ATOM'").fetchone()[0] == 0


@pytest.mark.parametrize("price,raw,unit", [
    (590_000_000, None, "KRW"),        # 원값 없음
    (590_000_000, "  ", "MANWON"),     # 공백 원값
    (590_000_000, "\t", "MANWON"),     # 탭만 있는 원값(E' \t\r\n' 제거 대상)
    (590_000_000, "\n\r", "MANWON"),   # 개행만 있는 원값
    (590_000_000, "59000", "UNKNOWN"),  # 단위 미확정인데 환산값 존재
    (None, "59000", "MANWON"),          # 환산값 없는데 확정 단위
])
def test_price_provenance_check_rejects_unverified_price(conn, price, raw, unit):
    aid = ann_id(conn)
    conn.autocommit = False
    try:
        with pytest.raises(errors.CheckViolation):
            conn.execute(
                "INSERT INTO public.cheongyak_housing_types(announcement_id, source_model_key, house_ty,"
                " price_max_krw, price_raw, price_source_unit) VALUES (%s,'P1','084',%s,%s,%s)",
                (aid, price, raw, unit))
    finally:
        conn.rollback()
        conn.autocommit = True
