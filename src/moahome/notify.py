"""북마크한 공고의 접수 시작일(KST) 당일 알림 발송.

규칙(docs/PRD.md 5절, backend/CLAUDE.md 7항):
- 회원의 북마크 + 기기별 활성 구독만 대상. 발송 전에 중복키를 DB에 먼저 확보(claim)한다.
- 중복키: (구독, 공고, 'receipt_start', 접수 시작일). 같은 날 접수 이벤트가 여러 개여도 구독당 공고당 한 번.
- 정시 도착·전달 성공·개인 자격을 보장하는 문구를 쓰지 않는다.
- 만료(404/410) 구독은 비활성화하고, 오래 비활성인 구독은 별도 정리 함수로 삭제한다.
- 오류 기록에는 예외 종류와 상태 코드만 남긴다(엔드포인트 URL은 개인 식별 가능 값이라 로그 금지).
"""
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# 접수 시작 알림 대상 이벤트(백엔드 매퍼가 원천 필드 접두어에서 딴 임시 코드 중 접수 계열)
RECEIPT_CODES = ["rcept", "spsply_rcept", "gnrl_rcept", "subscrpt_rcept", "gnrl_rnk1", "gnrl_rnk2"]
EVENT_CODE = "receipt_start"

# 알림 문구에 넣을 접수 단계 이름(표시 순서 포함). 이벤트 코드는 원천 필드 접두어에서 딴 임시 이름이라
# 화면(frontend eventLabel)과 같은 중립 표현만 쓴다.
STAGE_ORDER = ["spsply_rcept", "gnrl_rnk1", "gnrl_rnk2", "gnrl_rcept", "rcept", "subscrpt_rcept"]
STAGE_LABEL = {
    "spsply_rcept": "특별공급 접수", "gnrl_rnk1": "1순위 접수", "gnrl_rnk2": "2순위 접수",
    "gnrl_rcept": "일반공급 접수", "rcept": "청약 접수", "subscrpt_rcept": "청약 접수",
}
RETRY_AFTER_MINUTES = 10
SEND_TIMEOUT_SECONDS = 30


@dataclass
class Target:
    subscription_id: str
    endpoint: str
    p256dh: str
    auth_secret: str
    announcement_id: str
    house_nm: str
    family: str
    house_manage_no: str
    pblanc_no: str
    event_on: date | None = None  # 접수 시작일(KST). 전날 실패분 재시도 대상은 오늘이 아니다
    stages: tuple[str, ...] = ()  # 그날 시작하는 접수 이벤트 코드(문구에 단계 이름으로 사용)
    claimed_at: datetime | None = None  # claim 소유 표지: 이 값과 같은 행만 finish가 갱신한다


def build_payload(t: Target, today: date | None = None) -> dict:
    # 시각·자격·당첨 가능성을 약속하지 않는다. 접수 조건은 공식 공고에서 확인하도록 안내.
    carried = today is not None and t.event_on is not None and t.event_on < today
    names: list[str] = []
    for code in sorted(t.stages, key=lambda c: STAGE_ORDER.index(c) if c in STAGE_ORDER else 99):
        label = STAGE_LABEL.get(code, "접수")
        if label not in names:
            names.append(label)
    stage_text = ", ".join(names) if names else "접수"
    lead = f"{stage_text}가 시작된 관심 공고입니다." if carried else f"오늘부터 {stage_text} 일정이 있습니다."
    return {
        "title": "관심 공고 접수 단계 시작",
        "body": f"{t.house_nm} — {lead} 접수 시간과 조건은 공식 공고에서 확인하세요.",
        "url": f"/detail/?f={t.family}&h={t.house_manage_no}&p={t.pblanc_no}",
        "tag": f"receipt-{t.announcement_id}",
    }


def due_targets(conn, today) -> list[Target]:
    rows = conn.execute(
        """
        SELECT s.id, s.endpoint, s.p256dh, s.auth_secret, a.id, a.house_nm, a.source_family, a.house_manage_no, a.pblanc_no,
               (SELECT array_agg(DISTINCT e2.source_event_code) FROM public.announcement_events e2
                 WHERE e2.announcement_id = a.id AND e2.source_event_code = ANY(%s) AND e2.starts_on = %s)
        FROM public.user_bookmarks b
        JOIN public.cheongyak_announcements a ON a.id = b.announcement_id
        JOIN public.push_subscriptions s ON s.user_id = b.user_id AND s.enabled
        WHERE EXISTS (
            SELECT 1 FROM public.announcement_events e
            WHERE e.announcement_id = a.id AND e.source_event_code = ANY(%s) AND e.starts_on = %s
        )
        ORDER BY s.id, a.id
        """,
        (RECEIPT_CODES, today, RECEIPT_CODES, today),
    ).fetchall()
    return [Target(str(r[0]), r[1], r[2], r[3], str(r[4]), r[5], r[6], r[7], r[8], event_on=today, stages=tuple(r[9] or ())) for r in rows]


