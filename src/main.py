"""수집 진입점. 환경 변수: DATA_GO_KR_API_KEY, DATABASE_URL(로컬/테스트용 PostgreSQL 접속 문자열).

GitHub Actions용 Supabase REST(RPC) 저장소는 미구현이다. DATABASE_URL이 없으면 명확히 실패한다.
사용: python src/main.py [--mode daily|backfill] [--since YYYY-MM-DD]
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from moahome.client import CheongyakClient, normalize_key  # noqa: E402
from moahome.store import PgStore  # noqa: E402
from moahome.sync import run_sync  # noqa: E402

KST = timezone(timedelta(hours=9))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "backfill"], default="daily")
    ap.add_argument("--since", type=date.fromisoformat, default=None)
    a = ap.parse_args()
    key, dsn = os.environ.get("DATA_GO_KR_API_KEY", ""), os.environ.get("DATABASE_URL", "")
    if not key:
        sys.exit("DATA_GO_KR_API_KEY 미설정")
    if not dsn:
        sys.exit("DATABASE_URL 미설정: Supabase RPC 저장소는 아직 구현되지 않음(설계 결정 대기)")
    store = PgStore(dsn)
    try:
        result = run_sync(CheongyakClient(normalize_key(key)), store, datetime.now(KST).date(),
                          mode=a.mode, since=a.since)
    finally:
        store.close()
    print(f"status={result['status']} failures={result['failure_count']} totals={result['totals']}")
    sys.exit(0 if result["status"] == "succeeded" else 1)


if __name__ == "__main__":
    main()
