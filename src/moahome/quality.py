"""수집 데이터 품질 점검(읽기 전용). 로컬 실사용에서 누락·오분류·단위 오류 의심 항목을 빠르게 찾기 위한 리포트.

DB를 바꾸지 않는다(호출자가 READ ONLY 트랜잭션으로 연결한다). 공고 이름·자연키만 표본으로 보여 주며 개인정보·키는 다루지 않는다.
- warn: 데이터가 틀렸을 가능성이 커서 원천과 대조할 가치가 큰 항목.
- info: 정상일 수도 있어(원천이 제공하지 않는 값 등) 참고용으로 보는 항목.
"""
from dataclasses import dataclass, field
from datetime import date

A = "public.cheongyak_announcements"
H = "public.cheongyak_housing_types"
E = "public.announcement_events"
S = "public.housing_type_special_supply"

LABEL = "a.source_family || '/' || a.house_manage_no || '/' || a.pblanc_no || ' ' || left(a.house_nm, 24)"


@dataclass
class Check:
    key: str
    title: str
    level: str          # warn | info
    total_sql: str      # 분모(점검 대상 수)
    hit_from: str       # 해당 행을 고르는 FROM/WHERE (별칭 a 필수, 필요 시 h/e)
    hint: str           # 사람이 원천과 어떻게 대조할지


@dataclass
class Result:
    check: Check
    total: int
    hits: int
    samples: list = field(default_factory=list)

    @property
    def rate(self) -> float:
        return (self.hits / self.total) if self.total else 0.0


