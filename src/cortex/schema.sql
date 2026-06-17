-- cortex schema — 实现用(依据 03-data-model.md + 05-gap-closure.md;stage0 已验证)
-- schema 名为 `cortex`(stage0 探测用 cortex_stage0)。幂等:CREATE ... IF NOT EXISTS。
-- 扩展由部署侧/00_extensions 装好:vector/ltree/pg_trgm/unaccent/pgcrypto/uuid-ossp。

CREATE SCHEMA IF NOT EXISTS cortex;
SET search_path = cortex, public;

-- ── events: WAL / Events 层(03 §1 + 05 §2.5)──────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    event_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wal_offset          BIGSERIAL UNIQUE NOT NULL,
    scope               TEXT NOT NULL,
    modality            TEXT NOT NULL,
    content             JSONB NOT NULL,
    context             JSONB NOT NULL,
    caller              TEXT NOT NULL,
    observed_actor      TEXT NOT NULL,
    subject             TEXT,
    observed_at         TIMESTAMPTZ NOT NULL,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    directives          JSONB,
    idempotency_key     TEXT NOT NULL,
    excluded_from_recall BOOLEAN NOT NULL DEFAULT false,
    embed_status        TEXT,
    access_count        INT NOT NULL DEFAULT 0,
    UNIQUE (scope, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_events_scope_observed ON cortex.events (scope, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_observed_at    ON cortex.events (observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_wal_offset     ON cortex.events (wal_offset);
CREATE INDEX IF NOT EXISTS idx_events_content_fts    ON cortex.events USING gin (to_tsvector('english', content->>'text'));

-- ── entities: B over C 载体(03 §2)────────────────────────────────────────
CREATE TABLE IF NOT EXISTS entities (
    entity_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope              TEXT NOT NULL,
    canonical_name     TEXT NOT NULL,
    entity_type        TEXT,
    description        TEXT,
    embedding          vector(1024),
    merged_into        UUID REFERENCES cortex.entities(entity_id),
    merge_confidence   FLOAT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_entities_embedding   ON cortex.entities USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_entities_scope_name  ON cortex.entities (scope, canonical_name) WHERE merged_into IS NULL;
CREATE INDEX IF NOT EXISTS idx_entities_scope_type  ON cortex.entities (scope, entity_type)   WHERE merged_into IS NULL;

-- ── entity_aliases(03 §3)─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     UUID NOT NULL REFERENCES cortex.entities(entity_id),
    alias         TEXT NOT NULL,
    alias_type    TEXT,
    scope         TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, alias)
);
CREATE INDEX IF NOT EXISTS idx_aliases_scope_alias ON cortex.entity_aliases (scope, alias);
CREATE INDEX IF NOT EXISTS idx_aliases_entity      ON cortex.entity_aliases (entity_id);

-- ── facts: 图谱核心(03 §4)─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS facts (
    fact_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope            TEXT NOT NULL,
    subject_id       UUID NOT NULL REFERENCES cortex.entities(entity_id),
    predicate        TEXT NOT NULL,
    object_type      TEXT NOT NULL,
    object_entity_id UUID REFERENCES cortex.entities(entity_id),
    object_value     JSONB,
    valid_from       TIMESTAMPTZ NOT NULL,
    valid_to         TIMESTAMPTZ,
    recorded_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to      TIMESTAMPTZ,
    confidence       FLOAT NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    supports         UUID[] NOT NULL DEFAULT '{}',
    extracted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    extraction_model TEXT,
    CHECK (object_type = 'entity' AND object_entity_id IS NOT NULL
        OR object_type = 'literal' AND object_value IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_facts_subject         ON cortex.facts (scope, subject_id, predicate)       WHERE valid_to IS NULL AND recorded_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_facts_object          ON cortex.facts (scope, object_entity_id, predicate) WHERE valid_to IS NULL AND recorded_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_facts_subj_pred_valid ON cortex.facts (scope, subject_id, predicate, valid_from) WHERE recorded_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_facts_scope_conf      ON cortex.facts (scope, confidence DESC)             WHERE valid_to IS NULL AND recorded_to IS NULL;
-- facts 内容全文索引(供 facts 通道 BM25)
CREATE INDEX IF NOT EXISTS idx_facts_text_fts ON cortex.facts USING gin (
    to_tsvector('english', coalesce(predicate,'') || ' ' || coalesce(object_value->>'value',''))
);

-- ── beliefs(03 §5)─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS beliefs (
    belief_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope          TEXT NOT NULL,
    about_entity_id UUID NOT NULL REFERENCES cortex.entities(entity_id),
    stance         TEXT NOT NULL CHECK (stance IN ('supports','likely_true','uncertain','likely_false','contradicts')),
    claim          TEXT NOT NULL,
    confidence     FLOAT NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    confidence_interval JSONB,
    supports       UUID[] NOT NULL DEFAULT '{}',
    valid_from     TIMESTAMPTZ NOT NULL,
    valid_to       TIMESTAMPTZ,
    recorded_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to    TIMESTAMPTZ,
    last_revised_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (confidence_interval IS NULL OR jsonb_typeof(confidence_interval) = 'array')
);
CREATE INDEX IF NOT EXISTS idx_beliefs_scope_about ON cortex.beliefs (scope, about_entity_id) WHERE valid_to IS NULL AND recorded_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_beliefs_scope_conf  ON cortex.beliefs (scope, confidence DESC) WHERE valid_to IS NULL AND recorded_to IS NULL;

-- ── episodes(03 §6)────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS episodes (
    episode_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope           TEXT NOT NULL,
    title           TEXT,
    event_ids       UUID[] NOT NULL DEFAULT '{}',
    actors          TEXT[] NOT NULL DEFAULT '{}',
    causal_chain    JSONB,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,
    recorded_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to     TIMESTAMPTZ,
    sealed          BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_episodes_scope_time ON cortex.episodes (scope, started_at DESC) WHERE recorded_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_episodes_actors     ON cortex.episodes USING gin (actors);

-- ── jobs: Postgres-as-queue(03 §7)─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        TEXT NOT NULL,
    scope           TEXT NOT NULL,
    event_id        UUID REFERENCES cortex.events(event_id),
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
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON cortex.jobs (priority DESC, run_after, created_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_jobs_event ON cortex.jobs (event_id) WHERE status IN ('queued','running');

-- ── scopes / lifecycle_events / audit_log(03 §8/§9 + 简化审计)─────────────
CREATE TABLE IF NOT EXISTS scopes (
    scope_path        TEXT PRIMARY KEY,
    parent_path       TEXT REFERENCES cortex.scopes(scope_path),
    members           JSONB NOT NULL DEFAULT '[]',
    policies          JSONB NOT NULL DEFAULT '{}',
    auto_provisioned  BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scopes_parent ON cortex.scopes (parent_path);

CREATE TABLE IF NOT EXISTS lifecycle_events (
    lifecycle_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    scope           TEXT NOT NULL,
    event_id        UUID REFERENCES cortex.events(event_id),
    batch_id        UUID,
    job_id          UUID REFERENCES cortex.jobs(job_id),
    payload         JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_scope_ts ON cortex.lifecycle_events (scope, ts DESC);
CREATE INDEX IF NOT EXISTS idx_lifecycle_event    ON cortex.lifecycle_events (event_id);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor           TEXT NOT NULL,
    scope           TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    action          TEXT NOT NULL,
    decision        TEXT NOT NULL DEFAULT 'allow',
    target          TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_scope_ts ON cortex.audit_log (scope, ts DESC);

-- ── 05 新表:blobs / vocabularies / vocabulary_values / import_jobs / erasure_jobs / recall_packs ──
CREATE TABLE IF NOT EXISTS blobs (
    blob_id        TEXT PRIMARY KEY,
    sha256         TEXT NOT NULL UNIQUE,
    content_type   TEXT NOT NULL,
    size_bytes     BIGINT NOT NULL CHECK (size_bytes >= 0),
    storage        TEXT NOT NULL DEFAULT 'inline',
    data           BYTEA,
    external_path  TEXT,
    scope          TEXT NOT NULL,
    uploader_actor TEXT NOT NULL,
    refcount       BIGINT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_blobs_scope ON cortex.blobs (scope, created_at DESC);

CREATE TABLE IF NOT EXISTS vocabularies (
    vocab_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope       TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('closed','open')),
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, name)
);
CREATE TABLE IF NOT EXISTS vocabulary_values (
    value_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vocab_id        UUID NOT NULL REFERENCES cortex.vocabularies(vocab_id) ON DELETE CASCADE,
    canonical_value TEXT NOT NULL,
    aliases         TEXT[] NOT NULL DEFAULT '{}',
    sort_order      INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vocab_id, canonical_value)
);
CREATE INDEX IF NOT EXISTS idx_vocab_values_aliases ON cortex.vocabulary_values USING gin (aliases);
CREATE INDEX IF NOT EXISTS idx_vocab_values_vocab   ON cortex.vocabulary_values (vocab_id);

CREATE TABLE IF NOT EXISTS import_jobs (
    import_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope          TEXT NOT NULL,
    source         TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_import_jobs_status ON cortex.import_jobs (status, created_at DESC);

CREATE TABLE IF NOT EXISTS erasure_jobs (
    erasure_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope         TEXT NOT NULL,
    selector      JSONB NOT NULL,
    phase         TEXT NOT NULL DEFAULT 'enumerate'
                      CHECK (phase IN ('enumerate','refcount','delete','cleanup','completed','failed','cancelled')),
    preview_id    UUID,
    manifest      JSONB,
    refcount_breakdown JSONB,
    progress      JSONB NOT NULL DEFAULT '{}',
    audit_id      UUID,
    idempotency_key TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    CHECK (idempotency_key IS NULL OR length(idempotency_key) <= 64)
);
CREATE INDEX IF NOT EXISTS idx_erasure_phase ON cortex.erasure_jobs (phase, created_at DESC);

CREATE TABLE IF NOT EXISTS recall_packs (
    pack_id    TEXT PRIMARY KEY,
    scope      TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    pack_json  JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recall_packs_lookup ON cortex.recall_packs (scope, query_hash, expires_at DESC);

-- ── Stage 7 扩展 ───────────────────────────────────────────────────────────
-- events 软剪枝标记(methylation:长期不召回 → excluded_from_recall=true,methylated_at 记时)
ALTER TABLE cortex.events ADD COLUMN IF NOT EXISTS methylated_at TIMESTAMPTZ;

-- temporal_phrases:NL 时间短语注册(05 §C 档 + 07 §5)
CREATE TABLE IF NOT EXISTS temporal_phrases (
    phrase_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL UNIQUE,           -- 全局唯一(小写匹配)
    anchor       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expression   TEXT NOT NULL,                  -- 两 ISO8601 duration 以 '..' 隔,如 -P7D..P0D
    is_default   BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- synonyms:同义词扩展(检索 synonym 通道;predicate/value 同义)
CREATE TABLE IF NOT EXISTS synonyms (
    synonym_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope       TEXT NOT NULL,
    term        TEXT NOT NULL,         -- 规范词,如 own
    aliases     TEXT[] NOT NULL DEFAULT '{}',  -- 同义 possess/has/owns
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, term)
);
CREATE INDEX IF NOT EXISTS idx_synonyms_scope_term ON cortex.synonyms (scope, term);
CREATE INDEX IF NOT EXISTS idx_synonyms_aliases ON cortex.synonyms USING gin (aliases);

-- concepts: Understanding 层(LLM 概念合成,per topic;related 图)
CREATE TABLE IF NOT EXISTS concepts (
    concept_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope        TEXT NOT NULL,
    name         TEXT NOT NULL,
    topic        TEXT,
    version      INT NOT NULL DEFAULT 1,
    summary      TEXT,
    supports     UUID[] NOT NULL DEFAULT '{}',   -- [fact_id / belief_id / episode_id]
    related      JSONB NOT NULL DEFAULT '[]',    -- [{concept_id, relation}]
    confidence   FLOAT NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    valid_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_concepts_scope_topic ON cortex.concepts (scope, topic);

-- resolve 视图(03 决策 5:merged_into 软引用)
CREATE OR REPLACE VIEW cortex.entities_resolved AS
    SELECT entity_id, COALESCE(merged_into, entity_id) AS resolved_id,
           scope, canonical_name, entity_type, description, embedding, merged_into
    FROM cortex.entities;
