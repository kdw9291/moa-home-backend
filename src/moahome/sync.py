"""수집 오케스트레이션: 계열별 전 페이지 조회 -> 공고별 주택형 조회 -> 매핑 -> 공고 단위 원자적 반영.

실패 규칙(docs/API_SPEC.md 4절): 실패 공고는 건너뛰어 기존 데이터를 보존하고 실행을 failed로 기록한다.
data_status는 실패가 하나도 없을 때만 갱신한다. 건수가 직전 성공 대비 급감한 계열은 반영하지 않는다.
실패 기록에는 공고 키(공개 번호)와 오류 종류만 남기고 URL·키·원본 값은 남기지 않는다.
"""
from datetime import date, timedelta

import psycopg

from .client import ApiError
from .mappers import FAMILIES, OPS, MappingError, map_announcement

MAX_LOGGED_FAILURES = 50
ABORT_AFTER_CONSECUTIVE = 5


def _rcrit(rec):
    try:
        return date.fromisoformat(str(rec.get("RCRIT_PBLANC_DE")).strip())
    except ValueError:
        return None


def _select_targets(mode, rows, family, today, existing, open_keys, lookback_days, since):
    out = []
    cutoff = today - timedelta(days=lookback_days)
    for rec in rows:
        key = (family, str(rec.get("HOUSE_MANAGE_NO")), str(rec.get("PBLANC_NO")))
        d = _rcrit(rec)
        if mode == "backfill":
            if since is None or d is None or d >= since:
                out.append(rec)
        elif key in open_keys or d is None or d >= cutoff:
            # 매일 모드: 최근 모집공고, 진행 중·막 끝난 공고, 날짜를 알 수 없는 공고만.
            # DB에 없는 오래된 공고를 '신규'로 취급하면 첫 실행에서 수천 건을 조회하므로 백필 모드로만 받는다.
            out.append(rec)
    return out


def run_sync(client, store, today, mode="daily", lookback_days=90, since=None, drop_ratio=0.8):
    """today: KST 기준 날짜. 반환: 실행 요약(dict)."""
    run_id = store.start_run()
    details = {"mode": mode, "families": {}, "totals": {}, "failure_count": 0, "failures": []}

    def fail(entry):
        details["failure_count"] += 1
        if len(details["failures"]) < MAX_LOGGED_FAILURES:
            details["failures"].append(entry)

    try:
        existing, open_keys, prev = store.existing_keys(), store.open_keys(today), store.last_success_totals()
        processed = set()
        for fam in FAMILIES:
            det_op, mdl_op = OPS[fam]
            stat = {"detail_rows": 0, "targets": 0, "upserted": 0, "skipped": 0, "duplicates": 0, "warnings": 0}
            details["families"][fam] = stat
            try:
                rows, total, dups = client.get_all(det_op)
            except ApiError as e:
                fail({"family": fam, "stage": "detail", "error": e.kind})
                continue
            stat["detail_rows"], stat["duplicates"] = len(rows), dups
            if prev.get(fam) and total < prev[fam] * drop_ratio:
                fail({"family": fam, "stage": "detail", "error": "count_drop"})
                continue
            details["totals"][fam] = total
            targets = _select_targets(mode, rows, fam, today, existing, open_keys, lookback_days, since)
            stat["targets"] = len(targets)
            consecutive = 0
            for rec in targets:
                hmn, pno = str(rec.get("HOUSE_MANAGE_NO")).strip(), str(rec.get("PBLANC_NO")).strip()
                key = {"family": fam, "house_manage_no": hmn, "pblanc_no": pno}
                if (fam, hmn, pno) in processed:  # 공백만 다른 식별자 등 같은 실행의 정규화 키 중복
                    fail({**key, "stage": "detail", "error": "duplicate_key"})
                    stat["skipped"] += 1
                    continue
                processed.add((fam, hmn, pno))
                try:
                    models, _, _ = client.get_all(
                        mdl_op, cond={"cond[HOUSE_MANAGE_NO::EQ]": hmn, "cond[PBLANC_NO::EQ]": pno},
                        key_fields=("HOUSE_MANAGE_NO", "PBLANC_NO", "MODEL_NO"))
                    if not models and (fam, hmn, pno) in existing:
                        # 빈 주택형 응답은 정상/장애를 구분할 수 없다: 기존 공고(가격 요약·일정 포함)는 그대로 보존하되
                        # 오래된 상태로 남을 수 있으므로 실패로 기록해 성공·최신 시각으로 오인되지 않게 한다.
                        stat["empty_models_preserved"] = stat.get("empty_models_preserved", 0) + 1
                        fail({**key, "stage": "models", "error": "empty_models"})
                        stat["skipped"] += 1
                        continue
                    mapped = map_announcement(fam, rec, models)
                    store.upsert_announcement(mapped, prune_models=len(models) > 0)
                except ApiError as e:
                    fail({**key, "stage": "models", "error": e.kind})
                except MappingError as e:
                    fail({**key, "stage": "mapping", "error": str(e)})
                except psycopg.Error as e:
                    fail({**key, "stage": "db", "error": type(e).__name__})
                else:
                    stat["upserted"] += 1
                    stat["warnings"] += len(mapped.warnings)
                    consecutive = 0
                    continue
                stat["skipped"] += 1
                consecutive += 1
                if consecutive >= ABORT_AFTER_CONSECUTIVE:  # 인증·한도 오류 등에서 무의미한 반복 방지
                    fail({"family": fam, "stage": "aborted", "error": "consecutive_failures"})
                    break
    except BaseException as e:  # KeyboardInterrupt 포함: 실행 기록을 running으로 남기지 않는다(원문 메시지는 기록 안 함)
        fail({"stage": "fatal", "error": type(e).__name__})
        details["status"] = "failed"
        try:
            store.finish_run(run_id, "failed", details)
        except Exception:
            pass
        raise
    status = "succeeded" if details["failure_count"] == 0 else "failed"
    details["status"] = status
    # 실행 기록과 data_status 갱신은 한 트랜잭션: 성공 기록 없이 갱신 시각만 앞서가지 않는다
    try:
        store.finish_run(run_id, status, details, mark_success=(status == "succeeded"))
    except BaseException:  # 마감 기록 실패 시에도 running으로 남지 않게 실패 마감을 한 번 더 시도
        details["status"] = "failed"
        try:
            store.finish_run(run_id, "failed", {**details, "failures": [*details["failures"], {"stage": "finish", "error": "finish_run_failed"}]})
        except Exception:
            pass
        raise
    return details
