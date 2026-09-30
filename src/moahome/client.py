"""청약홈 공공데이터 API 클라이언트.

- 인증키는 params로만 전달한다(인코딩은 requests가 한 번). 포털이 URL 인코딩된 형태(`%` 포함)로
  발급한 키는 normalize_key에서 한 번만 디코딩한다.
- 오류 메시지·예외에는 요청 URL/키를 넣지 않는다(예외 종류와 op 이름만).
- 5xx·429·네트워크 오류만 제한적으로 재시도한다. 인증/권한/잘못된 요청/XML 오류/JSON 오류 코드는 즉시 실패.
- 전 페이지를 받고 matchCount와 대조한다. 불일치는 부분 응답으로 보고 실패 처리한다.
"""
import math
import random
import time
import xml.etree.ElementTree as ET
from urllib.parse import unquote

import requests

BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
PER_PAGE = 100
RETRY_STATUS = {429, 500, 502, 503, 504}


class ApiError(Exception):
    """kind: network | http | api_error | xml_error | unexpected_structure | incomplete"""

    def __init__(self, op, kind, detail=""):
        super().__init__(f"{op}: {kind}" + (f" ({detail})" if detail else ""))
        self.op, self.kind, self.detail = op, kind, detail


def normalize_key(raw):
    """포털 발급 키가 URL 인코딩 형태(%)이면 한 번만 디코딩한다."""
    raw = (raw or "").strip()
    return unquote(raw) if "%" in raw else raw


def _xml_error(op, body):
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise ApiError(op, "xml_error", "parse") from None
    code = next((e.text for e in root.iter() if e.tag.lower().endswith("reasoncode")), None)
    raise ApiError(op, "xml_error", f"code={code}")


class CheongyakClient:
    def __init__(self, key, session=None, sleep=time.sleep, max_retries=3, base=BASE, per_page=PER_PAGE):
        if not key:
            raise ValueError("API key is empty")
        self._key = key
        self._session = session or requests.Session()
        self._sleep, self._max_retries, self._base, self.per_page = sleep, max_retries, base, per_page

    def _request(self, op, params):
        attempt = 0
        while True:
            try:
                r = self._session.get(f"{self._base}/{op}", timeout=(5, 30),
                                      params={"serviceKey": self._key, **params})
            except requests.RequestException as e:  # e의 메시지에는 URL(키)이 있을 수 있어 사용 금지
                err = ApiError(op, "network", type(e).__name__)
            else:
                if r.status_code == 200:
                    return r
                err = ApiError(op, "http", str(r.status_code))
                if r.status_code not in RETRY_STATUS:
                    raise err
            attempt += 1
            if attempt > self._max_retries:
                raise err
            self._sleep(min(2 ** attempt, 30) + random.random())

    def get_page(self, op, page, cond=None):
        r = self._request(op, {"page": page, "perPage": self.per_page, **(cond or {})})
        body = r.content
        if body.lstrip()[:1] == b"<":
            _xml_error(op, body)
        try:
            j = r.json()
        except ValueError:
            raise ApiError(op, "unexpected_structure", "not json") from None
        if not isinstance(j, dict):
            raise ApiError(op, "unexpected_structure", type(j).__name__)
        if isinstance(j.get("code"), int) and j["code"] < 0:  # data가 함께 있어도 오류 코드면 오류
            raise ApiError(op, "api_error", f"code={j.get('code')}")
        if not isinstance(j.get("data"), list) or not isinstance(j.get("matchCount"), int):
            raise ApiError(op, "unexpected_structure", f"keys={sorted(j)}")
        return j

    def get_all(self, op, cond=None, key_fields=("HOUSE_MANAGE_NO", "PBLANC_NO")):
        """마지막 페이지까지 수집. 반환: (행 목록, totalCount, 중복 제거 수)."""
        rows, seen, dups, page = [], set(), 0, 1
        total = match = None
        while True:
            j = self.get_page(op, page, cond)
            if match is not None and j["matchCount"] != match:  # 수집 중 결과 집합이 바뀜
                raise ApiError(op, "incomplete", "matchCount changed")
            match, total = j["matchCount"], j.get("totalCount", j["matchCount"])
            if page > math.ceil(match / self.per_page) + 1:
                raise ApiError(op, "incomplete", "page limit")
            for rec in j["data"]:
                k = tuple(rec.get(f) for f in key_fields)
                if k in seen:
                    dups += 1
                    continue
                seen.add(k)
                rows.append(rec)
            if not j["data"] or len(rows) >= match:
                break
            page += 1
        if len(rows) != match:
            raise ApiError(op, "incomplete", f"got={len(rows)} match={match}")
        return rows, total, dups