def carryover_targets(conn, today) -> list[Target]:
    """어제 시작일이었는데 실패·중단으로 끝내지 못한 발송을 다음 날에도 재시도한다(1일 한정).

    북마크와 활성 구독이 아직 있고 공고의 접수 시작일이 그대로일 때만 대상이다.
    """
    y = today - timedelta(days=1)
    rows = conn.execute(
        """
        SELECT s.id, s.endpoint, s.p256dh, s.auth_secret, a.id, a.house_nm, a.source_family, a.house_manage_no, a.pblanc_no,
               (SELECT array_agg(DISTINCT e2.source_event_code) FROM public.announcement_events e2
                 WHERE e2.announcement_id = a.id AND e2.source_event_code = ANY(%s) AND e2.starts_on = %s)
        FROM public.notification_deliveries d
        JOIN public.push_subscriptions s ON s.id = d.subscription_id AND s.enabled
        JOIN public.cheongyak_announcements a ON a.id = d.announcement_id
        JOIN public.user_bookmarks b ON b.user_id = s.user_id AND b.announcement_id = a.id
        WHERE d.source_event_code = %s AND d.event_on = %s AND d.status IN ('failed', 'pending')
          AND EXISTS (SELECT 1 FROM public.announcement_events e
                      WHERE e.announcement_id = a.id AND e.source_event_code = ANY(%s) AND e.starts_on = %s)
        ORDER BY s.id, a.id
        """,
        (RECEIPT_CODES, y, EVENT_CODE, y, RECEIPT_CODES, y),
    ).fetchall()
    return [Target(str(r[0]), r[1], r[2], r[3], str(r[4]), r[5], r[6], r[7], r[8], event_on=y, stages=tuple(r[9] or ())) for r in rows]


def claim(conn, t: Target, today=None) -> bool:
    """발송 권리를 DB에서 먼저 확보한다. 이미 처리됐거나 다른 실행이 최근에 잡았으면 False.

    성공하면 t.claimed_at에 이번 claim의 시각을 기록한다(소유 표지).
    """
    on = t.event_on or today
    row = conn.execute(
        f"""
        INSERT INTO public.notification_deliveries(subscription_id, announcement_id, source_event_code, event_on, status, attempted_at)
        VALUES (%s, %s, %s, %s, 'pending', now())
        ON CONFLICT (subscription_id, announcement_id, source_event_code, event_on) DO UPDATE
            SET status = 'pending', attempted_at = now()
            WHERE public.notification_deliveries.status IN ('failed', 'pending')
              AND public.notification_deliveries.attempted_at < now() - interval '{RETRY_AFTER_MINUTES} minutes'
        RETURNING attempted_at
        """,
        (t.subscription_id, t.announcement_id, EVENT_CODE, on),
    ).fetchone()
    if row is None:
        return False
    t.claimed_at = row[0]
    return True


def finish(conn, t: Target, today, status: str) -> bool:
    """자기 claim이 아직 유효할 때만 결과를 기록한다. 그 사이 다른 실행이 재점유했으면 False(덮어쓰지 않음)."""
    on = t.event_on or today
    if status == "expired":
        # 404/410은 구독 자체가 무효라는 확인이다. claim을 잃었더라도 구독은 비활성화한다.
        conn.execute("UPDATE public.push_subscriptions SET enabled=false, updated_at=now() WHERE id=%s", (t.subscription_id,))
    n = conn.execute(
        "UPDATE public.notification_deliveries SET status=%s, attempted_at=now()"
        " WHERE subscription_id=%s AND announcement_id=%s AND source_event_code=%s AND event_on=%s"
        " AND status='pending' AND attempted_at=%s",
        (status, t.subscription_id, t.announcement_id, EVENT_CODE, on, t.claimed_at)).rowcount
    return n > 0


def make_webpush_sender(vapid_private_raw: str, subject: str):
    """pywebpush 발송기. 반환값: 'sent' | 'expired' | 'failed'. 예외 메시지는 밖으로 내보내지 않는다."""
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush

    vapid = Vapid.from_raw(vapid_private_raw.encode())

    def send(t: Target, payload: dict) -> str:
        try:
            webpush(
                subscription_info={"endpoint": t.endpoint, "keys": {"p256dh": t.p256dh, "auth": t.auth_secret}},
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=vapid,
                vapid_claims={"sub": subject},
                ttl=6 * 3600,  # 접수 시작 알림은 오래 지나면 의미가 없다
                timeout=SEND_TIMEOUT_SECONDS,  # 재점유 대기(10분)보다 훨씬 짧게 끝나야 중복 발송이 생기지 않는다
            )
            return "sent"
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            return "expired" if code in (404, 410) else "failed"
        except Exception:  # 네트워크 오류 등: 다음 실행에서 재시도
            return "failed"

    return send


def run_reminders(conn, today, sender) -> dict:
    """today: KST 날짜. sender(target, payload) -> 'sent'|'expired'|'failed'. conn은 autocommit."""
    stats = {"targets": 0, "claimed": 0, "sent": 0, "failed": 0, "expired": 0, "skipped": 0, "lost_claim": 0}
    due = due_targets(conn, today)
    seen = {(t.subscription_id, t.announcement_id) for t in due}
    # 같은 기기·공고에 오늘 알림이 이미 예정돼 있으면 어제 실패분을 따로 보내지 않는다(하루에 한 번만)
    carry = [t for t in carryover_targets(conn, today) if (t.subscription_id, t.announcement_id) not in seen]
    for t in [*due, *carry]:
        stats["targets"] += 1
        if not claim(conn, t, today):
            stats["skipped"] += 1
            continue
        stats["claimed"] += 1
        try:
            status = sender(t, build_payload(t, today))
        except Exception:
            status = "failed"
        if status not in ("sent", "expired", "failed"):
            status = "failed"
        if finish(conn, t, today, status):
            stats[status] += 1
        else:
            stats["lost_claim"] += 1  # 다른 실행이 재점유함: 결과를 덮어쓰지 않는다
    return stats


def cleanup_expired(conn, days: int = 30) -> int:
    """비활성화된 지 오래된 구독을 삭제한다(연결된 발송 기록도 함께 삭제)."""
    return conn.execute(
        "DELETE FROM public.push_subscriptions WHERE NOT enabled AND updated_at < now() - make_interval(days => %s)",
        (days,)).rowcount
