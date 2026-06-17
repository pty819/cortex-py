-- 05_entity_resolution.sql — pgvector 召回 + B over C 三阈值分支 + 合并(resolve)demo
-- 依据:01 §4(B over C)、03 §2(entities + merged_into)、§10.3 向量召回、§决策5(合并软引用)
-- 注:用 v3() 合成向量精确控制 cosine(验证 vector 机制 + 阈值逻辑);真实 embedding 质量是 R3(阶段 3 验)。

SET search_path = cortex_stage0, public;
\set scopeA 'org:acme/dept:sales/user:alice'

-- 查询向量 q(模拟新提到 "Bob" 的 embedding)
\set q 'v3(1,0.1,0.02)'

-- 预期(手算,前 3 维决定 cosine):
--   robert_smith v3(1,0,0)   cosine = 1/sqrt(1.0104)       ≈ 0.9948  → MERGE (>0.85)
--   robert_jones v3(1,1,0)   cosine = 1.1/(sqrt(1.0104)*sqrt2) ≈ 0.7737  → GRAYZONE (0.3~0.85)
--   bob_sales v3(0,1,0)      cosine = 0.1/sqrt(1.0104)     ≈ 0.0995  → NEW (<0.3)
--   acme/priya/q3 (x≈0)      cosine ≈ 0.05~0.09             → NEW

-- ── (1) C 层:top-5 最近邻召回(带 scope + merged_into IS NULL 过滤)────────
CREATE OR REPLACE FUNCTION recall_candidates(p_scope text, p_q vector, p_k int)
RETURNS TABLE(entity_id uuid, canonical_name text, cosine float) LANGUAGE sql STABLE AS $$
  SELECT entity_id, canonical_name, 1 - (embedding <=> p_q) AS cosine
    FROM entities
   WHERE scope = p_scope AND merged_into IS NULL
   ORDER BY embedding <=> p_q
   LIMIT p_k
$$;

DO $$
DECLARE top1 text; top1cos float;
BEGIN
  SELECT canonical_name INTO top1 FROM recall_candidates(:'scopeA', :q, 5) LIMIT 1;
  SELECT cosine INTO top1cos FROM recall_candidates(:'scopeA', :q, 5) LIMIT 1;
  IF top1 = 'Robert Smith' THEN RAISE NOTICE 'PASS: top-1 candidate = Robert Smith (cosine=%)', round(top1cos::numeric,4);
  ELSE RAISE NOTICE 'FAIL: top-1 = % (want Robert Smith)', top1; END IF;
END $$;

-- ── (2) B 层 + 阈值兜底:三分支判定 ────────────────────────────────────────
-- >0.85 直接合并(省 LLM);<0.3 直接新建;0.3~0.85 灰区走 B 层 LLM(mock)
-- 先别名精确命中(A 策略降级位):"Bob" 在 aliases 表命中 → 直接 ent_robert_smith,省向量召回
DO $$
DECLARE alias_hit uuid; branch text;
BEGIN
  -- A 层:别名精确查
  SELECT entity_id INTO alias_hit FROM entity_aliases WHERE scope=:'scopeA' AND alias='Bob' LIMIT 1;
  IF alias_hit IS NOT NULL THEN
    RAISE NOTICE 'PASS: A-layer alias exact match "Bob" → entity %', alias_hit;
    RAISE NOTICE '       (skips vector recall + LLM; first-line fast path)';
  ELSE RAISE NOTICE 'FAIL: alias "Bob" not found'; END IF;

  -- C 层 + 阈值:对 "Robert Jones"(grayzone)演示阈值分支
  WITH cand AS (
    SELECT canonical_name, 1-(embedding <=> :q) AS cos
      FROM entities WHERE scope=:'scopeA' AND merged_into IS NULL AND canonical_name='Robert Jones'
  )
  SELECT CASE
           WHEN cos > 0.85 THEN 'MERGE'
           WHEN cos < 0.30 THEN 'NEW'
           ELSE 'GRAYZONE(LLM)' END INTO branch
    FROM cand;
  IF branch = 'GRAYZONE(LLM)' THEN RAISE NOTICE 'PASS: Robert Jones → % (0.3<cos<0.85, would call LLM)', branch;
  ELSE RAISE NOTICE 'FAIL: Robert Jones branch=% (want GRAYZONE)', branch; END IF;
END $$;

-- ── (3) 合并 demo:把一个新 dup "Robert S." 合并进 robert_smith,验证 resolve ──
DO $$
DECLARE dup uuid; active_count int; resolved text;
BEGIN
  INSERT INTO entities (entity_id, scope, canonical_name, entity_type, description, embedding, merged_into, merge_confidence)
  VALUES ('11111111-0000-0000-0000-000000000099', :'scopeA', 'Robert S.', 'person', 'dup', v3(0.99,0.01,0.0),
          '11111111-0000-0000-0000-000000000010', 0.97) RETURNING entity_id INTO dup;

  -- 活实体查询(WHERE merged_into IS NULL)应排除已合并的 dup
  SELECT count(*) INTO active_count FROM entities WHERE scope=:'scopeA' AND canonical_name IN ('Robert Smith','Robert S.') AND merged_into IS NULL;
  IF active_count = 1 THEN RAISE NOTICE 'PASS: merged dup excluded from active set'; ELSE RAISE NOTICE 'FAIL: active count=% (want 1)', active_count; END IF;

  -- resolve 视图:dup → robert_smith
  SELECT canonical_name INTO resolved FROM entities_resolved WHERE entity_id = dup AND resolved_id = '11111111-0000-0000-0000-000000000010';
  IF resolved IS NOT NULL THEN RAISE NOTICE 'PASS: entities_resolved maps dup → Robert Smith (facts need not rewrite)';
  ELSE RAISE NOTICE 'FAIL: resolve view did not map dup'; END IF;
END $$;
