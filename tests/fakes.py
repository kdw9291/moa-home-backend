"""수집 테스트용 가짜 클라이언트: 실제 익명화 fixture를 API 응답처럼 돌려준다."""
import json
import os

from moahome.client import ApiError
from moahome.mappers import OPS

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)["data"]


class FixtureClient:
    """fail_models: {(house_manage_no): ApiError kind}로 특정 공고의 주택형 조회를 실패시킨다."""

    def __init__(self, fail_models=None, fail_detail=None, total_override=None):
        self.data = {}
        for fam, (det, mdl) in OPS.items():
            self.data[det] = load(f"{fam}_detail_cases.json")
            self.data[mdl] = load(f"{fam}_model_cases.json")
        self.fail_models, self.fail_detail = fail_models or {}, fail_detail or {}
        self.total_override = total_override or {}
        self.calls = []

    def get_all(self, op, cond=None, key_fields=("HOUSE_MANAGE_NO", "PBLANC_NO")):
        self.calls.append((op, dict(cond or {})))
        if op in self.fail_detail:
            raise ApiError(op, self.fail_detail[op])
        rows = self.data[op]
        if cond:
            hmn, pno = cond["cond[HOUSE_MANAGE_NO::EQ]"], cond["cond[PBLANC_NO::EQ]"]
            if hmn in self.fail_models:
                raise ApiError(op, self.fail_models[hmn])
            rows = [r for r in rows if (r["HOUSE_MANAGE_NO"], r["PBLANC_NO"]) == (hmn, pno)]
        total = self.total_override.get(op, len(rows))
        return [dict(r) for r in rows], total, 0
