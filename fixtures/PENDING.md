# Fixture 인벤토리와 원천 필드 검증 상태

기준일: 2026-09-29. `tools/fetch_samples.py`, `tools/build_fixtures.py`로 실제 승인 키 응답을 받아 익명화했다(사업주체·시공사·신문사명, 문의전화, 홈페이지 치환. 키·URL 미저장). 정의·단위는 공식 Swagger(`infuser.odcloud.kr/api/stages/37000/api-docs`)의 필드 설명으로 확인했다. 표본은 계열별 최신순 100건이며, **수집 시점의 관측값이라 보존된 fixture(계열별 공고 2~3건)만으로는 재검증되지 않는다.** 아래 '표본' 표기는 이 관측을 뜻한다.

## 보유 fixture
`{apt,remndr,urbty_ofctl}_{detail,model}_cases.json`(대표 사례), `apt_detail_empty_page.json`(HTTP 200 빈 페이지), `apt_detail_sample.xml`(XML 응답), `error_unregistered_key.json`(HTTP 401).

## 확인됨 / 미확인
| 항목 | apt | remndr | urbty_ofctl |
|---|---|---|---|
| 공고 키 | `HOUSE_MANAGE_NO`+`PBLANC_NO` (표본 100건 중복 0, 두 값 동일) | 동일 | 동일 |
| 주택형 키 | `MODEL_NO` (공고 내 유일 — **수집 시점 표본에서만 확인**. 공고 수정·재조회 시 유지되는지(안정성)는 **미확인**, 스키마 키 확정 전 검증 필요) | 동일 | 동일 |
| 분양가 필드 | `LTTOT_TOP_AMOUNT` **분양최고금액, 만원**(Swagger), 문자열 정수 | 동일 | `SUPLY_AMOUNT` 분양최고금액 만원(Swagger) |
| 면적 필드 | `SUPLY_AR` **공급면적**만 존재. **전용면적 필드 없음** | 동일 | `EXCLUSE_AR` **전용면적** |
| 일반/특별공급 수 | `SUPLY_HSHLDCO`, `SPSPLY_HSHLDCO`, 범주별 9개 필드 | `SUPLY_HSHLDCO`, `SPSPLY_HSHLDCO` (범주별 없음) | `SUPLY_HSHLDCO`만 |
| 세부 유형 | `HOUSE_SECD` 01 APT/09 민간사전청약/10 신혼희망타운, `HOUSE_DTL_SECD` 01 민영/03 국민, `RENT_SECD` | `HOUSE_SECD` 04 무순위/06 불법행위 재공급 (표본 87/13) | `HOUSE_SECD` 02, `HOUSE_DTL_SECD` 01~04, `SEARCH_HOUSE_SECD` |
| 일정 필드 | 접수 `RCEPT_*`, 특별공급, 1·2순위 해당/경기/기타 지역, 발표 `PRZWNER_PRESNATN_DE`, 계약 `CNTRCT_CNCLS_*` (특별공급·경기지역은 다수 null) | `SUBSCRPT_RCEPT_*`, `GNRL_RCEPT_*`, `SPSPLY_RCEPT_*`(대부분 null), 발표, 계약 | `SUBSCRPT_RCEPT_*`, 발표, 계약 |
| 지역 | `SUBSCRPT_AREA_CODE`(예 410)+`_NM`, `HSSPLY_ADRES` | 동일(예 100 서울) | 동일(예 400 인천) |
| 페이징/응답 | `page`, `perPage`, `totalCount`, `currentCount`, `matchCount`, `data`. 범위 밖 페이지는 **HTTP 200, `data=[]`** (apt에서만 관측) | 미확인(apt와 같다고 가정 금지) | 미확인 |
| 인증 오류 | HTTP 401, JSON `{code:-4,msg}` (apt 엔드포인트에서만 관측) | 미확인 | 미확인 |
| XML | `<results><item><col name=...>`, 결측은 JSON null 대신 **빈 문자열** | 미확인(apt만 수집) | 미확인 |

