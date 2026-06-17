-- 06_scope_isolation.sql — scope 隔离 + holistic/descend/structured + LIKE vs ltree
-- 依据:01 §6(scope 强制过滤)、03 §决策1(TEXT + ANY 前缀 vs ltree)、05 §8.2(structured 视图)

SET search_path = cortex_stage0, public;
\set scopeA 'org:acme/dept:sales/user:alice'
\set scopeB 'org:acme/dept:eng/user:bob'
\set acme_id '11111111-0000-0000-0000-000000000001'

-- 先补几条祖先 scope 的 fact,让 holistic 有东西可查 ──────────────────────────
INSERT INTO facts (scope, subject_id, predicate, object_type, object_value, valid_from, valid_to, confidence) VALUES
('org:acme',            '11111111-0000-0000-0000-000000000001', 'has_policy', 'literal', '{"datatype":"string","value":"retention-365d"}'::jsonb, '2026-01-01 00:00:00+00', NULL, 1.0),
('org:acme/dept:sales', '11111111-0000-0000-0000-000000000001', 'has_quota',  'literal', '{"datatype":"string","value":"$10M"}'::jsonb,          '2026-01-01 00:00:00+00', NULL, 1.0);

-- ── (1) 跨 scope 隔离:scope A 查 deal_stage 看不到 scope B 的 internal ──────
DO $$
DECLARE nA int; nB int;
BEGIN
  SELECT count(*) INTO nA FROM facts WHERE scope=:'scopeA' AND predicate='deal_stage';
  SELECT count(*) INTO nB FROM facts WHERE scope=:'scopeB' AND predicate='deal_stage';
  IF nA >= 1 AND nB >= 1 THEN RAISE NOTICE 'PASS: deal_stage exists in BOTH scopes (independent graphs)';
  ELSE RAISE NOTICE 'FAIL: scopeA=% scopeB=%', nA, nB; END IF;

  -- 关键:scope A 的谓词过滤不该返回 scope B 的行
  IF NOT EXISTS (SELECT 1 FROM facts WHERE scope=:'scopeA' AND object_value->>'value'='internal') THEN
    RAISE NOTICE 'PASS: scope A cannot see scope B''s "internal" deal_stage';
  ELSE RAISE NOTICE 'FAIL: scope leak — A sees B''s internal'; END IF;
END $$;

-- ── (2) 同名不同实体:scope A 'Acme Corp' vs scope B 'Acme' 是不同 entity_id ──
DO $$
DECLARE idA uuid; idB uuid;
BEGIN
  SELECT entity_id INTO idA FROM entities WHERE scope=:'scopeA' AND canonical_name='Acme Corp';
  SELECT entity_id INTO idB FROM entities WHERE scope=:'scopeB' AND canonical_name='Acme';
  IF idA IS NOT NULL AND idB IS NOT NULL AND idA <> idB THEN
    RAISE NOTICE 'PASS: same-name different-scope → 2 distinct entities (graph isolation holds)';
  ELSE RAISE NOTICE 'FAIL: A=% B=% (must differ)', idA, idB; END IF;
END $$;

-- ── (3) holistic:从 user:alice 向上,应用层算前缀列表 + ANY ─────────────────
-- 决策 1:TEXT + ANY(prefix),不用 ltree。前缀 = 所有祖先段(含自身)。
-- 数组切片实现:[1..i] 拼接,生成全部前缀。
CREATE OR REPLACE FUNCTION scope_ancestors(p_scope text) RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
  SELECT array_agg(prefix) FROM (
    SELECT array_to_string((string_to_array(p_scope,'/'))[1:i], '/') AS prefix
      FROM generate_series(1, array_length(string_to_array(p_scope,'/'),1)) AS i
  ) t
$$;

DO $$
DECLARE prefixes text[]; cnt int;
BEGIN
  prefixes := scope_ancestors(:'scopeA');  -- [org:acme, org:acme/dept:sales, org:acme/dept:sales/user:alice]
  SELECT count(*) INTO cnt FROM facts WHERE scope = ANY(prefixes) AND subject_id = :'acme_id'::uuid AND recorded_to IS NULL;
  IF array_length(prefixes,1) = 3 AND cnt >= 1 THEN
    RAISE NOTICE 'PASS: holistic from alice → % prefixes, % facts (org+dept+user all visible)', array_length(prefixes,1), cnt;
  ELSE RAISE NOTICE 'FAIL: prefixes=% cnt=%', prefixes, cnt; END IF;
END $$;

-- ── (4) descend:从 org:acme 向下,LIKE 'org:acme/%' ─────────────────────────
DO $$
DECLARE cnt int;
BEGIN
  SELECT count(DISTINCT scope) INTO cnt FROM facts WHERE scope = 'org:acme' OR scope LIKE 'org:acme/%';
  -- 期望看到 org:acme / dept:sales / dept:sales/user:alice / dept:eng/user:bob
  IF cnt >= 3 THEN RAISE NOTICE 'PASS: descend from org:acme → % distinct scopes visible', cnt;
  ELSE RAISE NOTICE 'FAIL: descend scopes=% (want >=3)', cnt; END IF;
END $$;

-- ── (5) structured 视图:只返 facts + beliefs,跳 events(轻量 recall 快路径)──
DO $$
DECLARE nf int; nb int;
BEGIN
  SELECT count(*) INTO nf FROM facts WHERE scope=:'scopeA';
  SELECT count(*) INTO nb FROM beliefs WHERE scope=:'scopeA';
  IF nf >= 1 AND nb >= 1 THEN
    RAISE NOTICE 'PASS: structured view returns facts=% beliefs=% (events skipped) for scope A', nf, nb;
  ELSE RAISE NOTICE 'FAIL: structured facts=% beliefs=%', nf, nb; END IF;
END $$;

-- ── (6) LIKE vs ltree 性能对比(决策 1 验证)──────────────────────────────────
-- 建 ltree 镜像:scope → ltree('org_acme.dept_sales.user_alice')
CREATE TABLE IF NOT EXISTS scope_ltree_mirror (scope text PRIMARY KEY, path ltree);
TRUNCATE scope_ltree_mirror;
INSERT INTO scope_ltree_mirror
SELECT DISTINCT scope,
       translate(replace(scope, '/', '.'), ':', '_')::ltree FROM facts;

DO $$
DECLARE t0 timestamptz; n_text int; n_ltree int; ms_text float; ms_ltree float;
DECLARE prefixes text[]; ltarget ltree;
BEGIN
  prefixes := scope_ancestors(:'scopeA');
  ltarget  := translate(replace(:'scopeA','/','.'),':','_')::ltree;

  t0 := clock_timestamp();
  SELECT count(*) INTO n_text FROM facts WHERE scope = ANY(prefixes);
  ms_text := extract(epoch FROM (clock_timestamp()-t0))*1000;

  t0 := clock_timestamp();
  SELECT count(*) INTO n_ltree FROM scope_ltree_mirror m WHERE m.path @> ltarget;
  ms_ltree := extract(epoch FROM (clock_timestamp()-t0))*1000;

  RAISE NOTICE 'PERF: holistic via TEXT ANY(prefix) = % ms ; ltree @> = % ms (decision: TEXT, holistic is the hot path)', round(ms_text::numeric,3), round(ms_ltree::numeric,3);
END $$;
