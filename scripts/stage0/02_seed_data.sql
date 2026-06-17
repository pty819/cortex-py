-- 02_seed_data.sql — 阶段 0 假数据
-- 依据:docs/specs/04-stage0-smoke-test.md §3 + 05-gap-closure.md(新增 blobs/vocab fixtures)
-- 所有对象在 cortex_stage0 schema。固定 UUID,供 03-09 测试脚本引用。
--
-- 测试用例埋点:
--   (a) 同名不同人/scope 隔离:scope A 的 Acme(客户) vs scope B 的 Acme(内部服务)
--   (b) 别名链:Robert Smith / Bob / [email protected] 归一到 ent_robert_smith
--   (c) 超替链:Acme deal_stage poc(01-01)→close(04-10)→signed(05-13),valid_to 依次闭合
--   (d) 图链:Acme --employs--> Priya --owns--> Q3-Renewal --has_status--> "negotiating"
--   (e) 实体链接三阈值:robert_smith(近,merge)/robert_jones(中,grayzone)/bob_sales(远,new)
--   (f) blobs:两段不同 bytes→不同 blob_id;同 bytes→去重同 id
--   (g) vocabularies:deal_stage closed 词表(signed/close/poc + 中文别名"签约")

SET search_path = cortex_stage0, public;

-- ── scope 常量 ───────────────────────────────────────────────────────────
-- scope A:销售场景,Acme 是客户
-- scope B:工程场景,Acme 是内部服务代号
\set scopeA 'org:acme/dept:sales/user:alice'
\set scopeB 'org:acme/dept:eng/user:bob'

-- ── events(支撑 supports 链;固定 UUID)──────────────────────────────────
-- 用 uuid_generate_v4() 不行(随机),用显式 UUID 保证可引用。
INSERT INTO events (event_id, scope, modality, content, context, caller, observed_actor, subject, observed_at, idempotency_key) VALUES
('aaaaaaaa-0000-0000-0000-000000000001', :'scopeA', 'conversation',
 '{"kind":"message","role":"user","text":"Acme deal moved to poc stage"}'::jsonb,
 '{"observed_at":"2026-01-01T10:00:00Z","labels":["acme"],"intent":"deal_status"}'::jsonb,
 'user:alice','user:alice','user:alice','2026-01-01 10:00:00+00','seed-evt-1'),
('aaaaaaaa-0000-0000-0000-000000000002', :'scopeA', 'conversation',
 '{"kind":"message","role":"user","text":"Acme deal advanced to close"}'::jsonb,
 '{"observed_at":"2026-04-10T10:00:00Z","labels":["acme"],"intent":"deal_status"}'::jsonb,
 'user:alice','user:alice','user:alice','2026-04-10 10:00:00+00','seed-evt-2'),
('aaaaaaaa-0000-0000-0000-000000000003', :'scopeA', 'conversation',
 '{"kind":"message","role":"user","text":"Acme deal signed!"}'::jsonb,
 '{"observed_at":"2026-05-13T10:00:00Z","labels":["acme"],"intent":"deal_status"}'::jsonb,
 'user:alice','user:alice','user:alice','2026-05-13 10:00:00+00','seed-evt-3'),
('aaaaaaaa-0000-0000-0000-000000000004', :'scopeA', 'conversation',
 '{"kind":"message","role":"user","text":"Priya owns the Q3 renewal at Acme"}'::jsonb,
 '{"observed_at":"2026-03-15T10:00:00Z","labels":["acme","priya"],"intent":"ownership"}'::jsonb,
 'user:alice','user:alice','user:alice','2026-03-15 10:00:00+00','seed-evt-4'),
('aaaaaaaa-0000-0000-0000-000000000005', :'scopeA', 'observation',
 '{"kind":"text","text":"Q3-Renewal is in negotiating status"}'::jsonb,
 '{"observed_at":"2026-03-20T10:00:00Z","labels":["q3"],"intent":"status"}'::jsonb,
 'user:alice','user:alice','user:alice','2026-03-20 10:00:00+00','seed-evt-5'),
