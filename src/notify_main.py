"""알림 발송 진입점. 환경 변수: DATABASE_URL, VAPID_PRIVATE_KEY, VAPID_SUBJECT.

실행 시각은 보장되지 않는다(스케줄 지연 가능). 같은 날 여러 번 실행해도 중복 발송하지 않는다.
사용: python src/notify_main.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg  # noqa: E402

from moahome.notify import cleanup_expired, make_webpush_sender, run_reminders  # noqa: E402

KST = timezone(timedelta(hours=9))


def main():
    dsn, priv, subject = os.environ.get("DATABASE_URL", ""), os.environ.get("VAPID_PRIVATE_KEY", ""), os.environ.get("VAPID_SUBJECT", "")
    if not (dsn and priv and subject):
        sys.exit("DATABASE_URL, VAPID_PRIVATE_KEY, VAPID_SUBJECT가 필요합니다")
    with psycopg.connect(dsn, autocommit=True) as conn:
        stats = run_reminders(conn, datetime.now(KST).date(), make_webpush_sender(priv, subject))
        stats["cleaned"] = cleanup_expired(conn)
    print(" ".join(f"{k}={v}" for k, v in stats.items()))
    sys.exit(1 if stats["failed"] else 0)


if __name__ == "__main__":
    main()