## 미확인·주의 (구현 보류 사유)
- **전용면적 부재 → 결정됨(아래 '설계 결정 반영').** APT·잔여세대 주택형에는 공급면적만 있으며, 전용면적은 미확인으로 유지하고 `HOUSE_TY`로 추정하지 않는다. `exclusive_area_sqm`은 urbty `EXCLUSE_AR`만 채운다.
- **가격 의미.** 필드가 "분양최고금액"이라 주택형별 최고가이며 단일 확정가가 아니다. 예산 판정 문구 검토 필요. 표본 300행에 빈 값·`-`·0은 없어 **미공개 가격의 실제 표현은 미확인**.
- `HOUSE_TY`에 앞뒤 공백 사례(`"084.7646 "`)가 있어 표시/비교 시 정규화하되 키로 쓰지 않는다.
- 일정 이벤트 코드(`source_event_code`, `scope_code`) 이름 체계는 위 필드 목록을 바탕으로 설계 검토 후 확정.
- 계약취소·재공급 회차의 API상 구분, 공고 수정 이력 필드, 다중 페이지 중간 장애·XML 오류 본문 형태는 미확인.
- 지역 코드 → 시도/시군구 표준코드 매핑(`SUBSCRPT_AREA_CODE`는 청약홈 자체 코드) 미확정.
- 오피스텔 엔드포인트는 도시형·오피스텔·민간임대·생활숙박시설을 함께 반환한다.
- 포털이 발급한 키가 URL 인코딩 형태(`%` 포함)이며 이 값 하나뿐이다. 이 값을 그대로 넘기면 401(등록되지 않은 인증키), 한 번 디코딩해 `params`로 넘기면 200임을 실측했다. 도구는 `%`가 있으면 메모리에서 한 번 디코딩한다(`.env` 원본 유지). **수집기·GitHub Secret에도 같은 처리 필요**(이중 인코딩 방지).

## 도구 안전 규칙 (2026-09-29 Codex 리뷰 반영)
- 원본 응답은 기본 `.raw_samples/`(gitignore, `*.raw` 포함)에 저장하며 `fixtures/` 아래로는 저장 거부. 익명화 전 원본은 검토 후 삭제한다.
- `build_fixtures.py`는 `tools/known_fields.json`(Swagger 필드) 밖 필드가 응답에 나타나면 저장 없이 중단(fail-closed), 모든 응답 검증 후에만 파일 기록, 주택형은 두 키로 조회해 `matchCount`까지 전 페이지 수집, 예외는 URL 없이 종류만 출력.
- 가격은 주택형 '최고금액'이므로 예산 이하면 일치, 초과면 미확인으로 판정한다(`contract.housing_type_matches`가 항상 적용, 옵션 없음).

## 설계 결정 반영 (2026-09-29, Codex 설계 결정 → docs 개정)
- 면적: `area_sqm` → `exclusive_area_sqm`(urbty `EXCLUSE_AR`만) + `supply_area_sqm`(apt/remndr `SUPLY_AR`). apt/remndr의 전용면적은 미확인으로 유지하며 `HOUSE_TY`로 추정하지 않는다.
- 가격: `price_krw` → `price_max_krw`(최고금액). 예산 이하면 일치, 초과면 미확인. 이에 따라 `contract.py`의 `price_is_ceiling` 옵션은 제거하고 항상 최고가 의미를 적용한다.
- 가격 출처: 값이 있으면 `price_raw` 비공백 + 확정 단위(KRW/MANWON), 없으면 `UNKNOWN`을 스키마 CHECK로 강제(DB 실행 검증은 미수행).

## 수집기 종단 실행 관찰 (2026-09-29, 실제 API → 로컬 테스트 DB, `--mode backfill --since 2026-09-01`)
- 3개 계열 전 페이지 조회(apt 2,884 / remndr 1,700 / urbty_ofctl 621건) 후 최근 공고 60건(25/27/8)과 주택형 279행을 저장. 실패 0, 중복 0, 매핑 경고 0. 가격 단위는 전부 `MANWON`, 최고금액 범위 3.076억~68.9억, 가격 NULL 0건.
- **잔여세대 주택형 55행 중 47행은 `SUPLY_AR`가 비어 있어 `supply_area_sqm=NULL`**(원천이 비어 있음). 잔여세대는 면적을 대부분 알 수 없으니 화면·필터에서 미확인 처리 필요.
- 확인된 이벤트 조합(공고 60건): `cntrct_cncls`·`przwner_presnatn` 60, `rcept` 25(apt), `spsply_rcept` 26, `gnrl_rnk1/2`의 crsparea·etc_area 25·etc_gg 2, `subscrpt_rcept` 35, `gnrl_rcept` 4. 이벤트 코드는 원천 필드 접두어에서 딴 임시 이름이며 의미 검증은 대기.
- 여전히 미확인: 미공개 가격 표현(이번 표본에도 없음), `MODEL_NO` 수정 간 안정성, remndr/urbty의 빈 페이지·오류·XML 동작.
- 일일 호출량: 이번 실행은 상세 조회 약 53회 + 공고당 1회(60회). 전체 백필은 공고 수(약 5,200)만큼 주택형 호출이 필요하므로 개발계정 일일 한도를 확인한 뒤 실행한다.

