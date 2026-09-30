"""실제 응답에서 대표 사례를 골라 익명화한 fixture를 fixtures/에 저장한다.

익명화: 사업주체·시공사·신문사명, 문의전화, 홈페이지 주소를 치환. 키·요청 URL은 저장하지 않는다.
공고번호·주소·단지명·금액은 공개 공고 정보라 유지한다(파서·지역·단위 검증에 필요).
안전장치: 알려진 필드(known_fields.json, Swagger 기준) 밖의 필드가 응답에 나타나면 저장하지 않고 중단한다.
모든 응답을 수집·검증한 뒤에만 파일을 쓴다(중간 실패 시 부분 fixture 없음).
예외 메시지에는 요청 URL(=키)이 들어갈 수 있어 예외 종류만 출력한다.
사용: python tools/build_fixtures.py
"""
import json
import os
import sys
import xml.etree.ElementTree as ET
from urllib.parse import unquote

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_samples import BASE, load_key  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "..", "fixtures")
FAMILIES = {
    "apt": ("getAPTLttotPblancDetail", "getAPTLttotPblancMdl"),
    "remndr": ("getRemndrLttotPblancDetail", "getRemndrLttotPblancMdl"),
    "urbty_ofctl": ("getUrbtyOfctlLttotPblancDetail", "getUrbtyOfctlLttotPblancMdl"),
}
MASK = {"BSNS_MBY_NM": "사업주체(익명)", "CNSTRCT_ENTRPS_NM": "시공사(익명)", "NSPRC_NM": "신문사(익명)",
        "MDHS_TELNO": "00000000", "HMPG_ADRES": "https://example.invalid"}
with open(os.path.join(HERE, "known_fields.json"), encoding="utf-8") as _f:
    KNOWN = json.load(_f)

KEY = None


class FixtureError(RuntimeError):
    pass


def get(op, **params):
    """요청 실패는 URL 없이 예외 종류만 담아 재발생."""
    try:
        return requests.get(f"{BASE}/{op}", timeout=(5, 30), params={"serviceKey": KEY, **params})
    except requests.RequestException as e:
        raise FixtureError(f"{op}: 요청 실패 {type(e).__name__}") from None


def get_json(op, **params):
    r = get(op, **params)
    if r.status_code != 200:
        raise FixtureError(f"{op}: HTTP {r.status_code}")
    try:
        j = r.json()
    except ValueError:
        raise FixtureError(f"{op}: JSON 아님 ({r.headers.get('Content-Type')})") from None
    if not isinstance(j, dict) or not isinstance(j.get("data"), list) or "code" in j:
        raise FixtureError(f"{op}: 오류 또는 예상 밖 구조 keys={sorted(j) if isinstance(j, dict) else type(j).__name__}")
    return j


def get_all(op, **cond):
    """마지막 페이지까지 수집하고 matchCount와 일치하는지 검사."""
    rows, page = [], 1
    while True:
        j = get_json(op, page=page, perPage=100, **cond)
        rows += j["data"]
        if len(rows) >= j["matchCount"] or not j["data"]:
            break
        page += 1
        if page > 50:  # 무한 루프 방지(5,000행 초과는 fixture 대상 아님)
            raise FixtureError(f"{op}: 페이지 상한 초과")
    if len(rows) != j["matchCount"]:
        raise FixtureError(f"{op}: 수집 {len(rows)} != matchCount {j['matchCount']}")
    return rows


def anon(op, rec):
    unknown = set(rec) - set(KNOWN[op])
    if unknown:  # 새 필드(연락처 등 가능)는 검토 전 저장 금지
        raise FixtureError(f"{op}: 알 수 없는 필드 {sorted(unknown)} — 익명화 규칙 검토 필요")
    return {k: (MASK[k] if k in MASK and v not in (None, "") else v) for k, v in rec.items()}


