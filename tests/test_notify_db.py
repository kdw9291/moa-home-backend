"""북마크 접수일 알림: 대상 선정·중복 방지·재시도·만료 정리를 실제 PostgreSQL에서 검증한다(가짜 발송기)."""
import os
import uuid
from datetime import date

import pytest

from moahome.notify import EVENT_CODE, Target, build_payload, cleanup_expired, run_reminders

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL 미설정: DB 검증 미실행")
TODAY = date(2026, 9, 29)
U1, U2 = str(uuid.uuid4()), str(uuid.uuid4())


@pytest.fixture()
def db(db_conn):
    for t in ("notification_deliveries", "push_subscriptions", "user_bookmarks", "announcement_events", "cheongyak_announcements"):
        db_conn.execute(f"DELETE FROM public.{t}")
    db_conn.execute("DELETE FROM auth.users WHERE id IN (%s, %s)", (U1, U2))
    db_conn.execute("INSERT INTO auth.users(id) VALUES (%s), (%s)", (U1, U2))
    yield db_conn
    for t in ("notification_deliveries", "push_subscriptions", "user_bookmarks", "announcement_events", "cheongyak_announcements"):
        db_conn.execute(f"DELETE FROM public.{t}")
    db_conn.execute("DELETE FROM auth.users WHERE id IN (%s, %s)", (U1, U2))


def ann(c, name, events, no="1"):
    aid = c.execute(
        "INSERT INTO public.cheongyak_announcements(source_family, house_manage_no, pblanc_no, house_nm) VALUES ('apt',%s,%s,%s) RETURNING id",
        (no, no, name)).fetchone()[0]
    for code, scope, s, e in events:
        c.execute("INSERT INTO public.announcement_events(announcement_id, source_event_code, scope_code, starts_on, ends_on) VALUES (%s,%s,%s,%s,%s)",
                  (aid, code, scope, s, e))
    return aid


def sub(c, user, ep="https://push.example/sub", enabled=True):
    return c.execute(
        "INSERT INTO public.push_subscriptions(user_id, endpoint, p256dh, auth_secret, enabled) VALUES (%s,%s,'k','a',%s) RETURNING id",
        (user, f"{ep}/{uuid.uuid4()}", enabled)).fetchone()[0]


def bookmark(c, user, aid):
    c.execute("INSERT INTO public.user_bookmarks(user_id, announcement_id) VALUES (%s,%s)", (user, aid))


class Sender:
    def __init__(self, result="sent"):
        self.result, self.calls = result, []

    def __call__(self, t: Target, payload: dict) -> str:
        self.calls.append((t, payload))
        return self.result


def rows(c):
    return c.execute("SELECT status, source_event_code, event_on FROM public.notification_deliveries ORDER BY status").fetchall()


def test_bookmarked_receipt_start_today_is_sent_once_per_subscription(db):
    a = ann(db, "오늘 접수", [("rcept", "all", "2026-09-29", "2026-09-30")])
    s = Sender()
    sub(db, U1); sub(db, U1)               # 기기 2대
    bookmark(db, U1, a)
    st = run_reminders(db, TODAY, s)
    assert st["sent"] == 2 and len(s.calls) == 2
    assert rows(db) == [("sent", EVENT_CODE, TODAY)] * 2


def test_second_run_same_day_does_not_resend(db):
    a = ann(db, "오늘 접수", [("rcept", "all", "2026-09-29", "2026-09-30")])
    sub(db, U1); bookmark(db, U1, a)
    s = Sender()
    run_reminders(db, TODAY, s)
    st = run_reminders(db, TODAY, s)
    assert len(s.calls) == 1 and st["skipped"] == 1 and st["sent"] == 0


def test_several_receipt_events_same_day_send_one_notification(db):
    a = ann(db, "여러 접수", [("gnrl_rnk1", "crsparea", "2026-09-29", "2026-09-29"),
                             ("gnrl_rnk1", "etc_area", "2026-09-29", "2026-09-29"),
                             ("spsply_rcept", "all", "2026-09-29", "2026-09-29")])
    sub(db, U1); bookmark(db, U1, a)
    s = Sender()
    run_reminders(db, TODAY, s)
    assert len(s.calls) == 1


