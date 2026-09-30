"""DB 테스트 공통 fixture. 일회용 테스트 DB 전용.

  TEST_DATABASE_URL   소유자/슈퍼유저 연결 문자열. DB 이름은 moa_test_* 형식이어야 한다.
  MOA_TEST_DB_CONFIRM 위 DB 이름을 그대로 다시 입력해야 스키마를 초기화한다(운영 DB 보호).
  MOA_SCHEMA_SQL      (선택) 스키마 경로. 기본 ../../docs/DATABASE_SCHEMA.sql
모듈마다 public 스키마를 DROP 후 스키마 SQL을 새로 적용한다. auth 스키마가 없으면(일반 PostgreSQL)
Supabase 흉내 shim을 만들며, 그 결과는 Supabase 실제 검증이 아니다.
"""
import os
import re

import psycopg
import psycopg.conninfo
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.environ.get(
    "MOA_SCHEMA_SQL",
    os.path.normpath(os.path.join(HERE, "..", "..", "..", "docs", "DATABASE_SCHEMA.sql")),
)

SHIM = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN CREATE ROLE service_role NOLOGIN BYPASSRLS; END IF;
END $$;
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY);
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
  $f$ SELECT coalesce(
        nullif(current_setting('request.jwt.claim.sub', true), ''),
        (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'))::uuid $f$;  -- 실제 Supabase auth.uid()와 같은 정의
GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION auth.uid() TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
"""



LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
OUR_TABLES = ["notification_deliveries", "push_subscriptions", "user_bookmarks", "user_filter_settings",
              "announcement_events", "housing_type_special_supply", "cheongyak_housing_types",
              "cheongyak_announcements", "sync_runs", "data_status"]


@pytest.fixture(scope="module")
def db_conn():
    """일회용 DB에서만 동작한다.

    로컬(127.0.0.1/localhost): DB 이름 moa_test_* + MOA_TEST_DB_CONFIRM=<DB 이름>. public 스키마를 초기화하고
      필요하면 Supabase 흉내 shim을 만든다(Supabase 실제 검증 아님).
    원격(실제 Supabase 테스트 프로젝트): MOA_TEST_SUPABASE_CONFIRM=<접속 호스트>를 그대로 다시 입력해야 한다.
      public 스키마는 지우지 않고 이 프로젝트 테이블 10개만 DROP 후 스키마 SQL을 적용한다. shim을 만들지 않고
      실제 auth.users·역할이 있어야 한다. 실사용 데이터가 있는 프로젝트에는 사용 금지.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL 미설정: DB 검증 미실행")
    host = (psycopg.conninfo.conninfo_to_dict(url).get("host") or "").strip()
    assert host and "," not in host and not host.startswith("/"),         "접속 문자열에 단일 host가 명시돼야 함(PGHOST·다중 호스트·소켓 경로 금지)"
    remote = host not in LOCAL_HOSTS
    with psycopg.connect(url, autocommit=True) as c:
        dbname = c.execute("SELECT current_database()").fetchone()[0]
        if remote:
            confirm = os.environ.get("MOA_TEST_SUPABASE_CONFIRM", "")
            assert confirm and confirm == host, "MOA_TEST_SUPABASE_CONFIRM=<접속 호스트> 필요(원격 테스트 프로젝트 확인)"
            assert c.execute("SELECT to_regclass('auth.users')").fetchone()[0], "원격 DB에 auth.users 없음: Supabase가 아님"
            for t in OUR_TABLES:
                c.execute(f"DROP TABLE IF EXISTS public.{t} CASCADE")
        else:
            assert re.fullmatch(r"moa_test_[a-z0-9_]+", dbname), f"DB 이름 '{dbname}'은 moa_test_* 형식이 아님"
            assert os.environ.get("MOA_TEST_DB_CONFIRM") == dbname, "MOA_TEST_DB_CONFIRM=<DB 이름> 필요(일회용 DB 확인)"
            c.execute("DROP SCHEMA IF EXISTS public CASCADE")
            c.execute("CREATE SCHEMA public")
            if c.execute("SELECT to_regclass('auth.users')").fetchone()[0] is None:
                c.execute(SHIM)
            c.execute("GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role")
        for role in ("anon", "authenticated", "service_role"):
            ok = c.execute(
                "SELECT pg_has_role(current_user, %s, 'MEMBER') FROM pg_roles WHERE rolname=%s", (role, role)
            ).fetchone()
            assert ok and ok[0], f"역할 {role} 없음 또는 현재 사용자가 멤버가 아님(SET ROLE 불가)"
        assert c.execute("SELECT to_regprocedure('auth.uid()')").fetchone()[0], "auth.uid() 없음: 부분 auth 설정"
        with open(SCHEMA, encoding="utf-8") as f:
            c.execute(f.read())  # 적용 SQL 오류는 여기서 실패로 드러난다
        yield c
