-- 04_graph_traversal.sql — 递归 CTE 图遍历 BFS + 性能计时 + 反向点查
-- 依据:03 §4d(递归 CTE 在 facts 自连接)、§决策6(双向索引价值在反向点查,不在 BFS)
-- 种子图:acme_sales --employs--> priya --owns--> q3 ; priya --works_at--> acme_sales

SET search_path = cortex_stage0, public;
\set scopeA 'org:acme/dept:sales/user:alice'
\set acme_id '11111111-0000-0000-0000-000000000001'

-- ── BFS 函数:seed 出发,max_hops 跳,可选 predicate 过滤,返回可达 entity 节点(去重)──
-- CTE 内部强制 scope + 双时态(WHERE valid_to IS NULL AND recorded_to IS NULL)+ 谓词过滤。
CREATE OR REPLACE FUNCTION bfs_reachable(p_scope text, p_seed uuid, p_max_hops int, p_preds text[] DEFAULT ARRAY[]::text[])
RETURNS TABLE(node uuid, hop int, predicate text) LANGUAGE sql STABLE AS $$
  WITH RECURSIVE gw AS (
    SELECT subject_id, object_entity_id AS node, predicate, 1 AS hop
      FROM facts
     WHERE subject_id = p_seed AND scope = p_scope
       AND valid_to IS NULL AND recorded_to IS NULL
       AND object_entity_id IS NOT NULL
       AND (cardinality(p_preds) = 0 OR predicate = ANY(p_preds))
    UNION ALL
    SELECT f.subject_id, f.object_entity_id, f.predicate, gw.hop + 1
      FROM facts f
      JOIN gw ON f.subject_id = gw.node
     WHERE f.scope = p_scope
       AND f.valid_to IS NULL AND f.recorded_to IS NULL
       AND f.object_entity_id IS NOT NULL
       AND gw.hop < p_max_hops
       AND (cardinality(p_preds) = 0 OR f.predicate = ANY(p_preds))
  )
  SELECT DISTINCT ON (node) node, hop, predicate FROM gw
   WHERE node IS NOT NULL AND node <> p_seed ORDER BY node, hop
$$;

-- ── (1) seed=acme, max_hops=2,全谓词 → 应可达 {priya, q3} ──────────────────
DO $$
DECLARE n int; reached text;
BEGIN
  SELECT string_agg(e.canonical_name, ',' ORDER BY e.canonical_name) INTO reached
    FROM bfs_reachable(:'scopeA', :'acme_id'::uuid, 2) b
    JOIN entities e ON e.entity_id = b.node;
  SELECT count(*) INTO n FROM bfs_reachable(:'scopeA', :'acme_id'::uuid, 2);
  IF n = 2 AND reached LIKE '%Priya%' AND reached LIKE '%Q3%' THEN
    RAISE NOTICE 'PASS: 2-hop BFS from Acme reaches 2 nodes: %', reached;
  ELSE RAISE NOTICE 'FAIL: 2-hop BFS nodes=% reached=[%] (want Priya Rao + Q3 Renewal)', n, reached; END IF;
END $$;

-- ── (2) max_hops=3 → 同可达集(小图,无新节点)──────────────────────────────
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM bfs_reachable(:'scopeA', :'acme_id'::uuid, 3);
  IF n = 2 THEN RAISE NOTICE 'PASS: 3-hop BFS reaches same 2 nodes (graph converges)'; ELSE RAISE NOTICE 'FAIL: 3-hop=% (want 2)', n; END IF;
END $$;

-- ── (3) 谓词过滤:仅 'owns' → 从 acme 出发无 owns 出边 → 0 节点 ─────────────
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM bfs_reachable(:'scopeA', :'acme_id'::uuid, 3, ARRAY['owns']);
  IF n = 0 THEN RAISE NOTICE 'PASS: predicate filter [owns] from Acme → 0 (Acme has no owns out-edge)'; ELSE RAISE NOTICE 'FAIL: owns-filter nodes=% (want 0)', n; END IF;
END $$;

-- ── (4) 反向点查:谁 works_at Acme?(走 idx_facts_object,非递归)────────────
DO $$
DECLARE n int; who text;
BEGIN
  SELECT string_agg(subject_id::text,',') INTO who
    FROM facts
   WHERE scope = :'scopeA' AND object_entity_id = :'acme_id'::uuid AND predicate='works_at'
     AND valid_to IS NULL AND recorded_to IS NULL;
  SELECT count(*) INTO n FROM facts
   WHERE scope = :'scopeA' AND object_entity_id = :'acme_id'::uuid AND predicate='works_at'
     AND valid_to IS NULL AND recorded_to IS NULL;
  IF n >= 1 THEN RAISE NOTICE 'PASS: reverse lookup works_at Acme → % subject(s)', n; ELSE RAISE NOTICE 'FAIL: reverse lookup=% (want >=1)', n; END IF;
END $$;

-- ── (5) 性能:种子规模虽小,仍计时(真实规模验证见 decision_probe.py 1万facts)──
DO $$
DECLARE t0 timestamptz; n int; ms2 float; ms3 float;
BEGIN
  t0 := clock_timestamp();
  PERFORM * FROM bfs_reachable(:'scopeA', :'acme_id'::uuid, 2); ms2 := extract(epoch FROM (clock_timestamp()-t0))*1000;
  t0 := clock_timestamp();
  PERFORM * FROM bfs_reachable(:'scopeA', :'acme_id'::uuid, 3); ms3 := extract(epoch FROM (clock_timestamp()-t0))*1000;
  RAISE NOTICE 'PERF: 2-hop BFS = % ms ; 3-hop BFS = % ms (seed graph; 10k-facts baseline in decision_probe: 5.47/6.94 ms)', round(ms2::numeric,2), round(ms3::numeric,2);
END $$;