def anon_xml(op, text):
    root = ET.fromstring(text)
    allowed = {"results", "currentCount", "matchCount", "page", "perPage", "totalCount", "data", "item", "col"}
    for el in root.iter():
        if el.tag not in allowed or (el.tag != "col" and el.attrib) or (el.tag == "col" and set(el.attrib) != {"name"}):
            raise FixtureError(f"{op}(XML): 알 수 없는 요소/속성 {el.tag} {sorted(el.attrib)}")
    for col in root.iter("col"):
        name = col.get("name")
        if name not in KNOWN[op]:
            raise FixtureError(f"{op}(XML): 알 수 없는 필드 {name}")
        if name in MASK and (col.text or "").strip():
            col.text = MASK[name]
    return ET.tostring(root, encoding="unicode")


def pick_cases(family, rows):
    """정상(필드 가장 충실), 누락 최다, 세부 유형 다른 사례를 고른다. 공고는 두 키로 식별."""
    def null_cnt(r):
        return sum(v is None for v in r.values())
    out, seen = [], set()

    def add(r):
        k = (r["HOUSE_MANAGE_NO"], r["PBLANC_NO"]) if r else None
        if r and k not in seen:
            seen.add(k)
            out.append(r)
    add(min(rows, key=null_cnt))
    add(max(rows, key=null_cnt))
    if family == "apt":
        add(next((r for r in rows if r["HOUSE_SECD"] == "10"), None))
    elif family == "remndr":
        add(next((r for r in rows if r["HOUSE_SECD"] == "06"), None))   # 불법행위 재공급
    return out


def write(files):
    os.makedirs(FIX, exist_ok=True)
    for name, content in files.items():
        with open(os.path.join(FIX, name), "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2) + "\n")


def main():
    global KEY
    KEY = load_key()
    if "%" in KEY:
        KEY = unquote(KEY)
    files = {}
    for fam, (det, mdl) in FAMILIES.items():
        rows = get_json(det, page=1, perPage=100)["data"]
        cases = pick_cases(fam, rows)
        models = []
        for r in cases:
            models += get_all(mdl, **{"cond[HOUSE_MANAGE_NO::EQ]": r["HOUSE_MANAGE_NO"],
                                      "cond[PBLANC_NO::EQ]": r["PBLANC_NO"]})
        files[f"{fam}_detail_cases.json"] = {"_note": "실제 응답에서 선택한 대표 사례(익명화)", "data": [anon(det, r) for r in cases]}
        files[f"{fam}_model_cases.json"] = {"_note": "위 공고들의 주택형 전체 응답", "data": [anon(mdl, m) for m in models]}
        print(fam, "공고", len(cases), "주택형", len(models))
    # 응답 형식 사례(apt만 수집: 다른 계열의 동작은 미확인)
    op = "getAPTLttotPblancDetail"
    empty = get(op, page=99999, perPage=10)
    eb = empty.json() if empty.status_code == 200 else None
    if not isinstance(eb, dict) or eb.get("data") != [] or not set(eb) <= {"currentCount", "matchCount", "page", "perPage", "totalCount", "data"}:
        raise FixtureError(f"{op}: 빈 페이지 응답이 예상과 다름")
    files["apt_detail_empty_page.json"] = {"http": empty.status_code, "body": eb}
    xml = get(op, page=1, perPage=1, returnType="XML")
    if xml.status_code != 200:
        raise FixtureError(f"{op}(XML): HTTP {xml.status_code}")
    files["apt_detail_sample.xml"] = anon_xml(op, xml.text)
    try:
        bad = requests.get(f"{BASE}/{op}", timeout=(5, 30), params={"serviceKey": "invalid", "page": 1, "perPage": 1})
    except requests.RequestException as e:
        raise FixtureError(f"오류 사례 요청 실패 {type(e).__name__}") from None
    files["error_unregistered_key.json"] = {"http": bad.status_code, "body": bad.json()}
    write(files)
    print("저장 완료", len(files), "파일")


if __name__ == "__main__":
    try:
        main()
    except FixtureError as e:
        sys.exit(f"중단: {e}")