def test_only_bookmarking_active_subscribed_users_and_only_start_day(db):
    today_ann = ann(db, "오늘", [("rcept", "all", "2026-09-29", "2026-09-30")], "1")
    tomorrow = ann(db, "내일", [("rcept", "all", "2026-09-30", "2026-10-01")], "2")
    ongoing = ann(db, "진행 중(어제 시작)", [("rcept", "all", "2026-09-28", "2026-09-30")], "3")
    winner_only = ann(db, "발표만 오늘", [("przwner_presnatn", "all", "2026-09-29", "2026-09-29")], "4")
    sub(db, U1); sub(db, U2, enabled=False)
    for a in (today_ann, tomorrow, ongoing, winner_only):
        bookmark(db, U1, a); bookmark(db, U2, a)
    other = ann(db, "북마크 안 함", [("rcept", "all", "2026-09-29", "2026-09-30")], "5")
    s = Sender()
    run_reminders(db, TODAY, s)
    assert [c[0].house_nm for c in s.calls] == ["오늘"]     # 시작일이 오늘인 접수, 활성 구독, 북마크한 사용자만
    assert all(c[0].announcement_id != str(other) for c in s.calls)


def test_other_users_bookmark_does_not_notify_me(db):
    a = ann(db, "타인 북마크", [("rcept", "all", "2026-09-29", "2026-09-30")])
    sub(db, U1); bookmark(db, U2, a)
    s = Sender()
    run_reminders(db, TODAY, s)
    assert s.calls == []


def test_failed_is_retried_only_after_the_wait_and_then_sent_once(db):
    a = ann(db, "재시도", [("rcept", "all", "2026-09-29", "2026-09-30")])
    sub(db, U1); bookmark(db, U1, a)
    first = Sender("failed")
    assert run_reminders(db, TODAY, first)["failed"] == 1
    immediate = Sender()
    assert run_reminders(db, TODAY, immediate)["skipped"] == 1 and immediate.calls == []   # 바로 재시도하지 않음
    db.execute("UPDATE public.notification_deliveries SET attempted_at = now() - interval '11 minutes'")
    later = Sender()
    assert run_reminders(db, TODAY, later)["sent"] == 1
    again = Sender()
    run_reminders(db, TODAY, again)
    assert again.calls == [] and rows(db) == [("sent", EVENT_CODE, TODAY)]


def test_crashed_pending_claim_is_reclaimed_after_the_wait(db):
    a = ann(db, "중단된 발송", [("rcept", "all", "2026-09-29", "2026-09-30")])
    sid = sub(db, U1); bookmark(db, U1, a)
    db.execute("INSERT INTO public.notification_deliveries(subscription_id, announcement_id, source_event_code, event_on, status, attempted_at)"
               " VALUES (%s,%s,%s,%s,'pending', now())", (sid, a, EVENT_CODE, TODAY))
    s = Sender()
    assert run_reminders(db, TODAY, s)["skipped"] == 1                       # 방금 잡힌 건 다른 실행이 처리 중
    db.execute("UPDATE public.notification_deliveries SET attempted_at = now() - interval '31 minutes'")
    assert run_reminders(db, TODAY, s)["sent"] == 1


def test_expired_subscription_is_disabled_and_not_used_again(db):
    a = ann(db, "만료", [("rcept", "all", "2026-09-29", "2026-09-30")])
    sid = sub(db, U1); bookmark(db, U1, a)
    assert run_reminders(db, TODAY, Sender("expired"))["expired"] == 1
    assert db.execute("SELECT enabled FROM public.push_subscriptions WHERE id=%s", (sid,)).fetchone()[0] is False
    s = Sender()
    run_reminders(db, TODAY, s)
    assert s.calls == []


def test_sender_exception_becomes_failed_not_crash(db):
    a = ann(db, "예외", [("rcept", "all", "2026-09-29", "2026-09-30")])
    sub(db, U1); bookmark(db, U1, a)

    def boom(t, p):
        raise RuntimeError("network down https://secret-endpoint.example")

    st = run_reminders(db, TODAY, boom)
    assert st["failed"] == 1 and rows(db)[0][0] == "failed"


def test_cleanup_deletes_only_long_disabled_subscriptions(db):
    old = sub(db, U1, enabled=False); recent = sub(db, U1, enabled=False); live = sub(db, U1)
    db.execute("UPDATE public.push_subscriptions SET updated_at = now() - interval '40 days' WHERE id=%s", (old,))
    assert cleanup_expired(db, 30) == 1
    left = {r[0] for r in db.execute("SELECT id FROM public.push_subscriptions")}
    assert left == {recent, live}


