"""DATABASE_SCHEMA.sql / UNSOLD_SCHEMA_PROPOSAL.sql 정적 감사.

PostgreSQL 실제 파서(pglast)로 문법을 검증하고, 테이블별 RLS 활성화와
anon/authenticated 권한(GRANT)·정책(POLICY) 구성을 추출한다.
DB에 연결하지 않으므로 실행 의미(FK 동작, 정책 평가)는 검증하지 않는다.

사용: python tests/sql_static_audit.py [--docs ../../docs]
"""
import argparse
import os
import sys

import pglast
from pglast import ast


def load(path):
    with open(path, encoding="utf-8") as f:
        return pglast.parse_sql(f.read())


def qualified(rv):
    return f"{rv.schemaname or 'public'}.{rv.relname}"


def audit(stmts):
    tables, rls, policies, grants = {}, set(), [], []
    for raw in stmts:
        s = raw.stmt
        if isinstance(s, ast.CreateStmt):
            tables[qualified(s.relation)] = s
        elif isinstance(s, ast.AlterTableStmt):
            for cmd in s.cmds:
                if cmd.subtype == pglast.enums.AlterTableType.AT_EnableRowSecurity:
                    rls.add(qualified(s.relation))
        elif isinstance(s, ast.CreatePolicyStmt):
            roles = [r.rolename for r in (s.roles or [])]
            policies.append((qualified(s.table), s.policy_name, s.cmd_name, roles))
        elif isinstance(s, ast.GrantStmt):
            grants.append(s)
    return tables, rls, policies, grants


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", default=os.path.normpath(os.path.join(here, "..", "..", "..", "docs")))
    args = ap.parse_args()

    rc = 0
    for name in ("DATABASE_SCHEMA.sql", "UNSOLD_SCHEMA_PROPOSAL.sql"):
        path = os.path.join(args.docs, name)
        try:
            stmts = load(path)
        except pglast.parser.ParseError as e:
            print(f"[FAIL] {name}: 문법 오류 {e}")
            rc = 1
            continue
        tables, rls, policies, grants = audit(stmts)
        no_rls = sorted(set(tables) - rls)
        print(f"[OK] {name}: 문 {len(stmts)}개 파싱, 테이블 {len(tables)}, RLS 활성 {len(rls & set(tables))}, 정책 {len(policies)}, GRANT/REVOKE {len(grants)}")
        if no_rls:
            print(f"  [WARN] RLS 미활성 테이블: {no_rls}")
            rc = 1
        policy_tables = {p[0] for p in policies}
        print(f"  정책 없는 RLS 테이블(전면 차단 대상): {sorted((rls & set(tables)) - policy_tables)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
