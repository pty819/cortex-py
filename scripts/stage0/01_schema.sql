-- 01_schema.sql — 阶段 0 完整 schema DDL
-- 所有对象落在独立 schema `cortex_stage0`(与 `cortex_probe` 探测 schema 分离,幂等可重建)。
-- DDL 依据:
--   docs/specs/03-data-model.md(8 主表 + 2 辅助 + 全部索引 + 7 锁定决策)
--   docs/specs/05-gap-closure.md(5 张新表:blobs/vocabularies/vocabulary_values/import_jobs/erasure_jobs
--                                + recall_packs + events 列扩展 embed_status/access_count)
--   合成向量 helper v3() 仅供阶段 0 冒烟使用(绕过真实 embedding 服务,验证 vector 类型/HNSW/<=> 机制)。
--
-- PG 18.4 + 扩展:vector(0.8.2)/ ltree / pg_trgm / pgcrypto / unaccent / uuid-ossp 均已安装。
-- gen_random_uuid() PG13+ 内建(本库 18.4),pgcrypto 作冗余保险。

-- ── schema 与 search_path ────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS cortex_stage0;
SET search_path = cortex_stage0, public;

-- 扩展(与 00_extensions.sql 一致,IF NOT EXISTS 幂等)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- 1. events — WAL / Events 层(03 §1 + 05 §2.5 列扩展)
-- ============================================================================
CREATE TABLE events (
    event_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wal_offset          BIGSERIAL UNIQUE NOT NULL,

    scope               TEXT NOT NULL,

    modality            TEXT NOT NULL,            -- conversation/document/tool_result/observation/feedback/imported
    content             JSONB NOT NULL,           -- 判别联合体 {kind, role, text, ...}
    context             JSONB NOT NULL,           -- {observed_at, labels, intent, preceded_by, ...}

    -- 三槽身份(05 §8:保留三槽语义;observed_actor 默认=caller,subject 默认=observed_actor)
    caller              TEXT NOT NULL,
    observed_actor      TEXT NOT NULL,
    subject             TEXT,

    observed_at         TIMESTAMPTZ NOT NULL,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    directives          JSONB,                    -- {extract[], consolidate_into, confidence_floor, embed}
    idempotency_key     TEXT NOT NULL,

    excluded_from_recall BOOLEAN NOT NULL DEFAULT false,  -- forget/redact 后排除

    -- 05 §2.5 列扩展(可空/默认,不破坏 03 锁定结构)
    embed_status        TEXT,                     -- 'pending'|'done'|'skipped'(?wait=indexed 轮询目标)
    access_count        INT NOT NULL DEFAULT 0,   -- recall 命中计数(C 档 salience/methylation 数据基础)

    UNIQUE (scope, idempotency_key)
);

CREATE INDEX idx_events_scope_observed ON events (scope, observed_at DESC);
CREATE INDEX idx_events_observed_at    ON events (observed_at DESC);
CREATE INDEX idx_events_wal_offset     ON events (wal_offset);
-- 全文检索(BM25 通道;content->>'text' 抽文本)
CREATE INDEX idx_events_content_fts    ON events USING gin (to_tsvector('english', content->>'text'));

-- ============================================================================
-- 2. entities — B over C 载体(03 §2)
-- ============================================================================
CREATE TABLE entities (
    entity_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope              TEXT NOT NULL,

    canonical_name     TEXT NOT NULL,
    entity_type        TEXT,                      -- 可空(发现型)
    description        TEXT,                      -- B 层判定 prompt 用

    embedding          vector(1024),             -- ⚠️ 维度须 == YAML embedding.dimension(jina-v5=1024)

    merged_into        UUID REFERENCES entities(entity_id),  -- 非空=已合并
    merge_confidence   FLOAT,

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_entities_embedding   ON entities USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_entities_scope_name  ON entities (scope, canonical_name) WHERE merged_into IS NULL;
CREATE INDEX idx_entities_scope_type  ON entities (scope, entity_type)   WHERE merged_into IS NULL;

-- ============================================================================
-- 3. entity_aliases(03 §3)
-- ============================================================================
CREATE TABLE entity_aliases (
    alias_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     UUID NOT NULL REFERENCES entities(entity_id),
    alias         TEXT NOT NULL,
    alias_type    TEXT,
    scope         TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, alias)
);
CREATE INDEX idx_aliases_scope_alias ON entity_aliases (scope, alias);
CREATE INDEX idx_aliases_entity      ON entity_aliases (entity_id);