('aaaaaaaa-0000-0000-0000-000000000006', :'scopeA', 'conversation',
 '{"kind":"message","role":"user","text":"Acme renewed ARR $480k"}'::jsonb,
 '{"observed_at":"2026-05-13T11:00:00Z","labels":["acme"],"intent":"finance"}'::jsonb,
 'user:alice','user:alice','user:alice','2026-05-13 11:00:00+00','seed-evt-6');

-- ── entities ─────────────────────────────────────────────────────────────
-- scope A
-- 注:三个"真实"实体的 embedding 取 x≈0(与 B-over-C 测试查询轴 v3(1,·,·) 正交),
--     避免它们在 05_entity_resolution 召回里误入 merge 分支。三个测试实体(robert_smith/jones/bob_sales)用前 3 维精确控制 cosine。
INSERT INTO entities (entity_id, scope, canonical_name, entity_type, description, embedding) VALUES
('11111111-0000-0000-0000-000000000001', :'scopeA', 'Acme Corp', 'org', 'Acme Corporation, customer', v3(0.0,0.7,0.7)),
('11111111-0000-0000-0000-000000000002', :'scopeA', 'Priya Rao', 'person', 'Account owner at Acme', v3(0.0,0.5,0.8)),
('11111111-0000-0000-0000-000000000003', :'scopeA', 'Q3 Renewal', 'project', 'Q3 renewal motion', v3(0.0,0.3,0.9)),
-- 实体链接测试实体(前 3 维精确控制 cosine;查询 q=v3(1,0.1,0.02))
('11111111-0000-0000-0000-000000000010', :'scopeA', 'Robert Smith', 'person', 'VP Sales, Robert Smith', v3(1,0,0)),      -- cosine~0.995 近(merge)
('11111111-0000-0000-0000-000000000011', :'scopeA', 'Robert Jones', 'person', 'Another Robert', v3(1,1,0)),               -- cosine~0.774 中(grayzone)
('11111111-0000-0000-0000-000000000012', :'scopeA', 'Bob Sales', 'person', 'Bob in sales', v3(0,1,0));                     -- cosine~0.099 远(new)
-- scope B:Acme 是内部服务(同名不同实体)
INSERT INTO entities (entity_id, scope, canonical_name, entity_type, description, embedding) VALUES
('22222222-0000-0000-0000-000000000001', :'scopeB', 'Acme', 'service', 'Internal Acme service', v3(0.9,0.9,0.9)),
('22222222-0000-0000-0000-000000000002', :'scopeB', 'Bob Eng', 'person', 'Engineer Bob', v3(0.05,0.05,0.05));

-- ── entity_aliases(别名链:bob/Robert/[email protected] → ent_robert_smith)────────
INSERT INTO entity_aliases (entity_id, alias, alias_type, scope) VALUES
('11111111-0000-0000-0000-000000000010', 'Bob',          'nickname',  :'scopeA'),
('11111111-0000-0000-0000-000000000010', '[email protected]', 'email',     :'scopeA'),
('11111111-0000-0000-0000-000000000010', 'B. Smith',     'abbreviation', :'scopeA'),
('11111111-0000-0000-0000-000000000002', 'Priya',        'nickname',  :'scopeA');

-- ── facts — 超替链(deal_stage poc→close→signed,valid_to 依次闭合)─────────
-- subject=acme_sales, predicate=deal_stage;3 版本,模拟 reconcile 后状态
INSERT INTO facts (fact_id, scope, subject_id, predicate, object_type, object_value, valid_from, valid_to, confidence, supports, extraction_model) VALUES
('33333333-0000-0000-0000-000000000001', :'scopeA', '11111111-0000-0000-0000-000000000001', 'deal_stage', 'literal',
 '{"datatype":"string","value":"poc"}'::jsonb,    '2026-01-01 00:00:00+00', '2026-04-10 00:00:00+00', 0.6,
 ARRAY['aaaaaaaa-0000-0000-0000-000000000001']::uuid[], 'smoke-seed'),
