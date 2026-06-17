-- 09_erasure_refcount.sql — erasure 引用计数 + redact vs delete + array_remove(supports) + blob 清理
-- 依据:05 §2.4(erasure_jobs)、§4.2(forget vs erasure 分离)、E1/E3(单 scope;refcount>0→redact,=0→delete;array_remove)

SET search_path = cortex_stage0, public;
\set scopeA 'org:acme/dept:sales/user:alice'
\set evt5   'aaaaaaaa-0000-0000-0000-000000000005'

-- refcount 查询:某 event 被多少派生记录引用(走 supports 数组,反向点查)
CREATE OR REPLACE FUNCTION event_refcount(p_scope text, p_event uuid)
RETURNS int LANGUAGE sql STABLE AS $$
  SELECT (SELECT count(*) FROM facts    WHERE scope=p_scope AND p_event = ANY(supports))
       + (SELECT count(*) FROM beliefs  WHERE scope=p_scope AND p_event = ANY(supports))
       + (SELECT count(*) FROM episodes WHERE scope=p_scope AND p_event = ANY(event_ids))
$$;

-- ── (1) refcount 统计:EVT_5 被 1 个 fact 引用(FACT_Q3_STATUS)──────────────
DO $$
DECLARE rc int;
BEGIN
  rc := event_refcount(:'scopeA', :'evt5'::uuid);
  IF rc = 1 THEN RAISE NOTICE 'PASS: event_refcount(EVT_5) = 1 (referenced by 1 fact)';
  ELSE RAISE NOTICE 'FAIL: refcount=% (want 1)', rc; END IF;
END $$;

-- ── (2) REDACT 路径(refcount>0):清 payload、保 id+wal_offset、excluded_from_recall ──
DO $$
DECLARE cnt_before int; cnt_after int; redacted_content jsonb; excl bool;
BEGIN
  SELECT count(*) INTO cnt_before FROM events WHERE event_id=:'evt5'::uuid;
  -- 模拟 erasure execute 的 delete 阶段:refcount>0 → redact(不删行)
  UPDATE events SET content='{}'::jsonb, excluded_from_recall=true
   WHERE event_id=:'evt5'::uuid AND event_refcount(:'scopeA',:'evt5'::uuid) > 0;
  SELECT count(*) INTO cnt_after FROM events WHERE event_id=:'evt5'::uuid;
  SELECT content INTO redacted_content FROM events WHERE event_id=:'evt5'::uuid;
  SELECT excluded_from_recall INTO excl FROM events WHERE event_id=:'evt5'::uuid;
  IF cnt_before=1 AND cnt_after=1 AND redacted_content='{}'::jsonb AND excl THEN
    RAISE NOTICE 'PASS: refcount>0 → REDACTED (row kept, payload blanked, excluded_from_recall=true)';
  ELSE RAISE NOTICE 'FAIL: redact (before=% after=% content=% excl=%)', cnt_before, cnt_after, redacted_content, excl; END IF;
END $$;

-- ── (3) array_remove:从 FACT_Q3_STATUS.supports 移除已 redact 的 EVT_5 ──────
DO $$
DECLARE still_in int;
BEGIN
  UPDATE facts SET supports = array_remove(supports, :'evt5'::uuid)
   WHERE :'evt5'::uuid = ANY(supports);
  SELECT count(*) INTO still_in FROM facts WHERE '33333333-0000-0000-0000-000000000013'::uuid IN (SELECT unnest(supports) FROM facts WHERE fact_id='33333333-0000-0000-0000-000000000013')
                                            AND :'evt5'::uuid = ANY(supports);
  -- 更直接:确认 FACT_Q3_STATUS 的 supports 不再含 EVT_5
  SELECT count(*) INTO still_in FROM facts WHERE fact_id='33333333-0000-0000-0000-000000000013' AND :'evt5'::uuid = ANY(supports);
  IF still_in = 0 THEN RAISE NOTICE 'PASS: array_remove scrubbed EVT_5 from supporting fact.supports';
  ELSE RAISE NOTICE 'FAIL: EVT_5 still in % facts'' supports', still_in; END IF;
END $$;

-- ── (4) DELETE 路径(refcount=0):物理删行 ──────────────────────────────────
DO $$
DECLARE evt7 uuid; rc int; gone int;
BEGIN
  -- 插一个无引用的孤立 event
  INSERT INTO events (event_id, scope, modality, content, context, caller, observed_actor, observed_at, idempotency_key)
  VALUES ('aaaaaaaa-0000-0000-0000-000000000007', :'scopeA', 'observation',
          '{"kind":"text","text":"orphan"}'::jsonb, '{"observed_at":"2026-06-01T00:00:00Z"}'::jsonb,
          'user:alice','user:alice','2026-06-01 00:00:00+00','erasure-test-7');
  evt7 := 'aaaaaaaa-0000-0000-0000-000000000007'::uuid;
  rc := event_refcount(:'scopeA', evt7);
  IF rc = 0 THEN
    DELETE FROM events WHERE event_id=evt7 AND event_refcount(:'scopeA',evt7)=0;
    SELECT count(*) INTO gone FROM events WHERE event_id=evt7;
    IF gone = 0 THEN RAISE NOTICE 'PASS: refcount=0 → physically DELETED from WAL';
    ELSE RAISE NOTICE 'FAIL: orphan still present'; END IF;
  ELSE RAISE NOTICE 'FAIL: orphan refcount=% (want 0)', rc; END IF;
END $$;

-- ── (5) blob refcount 清理:=0 → 物理删 ────────────────────────────────────
DO $$
DECLARE target text; gone int;
BEGIN
  SELECT blob_id INTO target FROM blobs WHERE content_type='application/pdf' LIMIT 1;  -- contract-pdf
  -- 模拟所有 envelope 引用释放 → refcount 归零
  UPDATE blobs SET refcount=0 WHERE blob_id=target;
  DELETE FROM blobs WHERE blob_id=target AND refcount=0;
  SELECT count(*) INTO gone FROM blobs WHERE blob_id=target;
  IF gone = 0 THEN RAISE NOTICE 'PASS: blob refcount→0 → physically deleted (content-addressed cleanup)';
  ELSE RAISE NOTICE 'FAIL: blob still present'; END IF;
END $$;
