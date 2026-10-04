-- 단계 E / E1-B 되돌리기: 회수 단계(20261002_02_revoke_direct_filter_writes.sql)를 취소해 authenticated 의 직접 쓰기를 복구한다.
-- 용도: 새 클라이언트(RPC 저장)에 문제가 생겨 구버전 클라이언트(직접 upsert)로 급히 돌아가야 할 때만 쓴다.
-- 이 파일은 RPC 함수·요청 기록 테이블(E1-A)은 지우지 않는다. 실행하면 직접 쓰기가 다시 가능해지므로 RPC의 revision 보호
-- (늦은 저장 덮어쓰기 방지)도 다시 무력해진다는 점에 유의한다. 여러 번 실행해도 결과가 같다.
BEGIN;

DROP POLICY IF EXISTS own_filter ON public.user_filter_settings;
CREATE POLICY own_filter ON public.user_filter_settings
    FOR ALL TO authenticated USING ((SELECT auth.uid()) = user_id)
    WITH CHECK ((SELECT auth.uid()) = user_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.user_filter_settings TO authenticated;

COMMIT;
