"""증분 마이그레이션(sql/migrations) 검증: 구스키마 + E1-A + E1-B 의 결과가 신규 스키마(docs/DATABASE_SCHEMA.sql)와 같고,
기존 데이터가 보존되며, 여러 번 실행해도 안전한지 확인한다. 별도의 일회용 DB(moa_test_mig_*)를 만들어 쓰고 끝나면 지운다.
"""
import glob
import os
import re
import uuid

import psycopg
import psycopg.conninfo
import pytest
from psycopg import errors

import conftest as base

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL 미설정: DB 검증 미실행")
MIG_DIR = os.path.normpath(os.path.join(base.HERE, "..", "sql", "migrations"))
U1 = str(uuid.uuid4())


def migration(name_part):
    (p,) = glob.glob(os.path.join(MIG_DIR, f"*{name_part}*.sql"))
    return open(p, encoding="utf-8").read()


def legacy_schema():
    """현재 docs 스키마에서 RPC 설계(요청 기록 테이블·함수·권한 회수)를 걷어내 '변경 전 스키마'를 복원한다."""
    s = open(base.SCHEMA, encoding="utf-8").read()
    def sub(pattern, repl, flags=0):
        nonlocal s
        s, k = re.subn(pattern, repl, s, flags=flags)
        assert k >= 1, f"legacy 변환 실패: {pattern[:50]}"

    sub(r"-- Successful RPC calls are recorded.*?ON public\.user_filter_save_requests\(created_at\);\n", "", re.S)
    sub(r"ALTER TABLE public\.user_filter_save_requests ENABLE ROW LEVEL SECURITY;\n", "")
    sub(r" public\.user_filter_save_requests,", "")
    sub(r"GRANT SELECT ON public\.user_filter_settings TO authenticated;\nREVOKE INSERT, UPDATE, DELETE ON public\.user_filter_settings FROM authenticated;\n"
        r"GRANT SELECT, INSERT, UPDATE, DELETE ON public\.user_bookmarks,\n    public\.push_subscriptions TO authenticated;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON public.user_filter_settings, public.user_bookmarks,\n    public.push_subscriptions TO authenticated;")
    sub(r"CREATE POLICY own_filter ON public\.user_filter_settings\n    FOR SELECT TO authenticated USING \(\(SELECT auth\.uid\(\)\) = user_id\);",
        "CREATE POLICY own_filter ON public.user_filter_settings\n    FOR ALL TO authenticated USING ((SELECT auth.uid()) = user_id)\n    WITH CHECK ((SELECT auth.uid()) = user_id);")
    a = s.index("-- Only the trusted function owner")
    b = s.index("TO authenticated;", s.index("GRANT EXECUTE ON FUNCTION public.save_user_filter_settings(")) + len("TO authenticated;")
    s = s[:a] + s[b:]
    assert "save_user_filter_settings" not in s and "user_filter_save_requests" not in s
    return s


def snapshot(c):
    q = lambda sql: c.execute(sql).fetchall()
    return {
        "policies": q("SELECT tablename, policyname, cmd, roles::text, qual, with_check FROM pg_policies WHERE schemaname='public' ORDER BY 1,2"),
        "grants": q("SELECT table_name, grantee, privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_schema='public' AND grantee IN ('anon','authenticated','service_role','PUBLIC') ORDER BY 1,2,3"),
        "rls": q("SELECT relname, relrowsecurity FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r' ORDER BY 1"),
        "function": q("SELECT pg_get_functiondef(p.oid), p.proacl::text, p.prosecdef, p.proconfig::text "
                      "FROM pg_proc p WHERE p.pronamespace='public'::regnamespace AND p.proname='save_user_filter_settings'"),
        "ledger_columns": q("SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name='user_filter_save_requests' ORDER BY ordinal_position"),
        "ledger_indexes": q("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename='user_filter_save_requests' ORDER BY 1"),
        "ledger_constraints": q("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                                "WHERE conrelid='public.user_filter_save_requests'::regclass ORDER BY 1"),
    }


@pytest.fixture(scope="module")
def admin_url():
    return os.environ["TEST_DATABASE_URL"]


@pytest.fixture()
def fresh_db(admin_url):
    """일회용 DB를 만들어 연결을 돌려주고, 끝나면 지운다. 역할(anon 등)은 클러스터 전역이라 SHIM이 이미 만들었다."""
    name = "moa_test_mig_" + uuid.uuid4().hex[:8]
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    url = psycopg.conninfo.make_conninfo(admin_url, dbname=name)
    conn = psycopg.connect(url, autocommit=True)
    conn.execute(base.SHIM)
    try:
        yield conn
    finally:
        conn.close()
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def run_script(c, sql):
    try:
        c.execute(sql)
    except Exception:
        c.execute("ROLLBACK")      # 스크립트가 BEGIN 뒤에 실패하면 트랜잭션이 열린 채 남는다
        raise


@pytest.fixture()
def legacy(fresh_db):
    fresh_db.execute(legacy_schema())
    return fresh_db


def can(c, role, sql, sub=None):
    c.execute("BEGIN")
    try:
        c.execute(f"SET LOCAL ROLE {role}")
        if sub:
            c.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (sub,))
        c.execute(sql)
        return True
    except errors.InsufficientPrivilege:
        return False
    finally:
        c.execute("ROLLBACK")