def test_payload_never_promises_time_eligibility_or_delivery():
    t = Target("s", "https://e", "k", "a", "aid", "테스트 단지", "apt", "1", "2")
    p = build_payload(t)
    text = p["title"] + p["body"]
    for banned in ("자격", "당첨", "확정", "정시", "반드시 도착", "100%"):
        assert banned not in text
    assert p["url"] == "/detail/?f=apt&h=1&p=2" and "공식 공고" in p["body"]


from datetime import timedelta

from moahome.notify import carryover_targets, claim, due_targets, finish


def test_late_finish_cannot_overwrite_a_reclaimed_delivery(db):
    """첫 실행이 오래 걸려 다른 실행이 재점유했다면, 첫 실행의 늦은 결과가 재점유 실행의 기록을 덮어쓰지 않는다."""
    a = ann(db, "재점유", [("rcept", "all", "2026-09-29", "2026-09-30")])
    sub(db, U1); bookmark(db, U1, a)
    [t1] = due_targets(db, TODAY)
    assert claim(db, t1, TODAY)
    db.execute("UPDATE public.notification_deliveries SET attempted_at = now() - interval '11 minutes'")
    [t2] = due_targets(db, TODAY)
    assert claim(db, t2, TODAY)                       # 두 번째 실행이 재점유
    assert finish(db, t2, TODAY, "sent") is True
    assert finish(db, t1, TODAY, "failed") is False   # 첫 실행의 늦은 결과는 무시
    assert rows(db) == [("sent", EVENT_CODE, TODAY)]


def test_failed_send_is_retried_the_next_day_once_with_carryover_wording(db):
    a = ann(db, "자정 넘김", [("rcept", "all", "2026-09-29", "2026-09-30")])
    sub(db, U1); bookmark(db, U1, a)
    assert run_reminders(db, TODAY, Sender("failed"))["failed"] == 1
    db.execute("UPDATE public.notification_deliveries SET attempted_at = now() - interval '11 minutes'")
    tomorrow = TODAY + timedelta(days=1)
    s = Sender()
    st = run_reminders(db, tomorrow, s)
    assert st["sent"] == 1 and len(s.calls) == 1
    body = s.calls[0][1]["body"]
    assert "오늘부터" not in body and "접수가 시작된" in body and "공식 공고" in body   # 어제 시작분에 '오늘'이라고 쓰지 않는다
    assert rows(db) == [("sent", EVENT_CODE, TODAY)]
    again = Sender()
    run_reminders(db, tomorrow, again)
    assert again.calls == []
    # 이틀 지나면 재시도하지 않는다
    db.execute("UPDATE public.notification_deliveries SET status='failed', attempted_at = now() - interval '11 minutes'")
    late = Sender()
    run_reminders(db, TODAY + timedelta(days=2), late)
    assert late.calls == []


@pytest.mark.parametrize("breaker", ["unbookmark", "disable_sub", "change_start"])
def test_carryover_requires_bookmark_active_subscription_and_unchanged_event(db, breaker):
    a = ann(db, "이월 후보", [("rcept", "all", "2026-09-29", "2026-09-30")])
    sid = sub(db, U1)
    bookmark(db, U1, a)
    run_reminders(db, TODAY, Sender("failed"))
    db.execute("UPDATE public.notification_deliveries SET attempted_at = now() - interval '11 minutes'")
    tomorrow = TODAY + timedelta(days=1)
    assert [t.house_nm for t in carryover_targets(db, tomorrow)] == ["이월 후보"]     # 조건이 유지되면 대상
    if breaker == "unbookmark":
        db.execute("DELETE FROM public.user_bookmarks WHERE announcement_id=%s", (a,))
    elif breaker == "disable_sub":
        db.execute("UPDATE public.push_subscriptions SET enabled=false WHERE id=%s", (sid,))
    else:
        db.execute("UPDATE public.announcement_events SET starts_on='2026-10-05', ends_on='2026-10-06' WHERE announcement_id=%s", (a,))
    assert carryover_targets(db, tomorrow) == []
    s = Sender()
    run_reminders(db, tomorrow, s)
    assert s.calls == []


