-- 단계 E / E1-B (회수 단계): authenticated 의 user_filter_settings 직접 쓰기를 막고 본인 행 SELECT 만 남긴다.
-- 반드시 E1-A 적용 후, 그리고 RPC를 쓰는 클라이언트가 배포된 뒤에 적용한다(구버전 클라이언트의 직접 upsert는 이후 실패한다).
-- 여러 번 실행해도 결과가 같다.
BEGIN;

DO $guard$
BEGIN
    IF to_regprocedure('public.save_user_filter_settings(text[], bigint, numeric, numeric, text[], text[], integer, uuid)') IS NULL THEN
        RAISE EXCEPTION 'save_user_filter_settings is missing: apply E1-A first';
    END IF;
END
$guard$;

-- 기존 FOR ALL 정책(own_filter)을 SELECT 전용으로 교체한다.
DROP POLICY IF EXISTS own_filter ON public.user_filter_settings;
CREATE POLICY own_filter ON public.user_filter_settings
    FOR SELECT TO authenticated USING ((SELECT auth.uid()) = user_id);

REVOKE INSERT, UPDATE, DELETE ON public.user_filter_settings FROM authenticated;
GRANT SELECT ON public.user_filter_settings TO authenticated;

COMMIT;
