"""수집 흐름 논리 테스트(가짜 저장소, DB 불필요)."""
from datetime import date

from fakes import FixtureClient
from moahome.sync import run_sync

TODAY = date(2026, 9, 29)


class FakeStore:
    def __init__(self, existing=None, prev_totals=None, fail_keys=()):
        self.rows, self.runs, self.success_marks = {}, [], 0
        self._existing, self._prev, self.fail_keys = set(existing or ()), prev_totals or {}, set(fail_keys)

    def start_run(self):
        self.runs.append(None)
        return len(self.runs) - 1

    def finish_run(self, run_id, status, details, mark_success=False):
        self.runs[run_id] = (status, details)
        if mark_success:
            self.success_marks += 1

    def last_success_totals(self):
        return self._prev

    def existing_keys(self):
        return self._existing | set(self.rows)

    def open_keys(self, today, grace_days=7):
        return set()

    def upsert_announcement(self, m, prune_models):
        if m.key in self.fail_keys:
            import psycopg
            raise psycopg.errors.CheckViolation("boom")
        self.rows[m.key] = (m, prune_models)


def test_full_success_records_run_and_marks_data_status():
    store = FakeStore()
    res = run_sync(FixtureClient(), store, TODAY)
    assert res["status"] == "succeeded" and res["failure_count"] == 0 and store.success_marks == 1
    assert len(store.rows) == 7  # apt 2 + remndr 3 + urbty 2
    assert store.runs[0][0] == "succeeded" and set(res["totals"]) == {"apt", "remndr", "urbty_ofctl"}


def test_models_fetched_with_both_keys():
    c = FixtureClient()
    run_sync(c, FakeStore(), TODAY)
    conds = [cond for op, cond in c.calls if cond]
    assert conds and all(set(cond) == {"cond[HOUSE_MANAGE_NO::EQ]", "cond[PBLANC_NO::EQ]"} for cond in conds)


def test_model_fetch_failure_skips_only_that_announcement_and_marks_failed():
    c = FixtureClient()
    victim = c.data["getAPTLttotPblancDetail"][0]["HOUSE_MANAGE_NO"]
    store = FakeStore()
    res = run_sync(FixtureClient(fail_models={victim: "network"}), store, TODAY)
    assert res["status"] == "failed" and res["failure_count"] == 1 and store.success_marks == 0
    assert len(store.rows) == 6 and ("apt", victim, victim) not in store.rows
    assert res["failures"][0]["stage"] == "models" and res["failures"][0]["house_manage_no"] == victim


def test_detail_failure_skips_family_and_preserves_others():
    res_store = FakeStore()
    res = run_sync(FixtureClient(fail_detail={"getRemndrLttotPblancDetail": "http"}), res_store, TODAY)
    assert res["status"] == "failed" and res_store.success_marks == 0
    assert {k[0] for k in res_store.rows} == {"apt", "urbty_ofctl"}


def test_mapping_error_is_isolated_and_recorded_without_values():
    c = FixtureClient()
    c.data["getAPTLttotPblancDetail"][0]["RCEPT_BGNDE"] = "2026-10-09"
    c.data["getAPTLttotPblancDetail"][0]["RCEPT_ENDDE"] = "2026-10-01"
    store = FakeStore()
    res = run_sync(c, store, TODAY)
    assert res["status"] == "failed" and len(store.rows) == 6
    f = res["failures"][0]
    assert f["stage"] == "mapping" and "2026-10" not in f["error"]


def test_db_error_is_isolated():
    c = FixtureClient()
    bad = ("apt",) + tuple([c.data["getAPTLttotPblancDetail"][0]["HOUSE_MANAGE_NO"]] * 2)
    store = FakeStore(fail_keys=[bad])
    res = run_sync(c, store, TODAY)
    assert res["status"] == "failed" and len(store.rows) == 6 and res["failures"][0]["error"] == "CheckViolation"


def test_count_drop_versus_last_success_skips_family():
    store = FakeStore(prev_totals={"apt": 100})
    res = run_sync(FixtureClient(), store, TODAY)
    assert res["status"] == "failed" and store.success_marks == 0
    assert not any(k[0] == "apt" for k in store.rows)
    assert any(f["error"] == "count_drop" for f in res["failures"])


def test_consecutive_failures_abort_family():
    c = FixtureClient()
    base = c.data["getAPTLttotPblancDetail"][0]
    c.data["getAPTLttotPblancDetail"] = [
        {**base, "HOUSE_MANAGE_NO": f"X{i}", "PBLANC_NO": f"X{i}"} for i in range(8)]
    fail = {f"X{i}": "http" for i in range(8)}
    c.fail_models = fail
    res = run_sync(c, FakeStore(), TODAY)
    assert res["families"]["apt"]["skipped"] == 5          # 5회 연속 실패 후 중단
    assert any(f["stage"] == "aborted" and f["family"] == "apt" for f in res["failures"])
    assert res["families"]["remndr"]["upserted"] == 3      # 다른 계열은 계속 처리


