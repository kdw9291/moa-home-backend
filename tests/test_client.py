import json

import pytest
import requests

from moahome.client import ApiError, CheongyakClient, normalize_key

SECRET = "SECRET-KEY-VALUE"


class Resp:
    def __init__(self, status=200, body=None, raw=None):
        self.status_code = status
        self.content = raw if raw is not None else json.dumps(body).encode()

    def json(self):
        return json.loads(self.content)


class Session:
    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def get(self, url, timeout=None, params=None):
        self.calls.append((url, dict(params)))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def page(rows, match=None, total=None):
    return {"currentCount": len(rows), "data": rows, "matchCount": len(rows) if match is None else match,
            "page": 1, "perPage": 100, "totalCount": total or len(rows)}


def client(*items, **kw):
    s = Session(*items)
    return CheongyakClient(SECRET, session=s, sleep=lambda _: None, **kw), s


def rec(n):
    return {"HOUSE_MANAGE_NO": str(n), "PBLANC_NO": str(n)}


def test_normalize_key_decodes_once_only_when_encoded():
    assert normalize_key("ab%2Bc%2Fd%3D%3D") == "ab+c/d=="
    assert normalize_key("plain+key/==") == "plain+key/=="
    assert normalize_key("  ") == ""


def test_key_is_sent_in_params_not_in_url():
    c, s = client(Resp(body=page([rec(1)])))
    c.get_all("op")
    url, params = s.calls[0]
    assert SECRET not in url and params["serviceKey"] == SECRET


def test_retries_5xx_then_succeeds():
    c, s = client(Resp(503, {}), Resp(500, {}), Resp(body=page([rec(1)])))
    rows, _, _ = c.get_all("op")
    assert len(rows) == 1 and len(s.calls) == 3


def test_gives_up_after_max_retries():
    c, s = client(*[Resp(500, {})] * 4)
    with pytest.raises(ApiError) as e:
        c.get_all("op")
    assert e.value.kind == "http" and len(s.calls) == 4


def test_auth_error_not_retried():
    c, s = client(Resp(401, {"code": -4, "msg": "x"}))
    with pytest.raises(ApiError) as e:
        c.get_all("op")
    assert e.value.detail == "401" and len(s.calls) == 1


def test_network_error_message_never_contains_secret():
    boom = requests.ConnectionError(f"GET https://x?serviceKey={SECRET} failed")
    c, _ = client(*[boom] * 4)
    with pytest.raises(ApiError) as e:
        c.get_all("op")
    assert SECRET not in str(e.value) and e.value.kind == "network"


def test_http200_json_error_body_is_error():
    c, _ = client(Resp(body={"code": -4, "msg": "등록되지 않은 인증키 입니다."}))
    with pytest.raises(ApiError) as e:
        c.get_all("op")
    assert e.value.kind == "api_error"


def test_http200_xml_error_body_is_error_and_not_retried():
    xml = (b"<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode>"
           b"</cmmMsgHeader></OpenAPI_ServiceResponse>")
    c, s = client(Resp(raw=xml))
    with pytest.raises(ApiError) as e:
        c.get_all("op")
    assert e.value.kind == "xml_error" and "30" in e.value.detail and len(s.calls) == 1


def test_unexpected_structure_rejected():
    c, _ = client(Resp(body={"foo": 1}))
    with pytest.raises(ApiError) as e:
        c.get_all("op")
    assert e.value.kind == "unexpected_structure"


def test_multi_page_collects_all_and_dedupes():
    c, s = client(Resp(body=page([rec(1), rec(2)], match=3)), Resp(body=page([rec(2), rec(3)], match=3)), per_page=2)
    rows, _, dups = c.get_all("op")
    assert [x["HOUSE_MANAGE_NO"] for x in rows] == ["1", "2", "3"] and dups == 1
    assert [call[1]["page"] for call in s.calls] == [1, 2]


def test_partial_response_is_incomplete_not_success():
    c, _ = client(Resp(body=page([rec(1), rec(2)], match=5)), Resp(body=page([], match=5)), per_page=2)
    with pytest.raises(ApiError) as e:
        c.get_all("op")
    assert e.value.kind == "incomplete"


def test_empty_result_with_zero_match_is_ok():
    c, _ = client(Resp(body=page([], match=0, total=10)))
    rows, total, _ = c.get_all("op", cond={"cond[HOUSE_MANAGE_NO::EQ]": "x"})
    assert rows == [] and total == 10


def test_error_code_is_error_even_when_data_list_present():
    body = page([rec(1)])
    body["code"] = -4
    c, _ = client(Resp(body=body))
    with pytest.raises(ApiError) as e:
        c.get_all("op")
    assert e.value.kind == "api_error"


def test_match_count_change_between_pages_is_incomplete():
    # 1쪽은 4건이라 했는데 2쪽은 3건이라고 답하면 수집한 3건이 일치해 보여도 실패
    c, _ = client(Resp(body=page([rec(1), rec(2)], match=4)), Resp(body=page([rec(3)], match=3)), per_page=2)
    with pytest.raises(ApiError) as e:
        c.get_all("op")
    assert e.value.kind == "incomplete" and "changed" in e.value.detail
