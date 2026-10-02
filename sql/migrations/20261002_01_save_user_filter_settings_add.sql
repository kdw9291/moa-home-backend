-- 단계 E / E1-A (추가 단계): 필터 저장 RPC와 요청 기록 테이블을 추가한다.
-- 이 파일은 기존 직접 쓰기 권한을 건드리지 않으므로, 구버전 클라이언트가 동작하는 동안 먼저 적용해도 안전하다.
-- 계약 원본: docs/DATABASE_SCHEMA.sql, docs/API_SPEC.md (필터 저장 RPC). 함수·테이블 정의는 그 원문과 같아야 한다
-- (동일성은 tests/test_filter_save_rpc_db.py 의 '신규 스키마 = 마이그레이션 결과' 테스트가 확인한다).
-- 여러 번 실행해도 결과가 같다(IF NOT EXISTS / CREATE OR REPLACE / REVOKE·GRANT 재실행).
BEGIN;

-- 함수 소유자는 user_filter_settings·요청 기록 테이블의 RLS를 우회할 수 있는 신뢰된 역할이어야 한다
-- (Supabase의 postgres, 로컬 테스트의 슈퍼유저). 그렇지 않으면 SECURITY DEFINER 쓰기가 RLS에 막히므로 여기서 중단한다.
DO $guard$
BEGIN
    IF NOT (SELECT rolsuper OR rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'migration role % must be superuser or BYPASSRLS to own save_user_filter_settings', current_user;
    END IF;
    IF to_regclass('public.user_filter_settings') IS NULL THEN
        RAISE EXCEPTION 'public.user_filter_settings is missing: apply the base schema first';
    END IF;
END
$guard$;

CREATE TABLE IF NOT EXISTS public.user_filter_save_requests (
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    request_input jsonb NOT NULL, -- canonical JSONB identity: expected revision + all six raw inputs
    applied_revision integer NOT NULL CHECK (applied_revision > 0),
    result_current jsonb NOT NULL, -- snapshot returned by the successful call
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_filter_save_requests_created_at
    ON public.user_filter_save_requests(created_at);

-- 요청 기록 테이블: RLS를 켜고 클라이언트 역할 권한을 모두 막는다(RPC 소유자만 접근).
ALTER TABLE public.user_filter_save_requests ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.user_filter_save_requests FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.user_filter_save_requests TO service_role;

CREATE OR REPLACE FUNCTION public.save_user_filter_settings(
    p_preferred_region_codes text[],
    p_budget_max_krw bigint,
    p_min_area_sqm numeric,
    p_max_area_sqm numeric,
    p_housing_families text[],
    p_qualification_preferences text[],
    p_expected_revision integer,
    p_request_id uuid
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    v_user_id uuid := auth.uid();
    v_input jsonb;
    v_prior public.user_filter_save_requests%ROWTYPE;
    v_settings public.user_filter_settings%ROWTYPE;
    v_current jsonb;
    v_min_area_sqm numeric(10,2);
    v_max_area_sqm numeric(10,2);
BEGIN
    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'authentication required' USING ERRCODE = '28000';
    END IF;

    -- Compare the complete call, including the expected revision, before
    -- validation so a reused request ID with different input always fails.
    v_input := pg_catalog.jsonb_build_object(
        'preferred_region_codes', p_preferred_region_codes,
        'budget_max_krw', p_budget_max_krw,
        'min_area_sqm', p_min_area_sqm,
        'max_area_sqm', p_max_area_sqm,
        'housing_families', p_housing_families,
        'qualification_preferences', p_qualification_preferences,
        'expected_revision', p_expected_revision);
    IF p_request_id IS NULL THEN
        RAISE EXCEPTION 'request_id is required' USING ERRCODE = '22023';
    END IF;

    -- Per-account serialization also covers the missing-row first insert and
    -- the request ledger. A hash collision only serializes extra accounts.
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(v_user_id::text, 0));
    SELECT * INTO v_prior FROM public.user_filter_save_requests
        WHERE user_id = v_user_id AND request_id = p_request_id;
    IF FOUND THEN
        IF v_prior.request_input <> v_input THEN
            RAISE EXCEPTION 'request_id reused with different input'
                USING ERRCODE = '22023';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'status', 'saved', 'applied_revision', v_prior.applied_revision,
            'replayed', true, 'current', v_prior.result_current);
    END IF;

    IF p_expected_revision IS NULL OR p_expected_revision < 0
       OR p_preferred_region_codes IS NULL OR p_housing_families IS NULL
       OR p_qualification_preferences IS NULL THEN
        RAISE EXCEPTION 'invalid filter input or expected_revision'
            USING ERRCODE = '22023';
    END IF;
    IF p_budget_max_krw < 0 THEN
        RAISE EXCEPTION 'budget_max_krw must be nonnegative'
            USING ERRCODE = '22023';
    END IF;
    -- The assignment uses the column's NUMERIC(10,2) rounding and range.
    -- Out-of-range assignments raise 22003 before any settings write.
    v_min_area_sqm := p_min_area_sqm;
    v_max_area_sqm := p_max_area_sqm;
    IF (v_min_area_sqm IS NOT NULL AND
        (v_min_area_sqm <= 0 OR v_min_area_sqm = 'NaN'::numeric))
       OR (v_max_area_sqm IS NOT NULL AND
           (v_max_area_sqm <= 0 OR v_max_area_sqm = 'NaN'::numeric))
       OR (v_min_area_sqm IS NOT NULL AND v_max_area_sqm IS NOT NULL
           AND v_min_area_sqm > v_max_area_sqm) THEN
        RAISE EXCEPTION 'invalid rounded area range' USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_settings FROM public.user_filter_settings
        WHERE user_id = v_user_id FOR UPDATE;
    IF NOT FOUND THEN
        IF p_expected_revision = 0 THEN
            INSERT INTO public.user_filter_settings (
                user_id, preferred_region_codes, budget_max_krw,
                min_area_sqm, max_area_sqm, housing_families,
                qualification_preferences, revision)
            VALUES (
                v_user_id, p_preferred_region_codes, p_budget_max_krw,
                v_min_area_sqm, v_max_area_sqm, p_housing_families,
                p_qualification_preferences, 1)
            ON CONFLICT (user_id) DO NOTHING
            RETURNING * INTO v_settings;
            IF FOUND THEN
                v_current := pg_catalog.jsonb_set(
                    pg_catalog.to_jsonb(v_settings) - 'user_id' - 'updated_at',
                    '{budget_max_krw}',
                    CASE WHEN v_settings.budget_max_krw IS NULL THEN 'null'::jsonb
                         ELSE pg_catalog.to_jsonb(v_settings.budget_max_krw::text) END);
            END IF;
        END IF;
    ELSIF v_settings.revision = p_expected_revision THEN
        IF v_settings.revision = 2147483647 THEN
            RAISE EXCEPTION 'revision exceeds integer range'
                USING ERRCODE = '22003';
        END IF;
        UPDATE public.user_filter_settings
        SET preferred_region_codes = p_preferred_region_codes,
            budget_max_krw = p_budget_max_krw,
            min_area_sqm = v_min_area_sqm,
            max_area_sqm = v_max_area_sqm,
            housing_families = p_housing_families,
            qualification_preferences = p_qualification_preferences,
            revision = revision + 1,
            updated_at = pg_catalog.now()
        WHERE user_id = v_user_id AND revision = p_expected_revision
        RETURNING * INTO v_settings;
        IF FOUND THEN
            v_current := pg_catalog.jsonb_set(
                pg_catalog.to_jsonb(v_settings) - 'user_id' - 'updated_at',
                '{budget_max_krw}',
                CASE WHEN v_settings.budget_max_krw IS NULL THEN 'null'::jsonb
                     ELSE pg_catalog.to_jsonb(v_settings.budget_max_krw::text) END);
        END IF;
    END IF;

    IF v_current IS NULL THEN
        -- A missing row is represented by defaults and revision 0.
        SELECT * INTO v_settings FROM public.user_filter_settings
            WHERE user_id = v_user_id;
        IF FOUND THEN
            v_current := pg_catalog.jsonb_set(
                pg_catalog.to_jsonb(v_settings) - 'user_id' - 'updated_at',
                '{budget_max_krw}',
                CASE WHEN v_settings.budget_max_krw IS NULL THEN 'null'::jsonb
                     ELSE pg_catalog.to_jsonb(v_settings.budget_max_krw::text) END);
        ELSE
            v_current := pg_catalog.jsonb_build_object(
                'preferred_region_codes', pg_catalog.jsonb_build_array(),
                'budget_max_krw', NULL, 'min_area_sqm', NULL,
                'max_area_sqm', NULL,
                'housing_families', pg_catalog.jsonb_build_array(),
                'qualification_preferences', pg_catalog.jsonb_build_array(),
                'revision', 0);
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'status', 'conflict', 'current', v_current);
    END IF;

    INSERT INTO public.user_filter_save_requests (
        user_id, request_id, request_input, applied_revision, result_current)
    VALUES (v_user_id, p_request_id, v_input, v_settings.revision, v_current);
    RETURN pg_catalog.jsonb_build_object(
        'status', 'saved', 'applied_revision', v_settings.revision,
        'replayed', false, 'current', v_current);
END;
$function$;

-- PostgreSQL grants EXECUTE on new functions to PUBLIC by default.
REVOKE ALL ON FUNCTION public.save_user_filter_settings(
    text[], bigint, numeric, numeric, text[], text[], integer, uuid)
    FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.save_user_filter_settings(
    text[], bigint, numeric, numeric, text[], text[], integer, uuid)
    TO authenticated;

COMMIT;