def test_daily_skips_old_known_closed_announcements_but_backfill_takes_all():
    c = FixtureClient()
    for r in c.data["getAPTLttotPblancDetail"]:
        r["RCRIT_PBLANC_DE"] = "2020-01-01"
    known = {("apt", r["HOUSE_MANAGE_NO"], r["PBLANC_NO"]) for r in c.data["getAPTLttotPblancDetail"]}
    daily = FakeStore(existing=known)
    res = run_sync(c, daily, TODAY, mode="daily")
    assert res["families"]["apt"]["targets"] == 0
    back = FakeStore(existing=known)
    res = run_sync(c, back, TODAY, mode="backfill")
    assert res["families"]["apt"]["targets"] == 2


def test_daily_does_not_fetch_old_unknown_announcements_but_backfill_does():
    """DB에 없는 오래된 공고를 매일 모드가 '신규'로 받아 호출 한도를 소진하지 않는다."""
    def old_client():
        c = FixtureClient()
        for r in c.data["getAPTLttotPblancDetail"]:
            r["RCRIT_PBLANC_DE"] = "2020-01-01"
        return c

    c = old_client()
    daily = run_sync(c, FakeStore(), TODAY, mode="daily")
    assert daily["families"]["apt"]["targets"] == 0
    assert not any(cond for op, cond in c.calls if op == "getAPTLttotPblancMdl")   # 주택형 조회 호출 없음
    back = run_sync(old_client(), FakeStore(), TODAY, mode="backfill")
    assert back["families"]["apt"]["targets"] == 2


def test_empty_models_do_not_prune():
    c = FixtureClient()
    c.data["getAPTLttotPblancMdl"] = []
    store = FakeStore()
    run_sync(c, store, TODAY)
    assert all(prune is False for (m, prune) in store.rows.values() if m.family == "apt")


def test_empty_models_preserve_existing_announcement_entirely():
    c = FixtureClient()
    c.data["getAPTLttotPblancMdl"] = []
    known = {("apt", r["HOUSE_MANAGE_NO"], r["PBLANC_NO"]) for r in c.data["getAPTLttotPblancDetail"]}
    store = FakeStore(existing=known)
    res = run_sync(c, store, TODAY)
    assert not any(k[0] == "apt" for k in store.rows)        # 가격 요약·일정 포함 그대로 둔다
    assert res["families"]["apt"]["empty_models_preserved"] == 2
    # 오래된 상태로 남을 수 있으므로 성공으로 기록하지 않는다
    assert res["status"] == "failed" and store.success_marks == 0
    assert sum(f["error"] == "empty_models" for f in res["failures"]) == 2


def test_empty_models_for_new_announcement_is_inserted_without_prices():
    c = FixtureClient()
    c.data["getAPTLttotPblancMdl"] = []
    store = FakeStore()
    run_sync(c, store, TODAY)
    m, prune = next((m, p) for m, p in store.rows.values() if m.family == "apt")
    assert prune is False and m.fields["min_price_krw"] is None and m.housing_types == []


def test_price_anomaly_fails_run_and_preserves_announcement():
    c = FixtureClient()
    c.data["getUrbtyOfctlLttotPblancMdl"][0]["SUPLY_AMOUNT"] = "abc"
    victim = c.data["getUrbtyOfctlLttotPblancMdl"][0]["HOUSE_MANAGE_NO"]
    store = FakeStore()
    res = run_sync(c, store, TODAY)
    assert res["status"] == "failed" and store.success_marks == 0
    assert ("urbty_ofctl", victim, victim) not in store.rows
    assert any(f["stage"] == "mapping" and "가격 이상치" in f["error"] and "abc" not in f["error"] for f in res["failures"])


def test_fatal_error_marks_run_failed_even_for_keyboard_interrupt():
    import pytest

    class Boom(FixtureClient):
        def get_all(self, *a, **k):
            raise KeyboardInterrupt

    store = FakeStore()
    with pytest.raises(KeyboardInterrupt):
        run_sync(Boom(), store, TODAY)
    assert store.runs[0][0] == "failed" and store.runs[0][1]["failures"][0]["error"] == "KeyboardInterrupt"


def test_duplicate_normalized_key_in_same_run_is_recorded_not_overwritten():
    c = FixtureClient()
    first = c.data["getUrbtyOfctlLttotPblancDetail"][0]
    c.data["getUrbtyOfctlLttotPblancDetail"].append({**first, "HOUSE_MANAGE_NO": " " + first["HOUSE_MANAGE_NO"] + " "})
    store = FakeStore()
    res = run_sync(c, store, TODAY)
    assert res["status"] == "failed" and any(f["error"] == "duplicate_key" for f in res["failures"])
    assert len([k for k in store.rows if k[0] == "urbty_ofctl"]) == 2


def test_finish_run_failure_still_attempts_failed_close():
    import pytest

    class BrokenFinish(FakeStore):
        def __init__(self):
            super().__init__()
            self.finish_calls = []

        def finish_run(self, run_id, status, details, mark_success=False):
            self.finish_calls.append((status, mark_success))
            if len(self.finish_calls) == 1:
                raise RuntimeError("connection lost")
            super().finish_run(run_id, status, details, mark_success)

    store = BrokenFinish()
    with pytest.raises(RuntimeError):
        run_sync(FixtureClient(), store, TODAY)
    assert store.finish_calls[0] == ("succeeded", True) and store.finish_calls[1] == ("failed", False)
    assert store.success_marks == 0