('33333333-0000-0000-0000-000000000002', :'scopeA', '11111111-0000-0000-0000-000000000001', 'deal_stage', 'literal',
 '{"datatype":"string","value":"close"}'::jsonb,  '2026-04-10 00:00:00+00', '2026-05-13 00:00:00+00', 0.75,
 ARRAY['aaaaaaaa-0000-0000-0000-000000000002']::uuid[], 'smoke-seed'),
('33333333-0000-0000-0000-000000000003', :'scopeA', '11111111-0000-0000-0000-000000000001', 'deal_stage', 'literal',
 '{"datatype":"string","value":"signed"}'::jsonb, '2026-05-13 00:00:00+00', NULL, 0.92,
 ARRAY['aaaaaaaa-0000-0000-0000-000000000003']::uuid[], 'smoke-seed');

-- ── facts — 图链(out-edge 友好,供递归 CTE BFS)──────────────────────────
INSERT INTO facts (fact_id, scope, subject_id, predicate, object_type, object_entity_id, valid_from, valid_to, confidence, supports, extraction_model) VALUES
-- Acme --employs--> Priya
('33333333-0000-0000-0000-000000000010', :'scopeA', '11111111-0000-0000-0000-000000000001', 'employs', 'entity',
 '11111111-0000-0000-0000-000000000002', '2026-01-01 00:00:00+00', NULL, 0.9, ARRAY[]::uuid[], 'smoke-seed'),
-- Priya --owns--> Q3
('33333333-0000-0000-0000-000000000011', :'scopeA', '11111111-0000-0000-0000-000000000002', 'owns', 'entity',
 '11111111-0000-0000-0000-000000000003', '2026-03-15 00:00:00+00', NULL, 0.88,
 ARRAY['aaaaaaaa-0000-0000-0000-000000000004']::uuid[], 'smoke-seed'),
-- Priya --works_at--> Acme(入边,测 idx_facts_object 反向点查)
('33333333-0000-0000-0000-000000000012', :'scopeA', '11111111-0000-0000-0000-000000000002', 'works_at', 'entity',
 '11111111-0000-0000-0000-000000000001', '2026-01-01 00:00:00+00', NULL, 0.85, ARRAY[]::uuid[], 'smoke-seed');

INSERT INTO facts (fact_id, scope, subject_id, predicate, object_type, object_value, valid_from, valid_to, confidence, supports, extraction_model) VALUES
-- Q3 --has_status--> "negotiating"(literal,图走到此为止)
('33333333-0000-0000-0000-000000000013', :'scopeA', '11111111-0000-0000-0000-000000000003', 'has_status', 'literal',
 '{"datatype":"string","value":"negotiating"}'::jsonb, '2026-03-20 00:00:00+00', NULL, 0.8,
 ARRAY['aaaaaaaa-0000-0000-0000-000000000005']::uuid[], 'smoke-seed'),
-- Acme --renewed_arr--> "$480k"(literal,财据)
('33333333-0000-0000-0000-000000000014', :'scopeA', '11111111-0000-0000-0000-000000000001', 'renewed_arr', 'literal',
 '{"datatype":"string","value":"$480k"}'::jsonb, '2026-05-13 00:00:00+00', NULL, 0.9,
 ARRAY['aaaaaaaa-0000-0000-0000-000000000006']::uuid[], 'smoke-seed');

-- scope B 的 Acme(internal service)有独立 fact,验证跨 scope 隔离
INSERT INTO facts (fact_id, scope, subject_id, predicate, object_type, object_value, valid_from, valid_to, confidence, extraction_model) VALUES
('33333333-0000-0000-0000-000000000020', :'scopeB', '22222222-0000-0000-0000-000000000001', 'deal_stage', 'literal',
 '{"datatype":"string","value":"internal"}'::jsonb, '2026-01-01 00:00:00+00', NULL, 0.5, 'smoke-seed');