def test_carryover_and_todays_event_for_same_announcement_send_only_one_notification(db):
    """어제 실패한 접수 시작 알림과, 같은 공고의 다른 접수(오늘 시작)가 겹쳐도 기기당 하루 한 번만 보낸다."""
    a = ann(db, "겹침", [("rcept", "all", "2026-09-29", "2026-09-30"), ("gnrl_rnk1", "crsparea", "2026-09-30", "2026-10-01")])
    sub(db, U1)
    bookmark(db, U1, a)
    assert run_reminders(db, TODAY, Sender("failed"))["failed"] == 1        # 9/29 시작분 실패
    db.execute("UPDATE public.notification_deliveries SET attempted_at = now() - interval '11 minutes'")
    tomorrow = TODAY + timedelta(days=1)                                     # 9/30: 1순위 접수가 오늘 시작
    s = Sender()
    st = run_reminders(db, tomorrow, s)
    assert len(s.calls) == 1 and st["sent"] == 1
    assert "오늘부터" in s.calls[0][1]["body"]                              # 오늘 시작분 문구가 우선
    # 오늘 알림으로 대체된 어제 실패 기록은 의도적으로 failed로 남고(이월은 1일 한정) 이후에도 재시도되지 않는다
    assert sorted(rows(db)) == [("failed", EVENT_CODE, TODAY), ("sent", EVENT_CODE, tomorrow)]
    db.execute("UPDATE public.notification_deliveries SET attempted_at = now() - interval '11 minutes'")
    later = Sender()
    run_reminders(db, tomorrow + timedelta(days=1), later)
    assert later.calls == []


def test_expired_result_disables_subscription_even_if_the_claim_was_lost(db):
    a = ann(db, "만료 확인", [("rcept", "all", "2026-09-29", "2026-09-30")])
    sid = sub(db, U1)
    bookmark(db, U1, a)
    [t1] = due_targets(db, TODAY)
    assert claim(db, t1, TODAY)
    db.execute("UPDATE public.notification_deliveries SET attempted_at = now() - interval '11 minutes'")
    [t2] = due_targets(db, TODAY)
    assert claim(db, t2, TODAY)                                              # 다른 실행이 재점유
    assert finish(db, t1, TODAY, "expired") is False                         # 기록은 덮어쓰지 않지만
    assert db.execute("SELECT enabled FROM public.push_subscriptions WHERE id=%s", (sid,)).fetchone()[0] is False   # 무효 구독은 끈다
    assert rows(db) == [("pending", EVENT_CODE, TODAY)]                      # 재점유 실행의 기록은 그대로


def test_payload_names_the_stage_that_starts_today_and_lists_all_same_day_stages(db):
    """단계별 알림: 그날 시작하는 접수 단계 이름을 문구에 넣고, 같은 날 여러 단계면 한 알림에 함께 적는다."""
    first = ann(db, "2순위만", [("rcept", "all", "2026-09-27", "2026-09-30"),
                               ("gnrl_rnk1", "crsparea", "2026-09-29", "2026-09-29"),
                               ("gnrl_rnk2", "crsparea", "2026-09-29", "2026-09-29"),
                               ("gnrl_rnk2", "etc_area", "2026-09-29", "2026-09-29")], "1")
    sub(db, U1)
    bookmark(db, U1, first)
    s = Sender()
    run_reminders(db, TODAY, s)
    assert len(s.calls) == 1                                                  # 같은 날 여러 단계도 알림은 한 번
    body = s.calls[0][1]["body"]
    assert "1순위 접수, 2순위 접수" in body and "청약 접수" not in body        # 오늘 시작하지 않은 단계는 넣지 않는다(순서: 1순위→2순위)
    assert s.calls[0][0].stages == ("gnrl_rnk1", "gnrl_rnk2")


def test_each_new_stage_start_day_sends_its_own_stage_named_notification(db):
    a = ann(db, "단계별", [("spsply_rcept", "all", "2026-09-28", "2026-09-28"),
                          ("gnrl_rnk1", "crsparea", "2026-09-29", "2026-09-29"),
                          ("gnrl_rnk2", "crsparea", "2026-09-30", "2026-09-30")])
    sub(db, U1)
    bookmark(db, U1, a)
    bodies = []
    for day in (date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)):
        s = Sender()
        run_reminders(db, day, s)
        assert len(s.calls) == 1
        bodies.append(s.calls[0][1]["body"])
    assert "특별공급 접수" in bodies[0] and "1순위 접수" in bodies[1] and "2순위 접수" in bodies[2]
    assert all("오늘부터" in b for b in bodies)


def test_carried_payload_uses_stage_name_without_saying_today():
    t = Target("s", "https://e", "k", "a", "aid", "이월 단지", "apt", "1", "2", event_on=date(2026, 9, 29), stages=("gnrl_rnk1",))
    body = build_payload(t, date(2026, 9, 30))["body"]
    assert "1순위 접수가 시작된 관심 공고" in body and "오늘부터" not in body
    assert "시각" not in body and "자격" not in body and "공식 공고" in body