-- ============================================================================
-- 4. facts — 图谱核心:双时态三元组 + 图边(03 §4)
-- ============================================================================
CREATE TABLE facts (
    fact_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope            TEXT NOT NULL,

    subject_id       UUID NOT NULL REFERENCES entities(entity_id),
    predicate        TEXT NOT NULL,

    -- object 双模式(03 决策 2)
    object_type      TEXT NOT NULL,                                  -- 'entity' | 'literal'
    object_entity_id UUID REFERENCES entities(entity_id),
    object_value     JSONB,                                          -- {datatype, value}

    -- 双时态 4 字段(03 决策 4:valid_to 闭合 + 查询过滤,不物化 timeline)
    valid_from       TIMESTAMPTZ NOT NULL,
    valid_to         TIMESTAMPTZ,                                    -- NULL=开放(当前为真)
    recorded_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to      TIMESTAMPTZ,                                    -- NULL=当前(系统仍信)

    confidence       FLOAT NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),

    supports         UUID[] NOT NULL DEFAULT '{}',                   -- [event_id, ...]

    extracted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    extraction_model TEXT,

    CHECK (object_type = 'entity' AND object_entity_id IS NOT NULL
        OR object_type = 'literal' AND object_value IS NOT NULL)
);
-- 图遍历核心索引(递归 CTE 自连接;部分索引只索引"当前为真")
CREATE INDEX idx_facts_subject         ON facts (scope, subject_id, predicate)       WHERE valid_to IS NULL AND recorded_to IS NULL;
CREATE INDEX idx_facts_object          ON facts (scope, object_entity_id, predicate) WHERE valid_to IS NULL AND recorded_to IS NULL;
CREATE INDEX idx_facts_subj_pred_valid ON facts (scope, subject_id, predicate, valid_from) WHERE recorded_to IS NULL;
CREATE INDEX idx_facts_scope_conf      ON facts (scope, confidence DESC)             WHERE valid_to IS NULL AND recorded_to IS NULL;

-- ============================================================================
-- 5. beliefs — 概率断言 + supports 链(03 §5)
-- ============================================================================
CREATE TABLE beliefs (
    belief_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope          TEXT NOT NULL,

    about_entity_id UUID NOT NULL REFERENCES entities(entity_id),
    stance         TEXT NOT NULL CHECK (stance IN ('supports','likely_true','uncertain','likely_false','contradicts')),
    claim          TEXT NOT NULL,
    confidence     FLOAT NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    confidence_interval JSONB,                                       -- [low, high]

    supports       UUID[] NOT NULL DEFAULT '{}',                    -- [fact_id / episode_id, ...]

    valid_from     TIMESTAMPTZ NOT NULL,
    valid_to       TIMESTAMPTZ,
    recorded_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to    TIMESTAMPTZ,

    last_revised_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (confidence_interval IS NULL OR jsonb_typeof(confidence_interval) = 'array')
);
CREATE INDEX idx_beliefs_scope_about ON beliefs (scope, about_entity_id) WHERE valid_to IS NULL AND recorded_to IS NULL;
CREATE INDEX idx_beliefs_scope_conf  ON beliefs (scope, confidence DESC) WHERE valid_to IS NULL AND recorded_to IS NULL;

-- ============================================================================
-- 6. episodes — 有界事件序列(03 §6;segmenter 逻辑阶段 3)
-- ============================================================================
CREATE TABLE episodes (
    episode_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope           TEXT NOT NULL,
    title           TEXT,
    event_ids       UUID[] NOT NULL DEFAULT '{}',
    actors          TEXT[] NOT NULL DEFAULT '{}',
    causal_chain    JSONB,                                           -- [{from, to, relation}, ...]

    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,                                     -- NULL=未封存
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,
    recorded_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to     TIMESTAMPTZ,

    sealed          BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX idx_episodes_scope_time ON episodes (scope, started_at DESC) WHERE recorded_to IS NULL;
CREATE INDEX idx_episodes_actors     ON episodes USING gin (actors);

-- ============================================================================
-- 7. jobs — Postgres-as-queue(03 §7 + 决策 7)
-- ============================================================================
CREATE TABLE jobs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        TEXT NOT NULL,                  -- extract/segment/consolidate/synthesize/embed/import/erasure
    scope           TEXT NOT NULL,

    event_id        UUID REFERENCES events(event_id),
    batch_id        UUID,

    status          TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','completed','failed','cancelled')),
    attempts        INT NOT NULL DEFAULT 0,
    max_attempts    INT NOT NULL DEFAULT 3,

    priority        INT NOT NULL DEFAULT 0,
    run_after       TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by       TEXT,
    locked_at       TIMESTAMPTZ,

    payload         JSONB,
    result          JSONB,
    error           TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);