-- ── beliefs(about Acme,概率断言 + supports 链)────────────────────────────
INSERT INTO beliefs (belief_id, scope, about_entity_id, stance, claim, confidence, confidence_interval, supports, valid_from) VALUES
('44444444-0000-0000-0000-000000000001', :'scopeA', '11111111-0000-0000-0000-000000000001',
 'likely_true', 'Acme is likely to renew again', 0.81, '[0.6,0.92]'::jsonb,
 ARRAY['33333333-0000-0000-0000-000000000003','33333333-0000-0000-0000-000000000014','33333333-0000-0000-0000-000000000013']::uuid[],
 '2026-05-13 00:00:00+00');

-- ── blobs(内容寻址去重;同 bytes → 同 blob_id)────────────────────────────
-- 用 digest() 算 sha256(pgcrypto),blob_id = 'blob_' || hex
INSERT INTO blobs (blob_id, sha256, content_type, size_bytes, storage, data, scope, uploader_actor, refcount) VALUES
('blob_' || encode(digest('hello-screenshot-png-bytes','sha256'),'hex'),
 encode(digest('hello-screenshot-png-bytes','sha256'),'hex'), 'image/png', octet_length('hello-screenshot-png-bytes'::bytea),
 'inline', 'hello-screenshot-png-bytes'::bytea, :'scopeA', 'user:alice', 1),
('blob_' || encode(digest('contract-pdf-bytes','sha256'),'hex'),
 encode(digest('contract-pdf-bytes','sha256'),'hex'), 'application/pdf', octet_length('contract-pdf-bytes'::bytea),
 'inline', 'contract-pdf-bytes'::bytea, :'scopeA', 'user:alice', 1);

-- ── vocabularies(deal_stage closed 词表,含中文别名"签约"→signed)─────────
INSERT INTO vocabularies (vocab_id, scope, name, kind, description) VALUES
('55555555-0000-0000-0000-000000000001', :'scopeA', 'deal_stage', 'closed', 'Canonical deal stages') RETURNING vocab_id;

INSERT INTO vocabulary_values (vocab_id, canonical_value, aliases, sort_order) VALUES
('55555555-0000-0000-0000-000000000001', 'poc',    ARRAY['pitch','discovery'], 1),
('55555555-0000-0000-0000-000000000001', 'close',  ARRAY['closing','verbal'],  2),
('55555555-0000-0000-0000-000000000001', 'signed', ARRAY['won','closed-won','签约','已签约'], 3);

-- ── scopes(显式注册两个测试 scope)────────────────────────────────────────
INSERT INTO scopes (scope_path, parent_path, members, policies, auto_provisioned) VALUES
('org:acme', NULL, '[{"actor":"user:alice","role":"owner"}]'::jsonb, '{"retention":"365d"}'::jsonb, true),
('org:acme/dept:sales', 'org:acme', '[]'::jsonb, '{}'::jsonb, true),
('org:acme/dept:sales/user:alice', 'org:acme/dept:sales', '[{"actor":"user:alice","role":"owner"}]'::jsonb, '{"default_view":"holistic"}'::jsonb, true),
('org:acme/dept:eng', 'org:acme', '[]'::jsonb, '{}'::jsonb, true),
('org:acme/dept:eng/user:bob', 'org:acme/dept:eng', '[{"actor":"user:bob","role":"owner"}]'::jsonb, '{}'::jsonb, true);

DO $$ BEGIN
  RAISE NOTICE 'SEED: events=% entities=% facts=% beliefs=% blobs=% vocab_values=%',
    (SELECT count(*) FROM events), (SELECT count(*) FROM entities), (SELECT count(*) FROM facts),
    (SELECT count(*) FROM beliefs), (SELECT count(*) FROM blobs), (SELECT count(*) FROM vocabulary_values);
END $$;
