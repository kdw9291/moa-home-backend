"""수집 데이터 품질 점검 진입점(읽기 전용). 환경 변수: DATABASE_URL.

사용: python src/quality_main.py [--samples N]
DB를 바꾸지 않는다(READ ONLY 트랜잭션). 출력에는 접속 정보가 들어가지 않는다.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg  # noqa: E402

from moahome.quality import format_report, overview, run_checks  # noqa: E402

KST = timezone(timedelta(hours=9))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=5, help="항목별 표본 수(기본 5)")
    args = ap.parse_args()
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        sys.exit("DATABASE_URL이 필요합니다")
    today = datetime.now(KST).date()
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        report = format_report(today, overview(conn), run_checks(conn, today, args.samples))
        conn.rollback()
    sys.stdout.reconfigure(encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
