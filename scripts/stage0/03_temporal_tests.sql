-- 03_temporal_tests.sql — 双时态超替 + timeline + as_of / as_known
-- 依据:03 §决策4(valid_to 闭合 + 查询过滤)、§10.1 timeline 查询
-- 断言方式:DO 块 + RAISE NOTICE 'PASS:...' / 'FAIL:...'

SET search_path = cortex_stage0, public;
\set scopeA 'org:acme/dept:sales/user:alice'
\set acme_id '11111111-0000-0000-0000-000000000001'

-- ── (1) 种子超替链:timeline 应返回 3 版本,valid_from 升序,仅最后一条 valid_to IS NULL ──
DO $$
DECLARE n int; cur text;
BEGIN
  SELECT count(*) INTO n FROM facts
   WHERE scope = :'scopeA' AND subject_id = :'acme_id'::uuid AND predicate='deal_stage' AND recorded_to IS NULL;
  IF n = 3 THEN RAISE NOTICE 'PASS: timeline has 3 versions'; ELSE RAISE NOTICE 'FAIL: timeline count=% (want 3)', n; END IF;

  SELECT (object_value->>'value') INTO cur FROM facts
   WHERE scope = :'scopeA' AND subject_id = :'acme_id'::uuid AND predicate='deal_stage'
     AND valid_to IS NULL AND recorded_to IS NULL;
  IF cur = 'signed' THEN RAISE NOTICE 'PASS: current value = signed'; ELSE RAISE NOTICE 'FAIL: current=% (want signed)', cur; END IF;
END $$;

-- ── (2) as_of 查询:t 时刻什么是真(valid_from <= t < valid_to)─────────────
--   2026-03-01 → poc ; 2026-05-01 → close
CREATE OR REPLACE FUNCTION as_of_value(p_scope text, p_subj uuid, p_pred text, p_t timestamptz)
RETURNS text LANGUAGE sql STABLE AS $$
  SELECT object_value->>'value' FROM facts
   WHERE scope=p_scope AND subject_id=p_subj AND predicate=p_pred AND recorded_to IS NULL
     AND valid_from <= p_t AND (valid_to IS NULL OR p_t < valid_to)
   ORDER BY valid_from DESC LIMIT 1
$$;

DO $$
DECLARE v1 text; v2 text;
BEGIN
  v1 := as_of_value(:'scopeA', :'acme_id'::uuid, 'deal_stage', '2026-03-01'::timestamptz);
  v2 := as_of_value(:'scopeA', :'acme_id'::uuid, 'deal_stage', '2026-05-01'::timestamptz);
  IF v1 = 'poc'   THEN RAISE NOTICE 'PASS: as_of 2026-03-01 = poc';   ELSE RAISE NOTICE 'FAIL: as_of 2026-03-01 = % (want poc)',   v1; END IF;
  IF v2 = 'close' THEN RAISE NOTICE 'PASS: as_of 2026-05-01 = close'; ELSE RAISE NOTICE 'FAIL: as_of 2026-05-01 = % (want close)', v2; END IF;
END $$;

-- ── (3) 实时超替操作:新证据"churned"@2026-06-01 → 闭合 signed 的 valid_to + 插入新 fact ──
DO $$
DECLARE n int; cur text; signed_id uuid := '33333333-0000-0000-0000-000000000003';
BEGIN
  -- 1. 闭合当前活 fact(signed)的 valid_to
  UPDATE facts SET valid_to = '2026-06-01 00:00:00+00'
   WHERE fact_id = signed_id;
  -- 2. 插入新 fact(churned)
  INSERT INTO facts (scope, subject_id, predicate, object_type, object_value, valid_from, valid_to, confidence)
  VALUES (:'scopeA', :'acme_id'::uuid, 'deal_stage', 'literal',
          '{"datatype":"string","value":"churned"}'::jsonb, '2026-06-01 00:00:00+00', NULL, 0.7);

  SELECT count(*) INTO n FROM facts
   WHERE scope = :'scopeA' AND subject_id = :'acme_id'::uuid AND predicate='deal_stage' AND recorded_to IS NULL;
  IF n = 4 THEN RAISE NOTICE 'PASS: live supersession → timeline now 4 versions'; ELSE RAISE NOTICE 'FAIL: timeline=% (want 4)', n; END IF;

  SELECT (object_value->>'value') INTO cur FROM facts
   WHERE scope = :'scopeA' AND subject_id = :'acme_id'::uuid AND predicate='deal_stage'
     AND valid_to IS NULL AND recorded_to IS NULL;
  IF cur = 'churned' THEN RAISE NOTICE 'PASS: current value = churned after supersession'; ELSE RAISE NOTICE 'FAIL: current=% (want churned)', cur; END IF;

  -- signed 现在闭合了(valid_to = 06-01),as_of 2026-05-15 仍应返回 signed(05-13<=05-15<06-01)
  IF as_of_value(:'scopeA', :'acme_id'::uuid, 'deal_stage', '2026-05-15'::timestamptz) = 'signed' THEN
    RAISE NOTICE 'PASS: as_of 2026-05-15 still = signed (supersession preserves history)';
  ELSE RAISE NOTICE 'FAIL: as_of 2026-05-15 not signed'; END IF;
END $$;

-- ── (4) as_known 查询(recorded 轴):用 recorded_from/recorded_to ────────────
-- 种子 facts 的 recorded_from = now()(seed 插入时刻),所以 as_known 一个很早的时刻 = 0 条
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM facts
   WHERE scope = :'scopeA' AND subject_id = :'acme_id'::uuid AND predicate='deal_stage'
     AND recorded_from <= '2000-01-01'::timestamptz AND (recorded_to IS NULL OR '2000-01-01'::timestamptz < recorded_to);
  IF n = 0 THEN RAISE NOTICE 'PASS: as_known 2000 = 0 (system did not know yet)'; ELSE RAISE NOTICE 'FAIL: as_known 2000 = % (want 0)', n; END IF;
END $$;