CREATE INDEX idx_jobs_queue ON jobs (priority DESC, run_after, created_at) WHERE status = 'queued';
CREATE INDEX idx_jobs_event ON jobs (event_id) WHERE status IN ('queued','running');

-- ============================================================================
-- 8. scopes — scope 注册表(03 §8,可选/auto-provision)
-- ============================================================================
CREATE TABLE scopes (
    scope_path        TEXT PRIMARY KEY,
    parent_path       TEXT REFERENCES scopes(scope_path),
    members           JSONB NOT NULL DEFAULT '[]',
    policies          JSONB NOT NULL DEFAULT '{}',
    auto_provisioned  BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_scopes_parent ON scopes (parent_path);
CREATE INDEX idx_scopes_auto   ON scopes (auto_provisioned) WHERE auto_provisioned = true;

-- ============================================================================
-- 9. lifecycle_events — SSE 源(03 §9)
-- ============================================================================
CREATE TABLE lifecycle_events (
    lifecycle_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            TEXT NOT NULL,                  -- captured/extracted/indexed/consolidated/compressed/forgotten/lagging/...
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    scope           TEXT NOT NULL,
    event_id        UUID REFERENCES events(event_id),
    batch_id        UUID,
    job_id          UUID REFERENCES jobs(job_id),
    payload         JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_lifecycle_scope_ts ON lifecycle_events (scope, ts DESC);
CREATE INDEX idx_lifecycle_event    ON lifecycle_events (event_id);

-- ============================================================================
-- 10. audit_log — 简化版审计(03 表清单"可选";append-only)
-- ============================================================================
CREATE TABLE audit_log (
    audit_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor           TEXT NOT NULL,
    scope           TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    action          TEXT NOT NULL,                  -- write/forget/erasure/...
    decision        TEXT NOT NULL DEFAULT 'allow',  -- allow/deny
    target          TEXT,                           -- event_id / fact_id / ...
    note            TEXT,
    prev_hash       TEXT,                           -- 链式(SHA-256);MVP 可空
    row_hash        TEXT
);
CREATE INDEX idx_audit_scope_ts ON audit_log (scope, ts DESC);

-- ============================================================================
-- 11. blobs — 内容寻址二进制(05 §2.1)
-- ============================================================================
CREATE TABLE blobs (
    blob_id        TEXT PRIMARY KEY,                -- 'blob_' || sha256(hex),内容寻址
    sha256         TEXT NOT NULL UNIQUE,
    content_type   TEXT NOT NULL,
    size_bytes     BIGINT NOT NULL CHECK (size_bytes >= 0),
    storage        TEXT NOT NULL DEFAULT 'inline',  -- 'inline' | 'file'
    data           BYTEA,
    external_path  TEXT,
    scope          TEXT NOT NULL,
    uploader_actor TEXT NOT NULL,
    refcount       BIGINT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_blobs_scope ON blobs (scope, created_at DESC);

-- ============================================================================
-- 12. vocabularies + vocabulary_values(05 §2.2)
-- ============================================================================
CREATE TABLE vocabularies (
    vocab_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope       TEXT NOT NULL,
    name        TEXT NOT NULL,                       -- 'deal_stage' / 'predicate' / 'entity_type' / 自定义
    kind        TEXT NOT NULL CHECK (kind IN ('closed','open')),
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, name)
);
CREATE TABLE vocabulary_values (
    value_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vocab_id        UUID NOT NULL REFERENCES vocabularies(vocab_id) ON DELETE CASCADE,
    canonical_value TEXT NOT NULL,
    aliases         TEXT[] NOT NULL DEFAULT '{}',
    sort_order      INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vocab_id, canonical_value)
);
CREATE INDEX idx_vocab_values_aliases ON vocabulary_values USING gin (aliases);
CREATE INDEX idx_vocab_values_vocab   ON vocabulary_values (vocab_id);

-- ============================================================================
-- 13. import_jobs — 批量/导入跟踪(05 §2.3)
-- ============================================================================
CREATE TABLE import_jobs (
    import_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope          TEXT NOT NULL,
    source         TEXT NOT NULL,                    -- bulk/mem0/zep/letta/openai/jsonl
    scope_template TEXT,
    status         TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running','completed','failed','cancelled')),
    accepted       INT NOT NULL DEFAULT 0,
    failed         INT NOT NULL DEFAULT 0,
    total          INT NOT NULL DEFAULT 0,
    ordering       TEXT,
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ
);
CREATE INDEX idx_import_jobs_status ON import_jobs (status, created_at DESC);

-- ============================================================================
-- 14. erasure_jobs — GDPR 引用计数真删(05 §2.4)
-- ============================================================================
CREATE TABLE erasure_jobs (
    erasure_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope         TEXT NOT NULL,
    selector      JSONB NOT NULL,
    phase         TEXT NOT NULL DEFAULT 'enumerate'
                      CHECK (phase IN ('enumerate','refcount','delete','cleanup','completed','failed','cancelled')),
    preview_id    UUID,
    manifest      JSONB,                             -- 逐行计划 delete vs redact
    refcount_breakdown JSONB,
    progress      JSONB NOT NULL DEFAULT '{}',
    audit_id      UUID,
    idempotency_key TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    CHECK (idempotency_key IS NULL OR length(idempotency_key) <= 64)
);
CREATE INDEX idx_erasure_phase ON erasure_jobs (phase, created_at DESC);

-- ============================================================================
-- 15. recall_packs — pack 缓存(/answer use_pack_id + /recall 60s 幂等)(05 §2.5)
-- ============================================================================
CREATE TABLE recall_packs (
    pack_id    TEXT PRIMARY KEY,
    scope      TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    pack_json  JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_recall_packs_lookup ON recall_packs (scope, query_hash, expires_at DESC);

-- ============================================================================
-- 合成向量 helper(仅供阶段 0 冒烟;真实 embedding 由 worker 调服务生成)
--   v3(x,y,z) → 1024 维向量,前 3 维 = x,y,z,其余 0。
--   用前 3 维即可精确控制 cosine,让 top-K 召回与三阈值分支可断言,绕过真实 embedding(那是 R3,阶段 3 验)。
-- ============================================================================
CREATE OR REPLACE FUNCTION v3(x float, y float, z float)
RETURNS vector LANGUAGE sql IMMUTABLE AS $$
    SELECT (
        '[' || x || ',' || y || ',' || z || ',' ||
        (SELECT string_agg('0', ',') FROM generate_series(1,1021)) ||
        ']'
    )::vector(1024)
$$;

-- 实体 resolve 视图(03 决策 5:merged_into 软引用;查询带 resolve)
CREATE OR REPLACE VIEW entities_resolved AS
    SELECT entity_id,
           COALESCE(merged_into, entity_id) AS resolved_id,
           scope, canonical_name, entity_type, description, embedding, merged_into
    FROM entities;

-- RAISE NOTICE 只能在 PL/pgSQL 内,故用 DO 块。
DO $$ BEGIN
  RAISE NOTICE 'SCHEMA: 15 tables + 1 view + v3() helper created in cortex_stage0';
END $$;

-- ── Episodes case 元数据扩展(问题7:诊断 case 结构)──────────────────────────
ALTER TABLE cortex_stage0.episodes ADD COLUMN IF NOT EXISTS case_id TEXT;
ALTER TABLE cortex_stage0.episodes ADD COLUMN IF NOT EXISTS equipment TEXT;
ALTER TABLE cortex_stage0.episodes ADD COLUMN IF NOT EXISTS lot TEXT;
ALTER TABLE cortex_stage0.episodes ADD COLUMN IF NOT EXISTS recipe TEXT;
ALTER TABLE cortex_stage0.episodes ADD COLUMN IF NOT EXISTS phase TEXT;
ALTER TABLE cortex_stage0.episodes ADD COLUMN IF NOT EXISTS root_cause TEXT;
ALTER TABLE cortex_stage0.episodes ADD COLUMN IF NOT EXISTS resolution TEXT;
ALTER TABLE cortex_stage0.episodes ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'open';
ALTER TABLE cortex_stage0.episodes ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}';

ALTER TABLE cortex_stage0.events ADD COLUMN IF NOT EXISTS case_id TEXT;

CREATE INDEX IF NOT EXISTS idx_episodes_case_id ON cortex_stage0.episodes (scope, case_id) WHERE recorded_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_episodes_status ON cortex_stage0.episodes (scope, status) WHERE recorded_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_events_case_id ON cortex_stage0.events (scope, case_id) WHERE excluded_from_recall = false;
