-- 08_blobs_vocab.sql — blobs SHA-256 去重 + vocabularies coerce(closed/open)
-- 依据:05 §2.1(blobs 内容寻址)、§2.2 + §4.7(vocabularies coerce:别名→canonical,closed→null,open→保留)

SET search_path = cortex_stage0, public;
\set scopeA 'org:acme/dept:sales/user:alice'

-- ── (1) blobs:同 bytes → 同 blob_id(内容寻址去重)──────────────────────────
DO $$
DECLARE before_cnt int; after_cnt int; dup_id text; sha text;
BEGIN
  SELECT count(*) INTO before_cnt FROM blobs;
  sha := encode(digest('hello-screenshot-png-bytes','sha256'),'hex');  -- 与 seed 相同 bytes
  -- 上传前去重查:命中既有 blob_id,免 INSERT
  SELECT blob_id INTO dup_id FROM blobs WHERE sha256 = sha;
  IF dup_id IS NULL THEN
    INSERT INTO blobs (blob_id, sha256, content_type, size_bytes, storage, data, scope, uploader_actor, refcount)
    VALUES ('blob_'||sha, sha, 'image/png', 25, 'inline', 'hello-screenshot-png-bytes'::bytea, :'scopeA', 'user:alice', 1);
  END IF;
  SELECT count(*) INTO after_cnt FROM blobs;
  IF after_cnt = before_cnt THEN RAISE NOTICE 'PASS: identical bytes dedup → blob_id % (count unchanged %)', dup_id, after_cnt;
  ELSE RAISE NOTICE 'FAIL: dedup failed (before=% after=%)', before_cnt, after_cnt; END IF;
END $$;

-- ── (2) blobs:不同 bytes → 新 blob_id ──────────────────────────────────────
DO $$
DECLARE before_cnt int; after_cnt int; newsha text; got text;
BEGIN
  SELECT count(*) INTO before_cnt FROM blobs;
  newsha := encode(digest('a-different-file','sha256'),'hex');
  INSERT INTO blobs (blob_id, sha256, content_type, size_bytes, storage, data, scope, uploader_actor, refcount)
  VALUES ('blob_'||newsha, newsha, 'image/png', 17, 'inline', 'a-different-file'::bytea, :'scopeA', 'user:alice', 1);
  SELECT count(*) INTO after_cnt FROM blobs;
  SELECT size_bytes::text INTO got FROM blobs WHERE sha256 = newsha;
  IF after_cnt = before_cnt + 1 AND got = '17' THEN RAISE NOTICE 'PASS: distinct bytes → new blob (count %→%)', before_cnt, after_cnt;
  ELSE RAISE NOTICE 'FAIL: new blob (before=% after=% size=%)', before_cnt, after_cnt, got; END IF;
END $$;

-- ── (3) coerce 函数(05 §4.7:别名精确→canonical;closed 未命中→null;open→保留)──
CREATE OR REPLACE FUNCTION vocab_coerce(p_scope text, p_vocab_name text, p_raw text)
RETURNS text LANGUAGE sql STABLE AS $$
  SELECT CASE
    WHEN NOT EXISTS (SELECT 1 FROM vocabularies WHERE scope=p_scope AND name=p_vocab_name) THEN p_raw
    ELSE COALESCE(
      (SELECT vv.canonical_value
         FROM vocabularies v JOIN vocabulary_values vv ON vv.vocab_id=v.vocab_id
        WHERE v.scope=p_scope AND v.name=p_vocab_name
          AND (vv.canonical_value = p_raw OR p_raw = ANY(vv.aliases))
        LIMIT 1),
      CASE WHEN (SELECT kind FROM vocabularies WHERE scope=p_scope AND name=p_vocab_name)='open' THEN p_raw ELSE NULL END
    )
  END
$$;

-- 补一个 open 词表测 open 分支
INSERT INTO vocabularies (vocab_id, scope, name, kind, description) VALUES
('55555555-0000-0000-0000-000000000099', :'scopeA', 'entity_type', 'open', 'Open entity types') ON CONFLICT DO NOTHING;
INSERT INTO vocabulary_values (vocab_id, canonical_value, aliases) VALUES
('55555555-0000-0000-0000-000000000099', 'person', ARRAY['people','individual']),
('55555555-0000-0000-0000-000000000099', 'org', ARRAY['organization','company']) ON CONFLICT DO NOTHING;

DO $$
DECLARE r1 text; r2 text; r3 text; r4 text; r5 text; r6 text;
BEGIN
  r1 := vocab_coerce(:'scopeA','deal_stage','签约');       -- 中文别名 → signed
  r2 := vocab_coerce(:'scopeA','deal_stage','pitch');      -- alias → poc
  r3 := vocab_coerce(:'scopeA','deal_stage','expired');    -- closed 未命中 → NULL
  r4 := vocab_coerce(:'scopeA','deal_stage','won');        -- alias → signed
  r5 := vocab_coerce(:'scopeA','entity_type','feature');   -- open 未命中 → 保留 feature
  r6 := vocab_coerce(:'scopeA','entity_type','company');   -- open alias → org
  IF r1='signed' AND r2='poc' AND r3 IS NULL AND r4='signed' THEN
    RAISE NOTICE 'PASS: CLOSED vocab coerce (签约→signed, pitch→poc, expired→NULL, won→signed)';
  ELSE RAISE NOTICE 'FAIL: closed coerce r1=% r2=% r3=% r4=%', r1,r2,r3,r4; END IF;
  IF r5='feature' AND r6='org' THEN
    RAISE NOTICE 'PASS: OPEN vocab coerce (unknown→kept feature, company→org)';
  ELSE RAISE NOTICE 'FAIL: open coerce r5=% r6=%', r5,r6; END IF;
END $$;
