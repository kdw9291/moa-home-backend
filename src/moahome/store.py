"""PostgreSQL 저장소(psycopg). 공고 단위 트랜잭션으로 마스터·주택형·특별공급·일정을 함께 반영한다.

주의: GitHub Actions에는 DB 접속 문자열이 없고 SUPABASE_URL/SECRET만 있다. REST로 같은 원자성을 얻으려면
DB 함수(RPC) 설계가 필요하며 이 저장소는 그 대안이 아니다(구현 보류, PROJECT_STATUS 참조).
"""

import psycopg
from psycopg.types.json import Jsonb

_ANN_COLS = ["house_nm", "source_subtype", "house_secd", "house_dtl_secd", "rent_secd", "rcrit_pblanc_de",
             "hssply_adres", "source_region_code", "source_region_name", "tot_suply_hshldco", "bsns_mby_nm",
             "mdhs_telno", "pblanc_url", "min_price_krw", "max_price_krw"]
_TYPE_COLS = ["house_ty", "exclusive_area_sqm", "supply_area_sqm", "general_supply_count",
              "special_supply_count", "price_max_krw", "price_raw", "price_source_unit"]


class PgStore:
    def __init__(self, conninfo):
        self._conn = psycopg.connect(conninfo, autocommit=True)

    def close(self):
        self._conn.close()

    # ---- 실행 기록 ----
    def start_run(self):
        return self._conn.execute("INSERT INTO public.sync_runs(status) VALUES ('running') RETURNING id").fetchone()[0]

    def finish_run(self, run_id, status, details, mark_success=False):
        """mark_success=True는 실패 없는 전체 성공일 때만. 실행 기록과 data_status를 한 트랜잭션으로 갱신."""
        with self._conn.transaction():
            self._conn.execute(
                "UPDATE public.sync_runs SET status=%s, finished_at=now(), details=%s WHERE id=%s",
                (status, Jsonb(details), run_id))
            if mark_success:
                self._conn.execute(
                    "UPDATE public.data_status SET last_successful_sync_at=now(), updated_at=now() WHERE singleton_id=1")

    def last_success_totals(self):
        row = self._conn.execute(
            "SELECT details FROM public.sync_runs WHERE status='succeeded' ORDER BY finished_at DESC LIMIT 1").fetchone()
        return (row[0] or {}).get("totals", {}) if row else {}

    # ---- 조회 ----
    def existing_keys(self):
        return {tuple(r) for r in self._conn.execute(
            "SELECT source_family, house_manage_no, pblanc_no FROM public.cheongyak_announcements")}

    def open_keys(self, today, grace_days=7):
        """일정이 끝난 지 grace_days 이내이거나 아직 남은 공고(수정 재조회 대상)."""
        return {tuple(r) for r in self._conn.execute(
            "SELECT DISTINCT a.source_family, a.house_manage_no, a.pblanc_no FROM public.cheongyak_announcements a"
            " JOIN public.announcement_events e ON e.announcement_id=a.id"
            " WHERE coalesce(e.ends_on, e.starts_on) >= %s::date - %s", (today, grace_days))}

    # ---- 공고 단위 원자적 반영 ----
    def upsert_announcement(self, m, prune_models):
        """m: MappedAnnouncement. prune_models=True일 때만 응답에 없는 주택형을 삭제(완전 수집 확인 시)."""
        with self._conn.transaction():
            cols = ", ".join(_ANN_COLS)
            marks = ", ".join(["%s"] * len(_ANN_COLS))
            sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in _ANN_COLS)
            ann_id = self._conn.execute(
                f"INSERT INTO public.cheongyak_announcements(source_family, house_manage_no, pblanc_no, {cols}, last_seen_at)"
                f" VALUES (%s,%s,%s,{marks}, now())"
                f" ON CONFLICT (source_family, house_manage_no, pblanc_no) DO UPDATE SET {sets}, last_seen_at=now(), updated_at=now()"
                " RETURNING id",
                (m.family, m.house_manage_no, m.pblanc_no, *[m.fields[c] for c in _ANN_COLS])).fetchone()[0]

            keys = []
            for t in m.housing_types:
                keys.append(t["source_model_key"])
                tcols = ", ".join(_TYPE_COLS)
                tmarks = ", ".join(["%s"] * len(_TYPE_COLS))
                tsets = ", ".join(f"{c}=EXCLUDED.{c}" for c in _TYPE_COLS)
                type_id = self._conn.execute(
                    f"INSERT INTO public.cheongyak_housing_types(announcement_id, source_model_key, {tcols})"
                    f" VALUES (%s,%s,{tmarks})"
                    f" ON CONFLICT (announcement_id, source_model_key) DO UPDATE SET {tsets}, updated_at=now()"
                    " RETURNING id",
                    (ann_id, t["source_model_key"], *[t[c] for c in _TYPE_COLS])).fetchone()[0]
                self._conn.execute("DELETE FROM public.housing_type_special_supply WHERE housing_type_id=%s", (type_id,))
                for s in t["special_supply"]:
                    self._conn.execute(
                        "INSERT INTO public.housing_type_special_supply(housing_type_id, category_code, supply_count)"
                        " VALUES (%s,%s,%s)", (type_id, s["category_code"], s["supply_count"]))
            if prune_models and keys:
                self._conn.execute(
                    "DELETE FROM public.cheongyak_housing_types WHERE announcement_id=%s AND source_model_key <> ALL(%s)",
                    (ann_id, keys))

            self._conn.execute("DELETE FROM public.announcement_events WHERE announcement_id=%s", (ann_id,))
            for e in m.events:
                self._conn.execute(
                    "INSERT INTO public.announcement_events(announcement_id, source_event_code, scope_code, starts_on, ends_on)"
                    " VALUES (%s,%s,%s,%s,%s)",
                    (ann_id, e["source_event_code"], e["scope_code"], e["starts_on"], e["ends_on"]))
        return ann_id