def checks(today: date) -> list[Check]:
    d = today.isoformat()
    return [
        Check("no_housing_types", "주택형이 하나도 없는 공고", "warn", f"SELECT count(*) FROM {A}",
              f"FROM {A} a WHERE NOT EXISTS (SELECT 1 FROM {H} h WHERE h.announcement_id = a.id)",
              "주택형 API 호출이 부분 실패했거나 원천에 주택형이 없는 공고일 수 있다. 공식 공고문과 대조."),
        Check("urbty_exclusive_missing", "오피스텔·도시형인데 전용면적이 없는 주택형", "warn",
              f"SELECT count(*) FROM {H} h JOIN {A} a ON a.id = h.announcement_id WHERE a.source_family = 'urbty_ofctl'",
              f"FROM {H} h JOIN {A} a ON a.id = h.announcement_id WHERE a.source_family = 'urbty_ofctl' AND h.exclusive_area_sqm IS NULL",
              "오피스텔·도시형은 전용면적(EXCLUSE_AR)을 제공하는 계열이다. 원천 응답 필드를 확인."),
        Check("apt_supply_missing", "APT인데 공급면적이 없는 주택형", "warn",
              f"SELECT count(*) FROM {H} h JOIN {A} a ON a.id = h.announcement_id WHERE a.source_family = 'apt'",
              f"FROM {H} h JOIN {A} a ON a.id = h.announcement_id WHERE a.source_family = 'apt' AND h.supply_area_sqm IS NULL",
              "APT는 공급면적(SUPLY_AR)을 제공하는 계열이다. 원천 응답 필드를 확인."),
        Check("remndr_supply_missing", "잔여세대인데 공급면적이 없는 주택형", "info",
              f"SELECT count(*) FROM {H} h JOIN {A} a ON a.id = h.announcement_id WHERE a.source_family = 'remndr'",
              f"FROM {H} h JOIN {A} a ON a.id = h.announcement_id WHERE a.source_family = 'remndr' AND h.supply_area_sqm IS NULL",
              "원천 API가 일부 잔여세대 공고(예: 공고번호 앞자리 2026910…)의 SUPLY_AR를 비워서 준다(2026-10-04 원천 응답 직접 확인, 수집기 누락 아님). 이런 주택형은 면적 필터에서 '판정 미확인'으로 나뉜다."),
        Check("price_unit_suspect", "최고 분양가가 1천만 원 미만이거나 1,000억 원 초과(단위 오류 의심)", "warn",
              f"SELECT count(*) FROM {H} WHERE price_max_krw IS NOT NULL",
              f"FROM {H} h JOIN {A} a ON a.id = h.announcement_id WHERE h.price_max_krw IS NOT NULL AND (h.price_max_krw < 10000000 OR h.price_max_krw > 100000000000)",
              "만원 단위를 두 번 환산했거나 환산하지 않았을 수 있다. price_raw·price_source_unit과 대조."),
        Check("area_out_of_range", "전용면적 10~300㎡ 또는 공급면적 10~400㎡를 벗어난 주택형", "warn",
              f"SELECT count(*) FROM {H} WHERE exclusive_area_sqm IS NOT NULL OR supply_area_sqm IS NOT NULL",
              f"FROM {H} h JOIN {A} a ON a.id = h.announcement_id WHERE (h.exclusive_area_sqm IS NOT NULL AND h.exclusive_area_sqm NOT BETWEEN 10 AND 300) OR (h.supply_area_sqm IS NOT NULL AND h.supply_area_sqm NOT BETWEEN 10 AND 400)",
              "소수점 위치 오류나 평·㎡ 혼동이 의심된다. 원천 값과 대조."),
        Check("supply_less_than_exclusive", "공급면적이 전용면적보다 작은 주택형", "warn",
              f"SELECT count(*) FROM {H} WHERE exclusive_area_sqm IS NOT NULL AND supply_area_sqm IS NOT NULL",
              f"FROM {H} h JOIN {A} a ON a.id = h.announcement_id WHERE h.exclusive_area_sqm IS NOT NULL AND h.supply_area_sqm IS NOT NULL AND h.supply_area_sqm < h.exclusive_area_sqm",
              "전용/공급면적 필드가 뒤바뀌었을 수 있다."),
        Check("no_events", "접수·발표 일정이 하나도 없거나 날짜가 모두 비어 있는 공고", "warn", f"SELECT count(*) FROM {A}",
              f"FROM {A} a WHERE NOT EXISTS (SELECT 1 FROM {E} e WHERE e.announcement_id = a.id AND (e.starts_on IS NOT NULL OR e.ends_on IS NOT NULL))",
              "일정이 없으면 D-Day·알림이 동작하지 않는다. 원천 날짜 필드 매핑을 확인."),
        Check("event_date_range", f"일정 날짜가 2000년 이전이거나 오늘({d}) 기준 2년 뒤를 넘는 이벤트", "warn", f"SELECT count(*) FROM {E}",
              f"FROM {E} e JOIN {A} a ON a.id = e.announcement_id WHERE (e.starts_on IS NOT NULL AND (e.starts_on < DATE '2000-01-01' OR e.starts_on > (DATE '{d}' + interval '2 years')::date)) OR (e.ends_on IS NOT NULL AND (e.ends_on < DATE '2000-01-01' OR e.ends_on > (DATE '{d}' + interval '2 years')::date))",
              "연도 오타나 날짜 파싱 오류가 의심된다."),
        Check("special_sum_mismatch", "특별공급 범주별 합계가 주택형 특별공급 수와 다른 주택형", "warn",
              f"SELECT count(DISTINCT s.housing_type_id) FROM {S} s JOIN {H} h ON h.id = s.housing_type_id WHERE h.special_supply_count IS NOT NULL",
              f"FROM {H} h JOIN {A} a ON a.id = h.announcement_id WHERE h.special_supply_count IS NOT NULL AND EXISTS (SELECT 1 FROM {S} s WHERE s.housing_type_id = h.id) AND (SELECT sum(s.supply_count) FROM {S} s WHERE s.housing_type_id = h.id) <> h.special_supply_count",
              "범주 매핑 누락이나 합산 규칙 차이일 수 있다."),
        Check("stale_but_active", "7일 넘게 수집되지 않았는데 아직 일정이 남은 공고", "warn", f"SELECT count(*) FROM {A}",
              f"FROM {A} a WHERE a.last_seen_at < now() - interval '7 days' AND EXISTS (SELECT 1 FROM {E} e WHERE e.announcement_id = a.id AND coalesce(e.ends_on, e.starts_on) >= DATE '{d}')",
              "원천에서 사라졌거나 수집 범위를 벗어난 공고일 수 있다. 공식 사이트에서 아직 게시 중인지 확인."),
        Check("summary_price_mismatch", "공고 요약 가격(min/max)이 주택형 가격과 맞지 않는 공고", "warn", f"SELECT count(*) FROM {A}",
              f"FROM {A} a WHERE (a.min_price_krw, a.max_price_krw) IS DISTINCT FROM ((SELECT min(h.price_max_krw) FROM {H} h WHERE h.announcement_id = a.id), (SELECT max(h.price_max_krw) FROM {H} h WHERE h.announcement_id = a.id))",
              "요약 컬럼 갱신 누락. 재수집으로 맞춰지는지 확인."),
        Check("rcrit_date_odd", f"모집공고일이 비어 있거나 오늘 기준 30일 뒤를 넘거나 10년보다 오래된 공고", "info", f"SELECT count(*) FROM {A}",
              f"FROM {A} a WHERE a.rcrit_pblanc_de IS NULL OR a.rcrit_pblanc_de > DATE '{d}' + 30 OR a.rcrit_pblanc_de < DATE '{d}' - 3650",
              "계열에 따라 모집공고일이 없을 수 있다(잔여세대 등)."),
        Check("total_supply_missing", "총 공급 세대수가 비어 있거나 0인 공고", "info", f"SELECT count(*) FROM {A}",
              f"FROM {A} a WHERE a.tot_suply_hshldco IS NULL OR a.tot_suply_hshldco = 0",
              "원천이 총 세대수를 주지 않는 계열일 수 있다."),
        Check("total_supply_mismatch", "총 공급 세대수와 주택형 일반+특별 공급 수 합이 다른 공고", "info",
              f"SELECT count(*) FROM {A} WHERE tot_suply_hshldco IS NOT NULL",
              f"FROM {A} a WHERE a.tot_suply_hshldco IS NOT NULL AND EXISTS (SELECT 1 FROM {H} h WHERE h.announcement_id = a.id) AND (SELECT sum(coalesce(h.general_supply_count,0) + coalesce(h.special_supply_count,0)) FROM {H} h WHERE h.announcement_id = a.id) <> a.tot_suply_hshldco",
              "원천이 합계를 다른 기준(예: 기관추천 포함)으로 줄 수 있어 참고용이다."),
        Check("region_missing", "공급지역 코드가 없는 공고", "info", f"SELECT count(*) FROM {A}",
              f"FROM {A} a WHERE a.source_region_code IS NULL",
              "지역 필터에서 빠질 수 있다. 원천 지역 필드를 확인."),
        Check("url_odd", "공식 공고 링크가 없거나 https가 아닌 공고", "info", f"SELECT count(*) FROM {A}",
              f"FROM {A} a WHERE a.pblanc_url IS NULL OR a.pblanc_url !~ '^https://'",
              "공고 원문 링크는 항상 노출해야 한다."),
        Check("duplicate_suspect", "같은 계열·이름·모집공고일이 겹치는 공고(중복 의심)", "info",
              f"SELECT count(*) FROM {A}",
              f"FROM {A} a WHERE EXISTS (SELECT 1 FROM {A} b WHERE b.id <> a.id AND b.source_family = a.source_family AND b.house_nm = a.house_nm AND b.rcrit_pblanc_de IS NOT DISTINCT FROM a.rcrit_pblanc_de)",
              "정정·재공고일 수 있다. 같은 단지가 중복 노출되는지 확인."),
    ]