def test_legacy_schema_has_the_old_shape(legacy):
    assert legacy.execute("SELECT to_regprocedure('public.save_user_filter_settings(text[], bigint, numeric, numeric, text[], text[], integer, uuid)')").fetchone()[0] is None
    assert legacy.execute("SELECT to_regclass('public.user_filter_save_requests')").fetchone()[0] is None
    legacy.execute("INSERT INTO auth.users(id) VALUES (%s)", (U1,))
    assert can(legacy, "authenticated", "INSERT INTO public.user_filter_settings(user_id) VALUES (auth.uid())", U1)   # 구버전 직접 쓰기 가능


def test_add_step_keeps_direct_writes_for_old_clients_and_adds_the_rpc(legacy):
    legacy.execute("INSERT INTO auth.users(id) VALUES (%s)", (U1,))
    legacy.execute("INSERT INTO public.user_filter_settings(user_id, budget_max_krw, revision) VALUES (%s, 123, 5)", (U1,))
    run_script(legacy, migration("_add"))
    # 추가 단계만으로는 구버전 클라이언트가 계속 동작한다
    assert can(legacy, "authenticated", "UPDATE public.user_filter_settings SET budget_max_krw=1", U1)
    # RPC는 기존 행의 revision(5)을 그대로 이어받는다
    legacy.execute("BEGIN")
    legacy.execute("SET LOCAL ROLE authenticated")
    legacy.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (U1,))
    r = legacy.execute("SELECT public.save_user_filter_settings(ARRAY['410'], 456, NULL, NULL, ARRAY[]::text[], ARRAY[]::text[], 5, gen_random_uuid())").fetchone()[0]
    legacy.execute("ROLLBACK")
    assert r["status"] == "saved" and r["applied_revision"] == 6 and r["current"]["budget_max_krw"] == "456"


def test_revoke_step_blocks_direct_writes_but_keeps_data_and_select(legacy):
    legacy.execute("INSERT INTO auth.users(id) VALUES (%s)", (U1,))
    legacy.execute("INSERT INTO public.user_filter_settings(user_id, budget_max_krw, revision) VALUES (%s, 123, 5)", (U1,))
    run_script(legacy, migration("_add"))
    run_script(legacy, migration("_revoke"))
    assert not can(legacy, "authenticated", "UPDATE public.user_filter_settings SET budget_max_krw=1", U1)
    assert not can(legacy, "authenticated", "INSERT INTO public.user_filter_settings(user_id) VALUES (auth.uid())", U1)
    assert not can(legacy, "authenticated", "DELETE FROM public.user_filter_settings", U1)
    assert can(legacy, "authenticated", "SELECT * FROM public.user_filter_settings", U1)
    assert legacy.execute("SELECT budget_max_krw, revision FROM public.user_filter_settings").fetchone() == (123, 5)   # 기존 행 보존


def test_migrations_are_idempotent(legacy):
    for _ in range(2):
        run_script(legacy, migration("_add"))
    for _ in range(2):
        run_script(legacy, migration("_revoke"))
    run_script(legacy, migration("_add"))            # 회수 뒤에 추가 단계를 다시 실행해도 직접 쓰기 회수 상태를 건드리지 않는다
    assert not can(legacy, "authenticated", "DELETE FROM public.user_filter_settings", None)


def test_migrated_legacy_equals_fresh_schema(admin_url, fresh_db):
    fresh_db.execute(legacy_schema())
    run_script(fresh_db, migration("_add"))
    run_script(fresh_db, migration("_revoke"))
    migrated = snapshot(fresh_db)
    name = "moa_test_mig_" + uuid.uuid4().hex[:8]
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    try:
        with psycopg.connect(psycopg.conninfo.make_conninfo(admin_url, dbname=name), autocommit=True) as c2:
            c2.execute(base.SHIM)
            c2.execute(open(base.SCHEMA, encoding="utf-8").read())
            fresh = snapshot(c2)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    assert fresh["function"], "신규 스키마에 함수가 없다"
    for key in fresh:
        assert migrated[key] == fresh[key], f"신규 스키마와 마이그레이션 결과가 다르다: {key}"


def test_revoke_step_refuses_to_run_without_the_add_step(legacy):
    with pytest.raises(errors.RaiseException):
        run_script(legacy, migration("_revoke"))
    assert can(legacy, "authenticated", "SELECT 1 FROM public.user_filter_settings", None)   # 아무것도 바뀌지 않았다


def test_add_step_refuses_a_role_that_cannot_bypass_rls(legacy):
    legacy.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='moa_mig_plain') THEN CREATE ROLE moa_mig_plain NOLOGIN; END IF; END $$")
    legacy.execute("GRANT CREATE ON SCHEMA public TO moa_mig_plain")
    legacy.execute("GRANT moa_mig_plain TO CURRENT_USER")
    try:
        legacy.execute("SET ROLE moa_mig_plain")
        with pytest.raises(errors.RaiseException):
            run_script(legacy, migration("_add"))
    finally:
        legacy.execute("RESET ROLE")
    assert legacy.execute("SELECT to_regclass('public.user_filter_save_requests')").fetchone()[0] is None
