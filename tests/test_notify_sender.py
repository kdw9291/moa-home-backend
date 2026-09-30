import base64
import json

import pytest
from py_vapid import Vapid
from pywebpush import WebPushException

import pywebpush
from moahome.notify import Target, make_webpush_sender


def raw_key() -> str:
    v = Vapid()
    v.generate_keys()
    return base64.urlsafe_b64encode(v.private_key.private_numbers().private_value.to_bytes(32, "big")).rstrip(b"=").decode()


T = Target("s", "https://push.example/secret-endpoint", "pk", "auth", "aid", "단지", "apt", "1", "2")


class Resp:
    def __init__(self, code):
        self.status_code = code


def sender(monkeypatch, behavior):
    monkeypatch.setattr(pywebpush, "webpush", behavior)
    return make_webpush_sender(raw_key(), "mailto:ops@example.com")


def test_success_sends_json_payload_with_vapid_and_short_ttl(monkeypatch):
    seen = {}

    def ok(**kw):
        seen.update(kw)

    s = sender(monkeypatch, ok)
    assert s(T, {"title": "제목", "body": "본문", "url": "/detail/", "tag": "t"}) == "sent"
    assert json.loads(seen["data"])["title"] == "제목"
    assert seen["subscription_info"]["keys"] == {"p256dh": "pk", "auth": "auth"}
    assert seen["vapid_claims"] == {"sub": "mailto:ops@example.com"} and seen["ttl"] <= 24 * 3600


@pytest.mark.parametrize("code,expected", [(404, "expired"), (410, "expired"), (400, "failed"), (429, "failed"), (500, "failed")])
def test_status_codes_map_to_result(monkeypatch, code, expected):
    def fail(**kw):
        raise WebPushException("x", response=Resp(code))

    assert sender(monkeypatch, fail)(T, {}) == expected


def test_exception_without_response_or_network_error_is_failed_not_raised(monkeypatch):
    def net(**kw):
        raise ConnectionError("connect to https://push.example/secret-endpoint failed")

    assert sender(monkeypatch, net)(T, {}) == "failed"

    def bare(**kw):
        raise WebPushException("no response")

    assert sender(monkeypatch, bare)(T, {}) == "failed"