def overview(conn) -> list[tuple]:
    """계열별 규모와 주요 필드 확인률(참고)."""
    return conn.execute(f"""
        SELECT a.source_family,
               count(DISTINCT a.id) AS announcements,
               count(h.id) AS housing_types,
               round(100.0 * count(h.exclusive_area_sqm) / nullif(count(h.id), 0), 1) AS exclusive_known_pct,
               round(100.0 * count(h.supply_area_sqm) / nullif(count(h.id), 0), 1) AS supply_known_pct,
               round(100.0 * count(h.price_max_krw) / nullif(count(h.id), 0), 1) AS price_known_pct,
               max(a.last_seen_at)::date AS last_seen
        FROM {A} a LEFT JOIN {H} h ON h.announcement_id = a.id
        GROUP BY a.source_family ORDER BY a.source_family""").fetchall()


def run_checks(conn, today: date, sample_limit: int = 5) -> list[Result]:
    out = []
    for c in checks(today):
        total = conn.execute(c.total_sql).fetchone()[0]
        hits = conn.execute(f"SELECT count(*) {c.hit_from}").fetchone()[0]
        samples = []
        if hits and sample_limit > 0:
            samples = [r[0] for r in conn.execute(f"SELECT {LABEL} {c.hit_from} ORDER BY 1 LIMIT %s", (sample_limit,)).fetchall()]
        out.append(Result(c, total, hits, samples))
    return out


def format_report(today: date, ov: list[tuple], results: list[Result]) -> str:
    lines = [f"모아홈 수집 데이터 품질 점검 ({today.isoformat()} 기준, 읽기 전용)", ""]
    lines.append("[계열별 규모와 확인률]")
    lines.append("계열           공고   주택형   전용면적확인  공급면적확인  가격확인   마지막 수집")
    for fam, n, ht, ex, su, pr, seen in ov:
        f = lambda v: "-" if v is None else f"{v}%"
        lines.append(f"{fam:<14}{n:>5}  {ht:>6}   {f(ex):>10}  {f(su):>10}  {f(pr):>8}   {seen}")
    lines.append("(APT는 공급면적, 오피스텔·도시형은 전용면적을 제공합니다. 잔여세대는 공급면적을 일부 공고에서만 줍니다. 한쪽이 낮은 것은 원천 특성입니다.)")
    for level, title in (("warn", "이상 의심 (원천과 대조 권장)"), ("info", "참고 (정상일 수 있음)")):
        lines.append("")
        lines.append(f"[{title}]")
        for r in (x for x in results if x.check.level == level):
            mark = "OK " if r.hits == 0 else ("!! " if level == "warn" else " * ")
            lines.append(f"{mark}{r.check.title}: {r.hits}/{r.total} ({r.rate * 100:.1f}%)")
            if r.hits:
                lines.append(f"     대조 방법: {r.check.hint}")
                for s in r.samples:
                    lines.append(f"     - {s}")
    warn = sum(1 for x in results if x.check.level == "warn" and x.hits)
    lines.append("")
    lines.append(f"요약: 이상 의심 항목 {warn}개 / 점검 {len([x for x in results if x.check.level == 'warn'])}개. 표본은 항목별 앞 {max((len(x.samples) for x in results), default=0)}건만 표시합니다.")
    return "\n".join(lines)
