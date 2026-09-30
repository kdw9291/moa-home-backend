"""청약홈 API 원본 응답 샘플 수집 (개발용, fixture 작성 전 단계).

.env의 DATA_GO_KR_API_KEY(디코딩 키)를 읽어 계열별 1페이지를 받아 --out 폴더에 저장한다(기본 .raw_samples/, gitignore 대상).
키와 요청 URL은 출력하지 않는다. 저장물은 검토·익명화 후에만 fixtures/로 옮긴다.
사용: python tools/fetch_samples.py --out <폴더> [--per-page 5]
"""
import argparse
import json
import os
import sys
from urllib.parse import unquote

import requests

BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
OPS = [
    "getAPTLttotPblancDetail", "getAPTLttotPblancMdl",
    "getRemndrLttotPblancDetail", "getRemndrLttotPblancMdl",
    "getUrbtyOfctlLttotPblancDetail", "getUrbtyOfctlLttotPblancMdl",
]


def load_key():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("DATA_GO_KR_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".raw_samples"))
    ap.add_argument("--per-page", type=int, default=5)
    a = ap.parse_args()
    key = load_key()
    if not key:
        sys.exit("DATA_GO_KR_API_KEY 비어 있음")
    if "%" in key:  # Encoding 키가 저장된 경우: 메모리에서 한 번만 디코딩(.env는 수정하지 않음)
        print("경고: .env에 인코딩된 키로 보이는 값이 있어 디코딩해 사용합니다. Decoding 키로 교체 권장")
        key = unquote(key)
    fix = os.path.normcase(os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures")))
    if os.path.normcase(os.path.realpath(a.out)).startswith(fix):
        sys.exit("원본 응답은 fixtures/ 아래에 저장할 수 없음(익명화 전)")
    root = os.path.normcase(os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
    if not (os.path.normcase(os.path.realpath(a.out)) + os.sep).startswith(root + os.sep):
        sys.exit("--out은 source/backend 아래여야 함(gitignore 보호 범위)")
    os.makedirs(a.out, exist_ok=True)
    for op in OPS:
        try:
            # params로 전달해 인코딩은 requests가 한 번만 수행
            r = requests.get(f"{BASE}/{op}", timeout=(5, 30),
                             params={"serviceKey": key, "page": 1, "perPage": a.per_page})
            status, ctype, body = r.status_code, r.headers.get("Content-Type", ""), r.content
        except requests.RequestException as e:
            print(f"{op}: 요청 실패 {type(e).__name__}")
            continue
        with open(os.path.join(a.out, f"{op}.raw"), "wb") as f:
            f.write(body)
        summary = ""
        if "json" in ctype.lower():
            try:
                j = json.loads(body)
                summary = f"keys={list(j)[:8]} totalCount={j.get('totalCount')} currentCount={j.get('currentCount')}"
            except ValueError:
                summary = "JSON 파싱 실패"
        print(f"{op}: HTTP {status} {ctype.split(';')[0]} {len(body)}B {summary}")


if __name__ == "__main__":
    main()
