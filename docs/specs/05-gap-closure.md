# 05 — 缺口闭合与范围扩展

> **状态**:待用户批准。本文档**只新增、不修改** `03-data-model.md` 的 7 个锁定决策与 8 张主表结构。
> **定位**:基于通读 `docs/reference/` 56 篇原文 + 4 份 spec 后的差距分析,把 CortexDB **主要功能**完整复刻所需的 A 档(原 spec 缺失的主要功能)与 B 档(原 spec 提到但不足)补齐到可落地。
> **日期**:2026-06-18
> **依据**:[`../HANDOFF.md`](../HANDOFF.md)、[`01`](01-technical-decisions.md)、[`02`](02-research-notes.md)、[`03`](03-data-model.md)、[`04`](04-stage0-smoke-test.md) + `docs/reference/`(架构白皮书、recall-tuning、24 篇 API/feature/operations 全文)

---

## 0. 为什么有这份文档

`01–04` 已把 CortexDB 的**主线**(五层 / 双时态 / scopes / envelope / 事件溯源 / 知识图谱 / 4 通道检索 / B over C / Postgres-as-queue / 简化授权)设计到可落地,且有实测背书。但通读 CortexDB 原始文档后发现,**"完整复刻主要功能"还差四块主要功能(A 档)和若干被一笔带过的子系统(B 档)**。用户已裁定:**A 档全部纳入实现范围**(Blobs + 批量/导入、Vocabularies、Erasures)。

本文档产出:
1. **§2–3**:新增数据模型(5 张新表 + 2 处列扩展),每个新决策点附 rationale,与 `03 §11` 同等严谨度
2. **§4**:B 档组件设计(StratifiedPack 装配 / forget·erasures 双轨 / `?wait=` / 层直读 + `beliefs/why` LLM / `/answer` 管线 / vocabularies coerce / blobs / importers)
3. **§5**:把 A/B 档纳入的**修订分阶段路线图**(Stage 0→7)
4. **§6**:LLM 调用点全清单 → 映射到 `config llm.*`
5. **§7**:C 档(合理推迟项)的**显式登记**(原 spec 连提都没提,此处补登记)
6. **§8–9**:概念性修正、新开放决策点、运行时风险更新

**阅读对象**:实现者。批准后,本文档与 `03` 共同构成 schema DDL 的设计依据。

---

## 1. 范围裁定(用户 2026-06-18 确认)

### 1.1 纳入实现(完整复刻边界)

| 档 | 功能 | 来源 |
|----|------|------|
| **A** | Blobs(内容寻址二进制) | 本文 §2.1 / §4.7 |
| **A** | 批量写 `experience/bulk` + 5 个导入器 | 本文 §2.3 / §4.8 / §4.9 |
| **A** | Vocabularies(受控词表 coerce) | 本文 §2.2 / §4.7 |
| **A** | Erasures(GDPR 引用计数真删) | 本文 §2.4 / §4.2 |
| **B** | StratifiedPack 响应装配(context_block / provenance / budgets / stream) | §4.1 |
| **B** | Forget 双轨语义(derived_only / redact_events) | §4.2 |
| **B** | `?wait=` 同步写路径 | §4.3 |
| **B** | 层直读端点 + `/beliefs/why` LLM narrative | §4.4 |
| **B** | `/answer` 真实管线(question routing / evidence pack / verifier) | §4.5 |
| **B** | Episodes 因果链 + `POST /episodes/build` | §4.6 |

### 1.2 推迟(显式登记,见 §7)

memory evolution(methylation/consolidation 调度)、temporal-phrases NL 解析、question-type LLM 路由 + HyDE + multihop + salience、admin/metrics(Prometheus)、entity enrichment LLM、PASETO/4 层能力栈/企业安全/集群(原 spec 已 YAGNI)。

### 1.3 不动(锁定)

`03 §11` 的 7 个数据模型决策、`01` 的全部技术选型。本文档的所有新表是**加法**,不改既有 8 张主表的核心结构(仅 §2.5 有 2 处可空列扩展)。

---

## 2. 数据模型扩展(新增表,additive)

> 全部新表 schema 落在 `cortex` schema(与 `cortex_stage0` 探测 schema 分离)。Stage 0 的 DDL 一并建。

### 2.1 `blobs` 表(内容寻址二进制)

**角色**:SHA-256 内容寻址存储,供 `content.kind="blob_ref"` 与 `content.media[].blob_id` 引用。同 bytes → 同 blob。

```sql
CREATE TABLE blobs (
    blob_id        TEXT PRIMARY KEY,             -- 'blob_' || sha256_hex，内容寻址
    sha256         TEXT NOT NULL UNIQUE,         -- 冗余便于查询/校验
    content_type   TEXT NOT NULL,                -- 上传时声明的 MIME，GET 原样回显，不转码
    size_bytes     BIGINT NOT NULL CHECK (size_bytes >= 0),
    storage        TEXT NOT NULL DEFAULT 'inline',  -- 'inline'(BYTEA) | 'file'(external_path)
    data           BYTEA,                        -- storage='inline' 时
    external_path  TEXT,                         -- storage='file' 时（本地 FS / 对象存储 key）
    scope          TEXT NOT NULL,                -- 上传者的 scope（鉴权边界）
    uploader_actor TEXT NOT NULL,
    refcount       BIGINT NOT NULL DEFAULT 0,    -- 被 envelope 引用次数（erasures/redact 用）
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_blobs_scope ON blobs (scope, created_at DESC);
```

**决策点与 rationale**

| # | 决策 | rationale |
|---|------|-----------|
| **B1** | blob_id = `'blob_' \|\| sha256(hex)` | 内容寻址天然去重,与 CortexDB 一致。上传前先算 sha256 → 查 `blobs.sha256` → 命中则直接返回已有 blob_id(免存)。 |
| **B2** | 默认 `storage='inline'`(BYTEA) | 单机、零依赖、事务一致(erasures 删 blob 与删引用同事务)。`storage='file'` 留作大文件升级位(>N MB 走 FS),由配置阈值切换,**不改表结构**。 |
| **B3** | refcount 列 | erasures/redact 时判断"无人引用的 blob 可物理删,有引用的保留"。refcount 由 envelope 写入/删除时维护(envelope 引用 blob → +1)。 |
| **B4** | `Content-Type` 原样回显,不转码 | 与 CortexDB 一致(415 拒 JSON/multipart 上传,Accept 被忽略)。 |

### 2.2 `vocabularies` + `vocabulary_values` 表(受控词表)

**角色**:scope 级受控词表。抽取时把 LLM 吐出的 predicate / object 字符串 coerce 成规范 ID。`kind=closed` 仅允许已登记值(未命中→null);`kind=open` 偏好已登记值但保留其他。

```sql
CREATE TABLE vocabularies (
    vocab_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope       TEXT NOT NULL,                   -- scope 级隔离
    name        TEXT NOT NULL,                   -- 'deal_stage' / 'entity_type' / 'predicate' / 自定义
    kind        TEXT NOT NULL CHECK (kind IN ('closed','open')),
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, name)
);

CREATE TABLE vocabulary_values (
    value_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vocab_id        UUID NOT NULL REFERENCES vocabularies(vocab_id) ON DELETE CASCADE,
    canonical_value TEXT NOT NULL,               -- 规范值 'signed'
    aliases         TEXT[] NOT NULL DEFAULT '{}', -- ['won','closed-won','签约']
    sort_order      INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vocab_id, canonical_value)
);
CREATE INDEX idx_vocab_values_aliases ON vocabulary_values USING gin (aliases);
CREATE INDEX idx_vocab_values_vocab ON vocabulary_values (vocab_id);
```

**决策点与 rationale**

| # | 决策 | rationale |
|---|------|-----------|
| **V1** | 两张表(词表头 + 值) | 值可带多别名(gin 索引),`PUT vocab` 整体替换值集合(与 CortexDB `PUT /vocabularies/{name}` 语义一致)。 |
| **V2** | coerce 在抽取管线里做,不是 DB 触发器 | coerce 需要"先 alias 精确匹配 → closed 未命中归 null / open 保留"的业务逻辑,放应用层(§4.7)。facts 表存 coerce 后的 canonical_value。 |
| **V3** | closed 词表删值 → 已有 facts 的值保留(不回填 null) | 与 CortexDB 一致:`DELETE` 返回 204,facts 保留最后抽取值。只有**新抽取**受词表变更影响。避免大规模回写破坏双时态不可变性。 |
| **V4** | scope 级隔离 | 与 entities/facts 一致,不同 scope 的 "deal_stage" 是不同词表。 |

### 2.3 `import_jobs` 表(批量/导入跟踪)

**角色**:跟踪 `experience/bulk` 与 5 个 importer 的进度。**复用 `jobs` 表的 worker 抢占机制**,但批量/导入需要独立的进度聚合表(jobs 表是单任务粒度)。

```sql
CREATE TABLE import_jobs (
    import_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope         TEXT NOT NULL,
    source        TEXT NOT NULL,                 -- 'bulk'/'mem0'/'zep'/'letta'/'openai'/'jsonl'
    scope_template TEXT,                         -- importer 用,{user} 占位
    status        TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running','completed','failed','cancelled')),
    accepted      INT NOT NULL DEFAULT 0,        -- 解析成功的 envelope 数
    failed        INT NOT NULL DEFAULT 0,        -- 解析失败数
    total         INT NOT NULL DEFAULT 0,
    ordering      TEXT,                          -- 'strict_temporal' / 'batch_throughput'
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ
);
CREATE INDEX idx_import_jobs_status ON import_jobs (status, created_at DESC);
```

**rationale**:批量/导入是"一个 import_id → N 个 experience → N 个 extract job"的扇出。import_jobs 聚合计数,experience 落 events 表,extract 落 jobs 表。lifecycle_events 用 `import_id` 关联(emit `import_progress`/`import_complete`)。

### 2.4 `erasure_jobs` 表(GDPR 引用计数真删)

**角色**:erasures 的 preview → manifest → execute 四阶段。**forget 走另一条路(§4.2,不建新表)**。

```sql
CREATE TABLE erasure_jobs (
    erasure_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope         TEXT NOT NULL,
    selector      JSONB NOT NULL,                -- {about_subject, about_entity, predicate, memory_ids, valid_during, recorded_during}
    phase         TEXT NOT NULL DEFAULT 'enumerate'
                      CHECK (phase IN ('enumerate','refcount','delete','cleanup','completed','failed','cancelled')),
    preview_id    UUID,                          -- preview 阶段产物,24h 过期
    manifest      JSONB,                         -- 逐行计划:delete vs redact,跨 scope refs,需降级的 beliefs
    refcount_breakdown JSONB,                    -- {events_to_delete, events_to_redact, facts_to_demote, beliefs_to_demote}
    progress      JSONB NOT NULL DEFAULT '{}',   -- {deleted, redacted, demoted}
    audit_id      UUID,
    idempotency_key TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    CHECK (idempotency_key IS NULL OR length(idempotency_key) <= 64)
);
CREATE INDEX idx_erasure_phase ON erasure_jobs (phase, created_at DESC);
```

**决策点与 rationale**

| # | 决策 | rationale |
|---|------|-----------|
| **E1** | 单 scope MVP,不做 `cross_workspace` | 个人/小团队无跨工作区。selector 命中的 events:有跨 scope 引用 → redact(清 payload 保 id+wal_offset);无引用 → 物理删。refcount 由 `facts.supports`/`beliefs.supports` 数组含 `event_id = ANY(supports)` 统计。 |
| **E2** | 四阶段 enumerate→refcount→delete→cleanup | 与 CortexDB 一致。preview 产 manifest(24h 过期),execute 校验 manifest 未 stale(否则 409)。各阶段 emit `erasure_progress`,`completed` emit `erasure_complete`。 |
| **E3** | 物理删 event = 删 events 行 + 级联清 facts/beliefs 的 supports 数组 | PG 用 `UPDATE facts SET supports = array_remove(supports, $event_id)`。删完后 refcount=0 的 blob 物理删(§2.1 B3)。 |
| **E4** | erasures 是真删,forget 是软关(§4.2) | 两者职责清晰分离,与 CortexDB `/forget`(非破坏) vs `/erasures`(GDPR)对齐。 |

### 2.5 既有表的列扩展(可空,不破坏锁定决策)

> 这两处是**加可空列**,不改 `03` 锁定的列定义/约束/索引,故不违反"不改 03"。

```sql
-- (a) events: envelope 的 directives 已是 JSONB（03 已有）。补充两列支撑 §4.3 / §4.5
ALTER TABLE events ADD COLUMN embed_status TEXT;        -- 'pending'|'done'|'skipped'，支撑 embed 指令与 wait=indexed
ALTER TABLE events ADD COLUMN access_count INT NOT NULL DEFAULT 0;  -- recall 命中计数，供未来 salience/methylation（C 档，先建列不写逻辑）

-- (b) 通用 pack 缓存（/answer use_pack_id 复用 recall pack，60s TTL）
CREATE TABLE recall_packs (
    pack_id    TEXT PRIMARY KEY,                 -- 'pack_...'
    scope      TEXT NOT NULL,
    query_hash TEXT NOT NULL,                    -- sha256(scope + query + temporal + filters)
    pack_json  JSONB NOT NULL,                   -- 完整 StratifiedPack
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL              -- created_at + 60s
);
CREATE INDEX idx_recall_packs_lookup ON recall_packs (scope, query_hash, expires_at DESC);
```

**rationale**:`embed_status` 让 `?wait=indexed` 有明确的轮询目标;`access_count` 是 C 档 salience/methylation 的数据基础,先建列(零成本)避免将来加列迁移。`recall_packs` 实现 `/answer` 的 `use_pack_id` 复用 + `/recall` 的 60s idempotency_key 缓存(同一 key 60s 内返回同 pack_id)。

---

## 3. 新增决策点汇总(本文裁定)

| 决策 | 一句话 | 详见 |
|------|--------|------|
| **B1–B4** | blobs 内容寻址 + inline 默认 + refcount | §2.1 |
| **V1–V4** | vocabularies 两表 + 应用层 coerce + closed 删值不回填 | §2.2 |
| **E1–E4** | erasures 单 scope + 四阶段 + 物理删 + 与 forget 分离 | §2.4 |
| **F1–F3** | forget 用 `recorded_to` 软关(不硬删派生层) | §4.2 |
| **P1–P4** | StratifiedPack:knapsack 装配 + context_block LLM + provenance 漏斗 + pack 缓存 | §4.1 |
| **W1** | `?wait=` 用 lifecycle 事件 + LISTEN/NOTIFY 阻塞 | §4.3 |

全部 rationale 见对应小节。这些决策**不重新开放** `03 §11` 的 7 项。

---

## 4. 组件设计(B 档 + A 档落地细节)

### 4.1 StratifiedPack 响应装配(P1–P4)

原 `02 §11` 画了 pack 结构,但 Stage 4 计划只写"4 通道"。本节补齐**响应装配**。

**流水**(recall 端点内):
```
query → [embed] → 4 通道并行(全文+向量+图+rerank) → RRF 融合 → 候选带 layer 标签
   → knapsack 装配(按 per_layer_limits 填,总 token ≤ budgets.max_tokens)   [P1]
   → context_block LLM(打包后的 layers + query → 带 [n] 标记的叙述)          [P2]
   → provenance.trail(每步 filter 的 kept 数)+ citations(标记→{layer,id})    [P3]
   → diagnostics(time_ms per stage)
   → 写 recall_packs(60s TTL,idempotency_key 命中直接返回)                  [P4]
```

| # | 决策 | rationale |
|---|------|-----------|
| **P1** | knapsack 按 `per_layer_limits` 硬上限 + `max_tokens` 软目标装填 | 与 CortexDB `budgets` 一致。先填各层硬上限,再按 token 估算裁剪到 `max_tokens`(events 优先裁,因为是逐字引用最贵)。 |
| **P2** | context_block 用 **synthesis LLM**(不是 answer LLM) | context_block 是"叙述性摘要带引用",与 answer 的"回答用户问题"职责不同。复用 `llm.synthesis` 配置(MVP 与 extraction 同模型,见 01)。citation_mode 控制标记形态(`inline_with_markers` 默认 / `block_at_end` / `structured_only` / `none`)。 |
| **P3** | provenance.trail 记录每个 filter 步的 `{step, filter, kept}` | 与 CortexDB `provenance.trail` 一致。步骤:plan / fetch(每通道) / fuse(RRF) / rerank / pack。citations 由 context_block LLM 输出的标记位置反查候选 layer+id。 |
| **P4** | pack 缓存 60s(idempotency_key 或 query_hash) | `/recall` 同 key 60s 返回同 pack_id;`/answer` 的 `use_pack_id` 跳过 recall。表见 §2.5 `recall_packs`。 |
| **P-stream** | `/recall/stream` SSE 逐层 emit | 复用 StratifiedPack 装配,每层 score 完即 emit `event: layer`;最后 `context_block` / `provenance` / `diagnostics` / `done`。**与 lifecycle SSE 是两套**(recall/stream 是同步请求的流式响应,lifecycle 是异步 job 状态广播)。 |

### 4.2 Forget / Erasures 双轨语义(F1–F3)

原 spec 只有 `events.excluded_from_recall` 布尔。补齐 CortexDB 的两套删除。

**Forget(`/v1/forget`,非破坏)**——**关键洞察:用双时态 `recorded_to` 软关,不硬删派生层**。

| cascade | 行为 | 实现动作 |
|---------|------|----------|
| `derived_only`(默认) | 删派生层,events 原样 | 命中 selector 的 facts/beliefs/episodes:`UPDATE ... SET recorded_to = now()`(系统停止相信)。**不删行**,timeline 仍可查历史(符合双时态语义)。 |
| `redact_events` | 派生软关 + event payload 置空 | 上面 + `UPDATE events SET content = '{}'::jsonb, excluded_from_recall = true`(保 event_id + wal_offset,维护 supports/refcount 完整性)。 |

| # | 决策 | rationale |
|---|------|-----------|
| **F1** | forget 派生层 = `recorded_to = now()` 软关 | 这是双时态模型的自然用法:"系统停止相信"=recorded 轴闭合。不破坏不可变性,可逆(清 recorded_to 即恢复),与 timeline 查询一致(recorded_to IS NULL 才是当前)。比硬删优雅且一致。 |
| **F2** | selector 支持 about_subject/about_entity/predicate/memory_ids/valid_during/recorded_during,可组合 | 与 CortexDB `/forget` selector 对齐。`memory_ids` 与其他字段互斥(422 invalid_selector)。空 selector + 非 confirm_all → 422(防误删全 scope)。 |
| **F3** | forget 写 audit_log + emit `forgotten` lifecycle | 审计可追溯。forget 返回 `{deleted:{events,episodes,facts,beliefs,understanding}, audit_id}`(deleted 计数 = 软关的记录数)。 |

**Erasures(`/v1/erasures`,GDPR 真删)**——见 §2.4,四阶段真删,与 forget 路径完全分离。MVP 单 scope,跳 `cross_workspace`/legal_hold。

### 4.3 `?wait=` 同步写路径(W1)

原 `02 §7` 提了 wait 语义的延迟数字,但没落成实现任务。

| wait 值 | 返回时机 | 实现 |
|---------|----------|------|
| (省略) | WAL append | 202,~5ms |
| `captured` | WAL fsync | commit 后 200 |
| `indexed` | embed + fts 入库 | 阻塞到该 event 的 `lifecycle_events` 出现 `kind='indexed'`,或超时 |
| `consolidated` | beliefs 聚合 | 阻塞到 `kind='consolidated'` |

**W1**:用 Postgres `LISTEN/NOTIFY`。worker 完成某 stage 后 `NOTIFY cortex_lifecycle, <lifecycle_id>`,写 `?wait=` 的 handler `LISTEN` 该 channel,收到对应 event_id 的目标 kind 即返回(带 `stages_completed` + `elapsed_ms`)。超时(默认 consolidated=30s)返回 202 + 当前 stages(不报错,降级为 async)。**避免轮询 polling 的空转**。

### 4.4 层直读端点 + `/beliefs/why` LLM

| 端点 | 实现 | LLM? |
|------|------|------|
| `GET /v1/events` | 按 scope/view/since/until/observed_actor/modality/labels 过滤 + cursor 分页 | 否 |
| `GET /v1/episodes[/{id}]` | scope/time/actor 过滤,`with_causal_chain` 控制是否返回 causal_chain | 否 |
| `GET /v1/facts` | subject/predicate/object/as_of/min_confidence 过滤 | 否 |
| `GET /v1/facts/timeline` | **`03 §10.1` 已有 SQL**✓,按 (subject, predicate) 返回超替链全历史 | 否 |
| `GET /v1/beliefs` | about/min_confidence/as_of 过滤 | 否 |
| **`GET /v1/beliefs/why`** | 遍历 `supports[]` 图(belief→facts→events),组装 nodes/edges/weight,再**一次 synthesis LLM 调用渲染 narrative** | **是(synthesis)** |
| `GET /v1/understanding` | MVP:读已合成的 concept(若 Stage 7 做合成则填充) | 否(MVP) |

`beliefs/why` 的图遍历:belief.supports → 取 facts → fact.supports → 取 events → 组成 `{nodes:[{id,type,weight,summary}], edges:[{from,to,relation}]}`,喂 synthesis LLM 生成 narrative(默认模型,响应含 `narrative_model`)。**这是检索/抽取之外的第三个高频 LLM 调用点**,映射到 `llm.synthesis`。

### 4.5 `/v1/answer` 真实管线

原 Stage 5 一句"answer LLM"。补齐:

```
/v1/answer
 ├─ use_pack_id 命中? → 跳过 recall(读 recall_packs)                       [P4]
 ├─ 否 → /v1/recall 同款装配得 pack
 ├─ question routing:规则启发式(有无 as_of / 跨 session 信号)分类             [A-路由, MVP 规则版]
 │     → 影响 TOP_K(单 session 40 / 多 160) + RERANK_POOL(25/40)
 ├─ evidence pack:从 pack.layers 抽 facts+beliefs+events 作为 grounding
 ├─ answer generation LLM(llm.answer):pack + query → 带引用回答              [必]
 ├─ (可选)verifier LLM(llm.verifier,不同 family):对照 citations 查幻觉      [C-可选, 默认关]
 └─ 返回 {answer, citations, model_used, pack_id, diagnostics}
```

**配置扩展**(在 01 的 `llm.*` 基础上加,不改既有):
```yaml
llm:
  answer:   { ... }       # 01 已有
  synthesis: { ... }      # 01 已有(context_block + beliefs/why)
  verifier:                # 新增,可选,默认 disabled
    enabled: false
    model: ...
    api_base: ...
```

MVP:question routing 用规则(不开 LLM 路由),verifier 默认关。两者都是 C 档升级位,但 schema/配置先留好。

### 4.6 Episodes 因果链 + `POST /episodes/build`

原 spec 表有 `causal_chain JSONB` 但没设计构建逻辑。

**segmenter 策略(Stage 3,接口可替换,起步时间窗规则)**:
1. 扫某 scope 内 `recorded_to IS NULL` 且无 episode 归属的 events
2. **时间窗**:相邻 event 的 `observed_at` 间隔 > 阈值(默认 30min)→ 封存当前 episode
3. **preceded_by 链**:读 `context.preceded_by[]`(envelope 显式因果提示),构建 `causal_chain:[{from, to, relation}]`(relation 起步用 'precedes',升级位 LLM 判定)
4. `actors` = 该 episode 内 events 的 observed_actor 去重
5. 封存:`UPDATE episodes SET ended_at=..., sealed=true`

`POST /v1/episodes/build`(需 `scope.write`):手动触发 segmenter 对指定 scope/时间窗跑一次,返回 `{built, items[]}`。

### 4.7 Vocabularies coerce + Blobs 读写

**coerce 流程(抽取管线内,§4 of extraction)**:
```
LLM 抽出 fact {predicate:'签约', object:'已签约'}
 → 查 (scope, name='predicate') 词表:alias '签约' 命中 → canonical 'signed'
 → 查 (scope, name='deal_stage') 词表(object 若属该词表)
 → closed 词表未命中 → object_value=null(并记 coerce_warning)
 → open 词表未命中 → 保留原值
fact 落库用 canonical_value
```
**先 alias 精确匹配(省 LLM)**,词表未命中时按 kind 决定 null/保留。coerce_warning 落 fact 的 extraction 元数据,便于后续人工补词表。

**blobs 读写**:
- `POST /v1/blobs`:读 raw body → 算 sha256 → `SELECT blob_id FROM blobs WHERE sha256=$`(命中去重,免存)→ 未命中则 INSERT(`storage` 按大小阈值 inline/file)→ 返回 `{blob_id, size_bytes, content_type, sha256}`
- `GET /v1/blobs/{id}`:SELECT → 按 storage 取 inline/FS → 原样 Content-Type 回显
- envelope 引用:`content.kind="blob_ref"` 或 `content.kind="message"` 的 `media[].blob_id`;写入时 refcount+=1,forget/erasure 删引用时 refcount-=1(=0 物理删)

### 4.8 批量写 `experience/bulk`

```
POST /v1/experience/bulk  (items ≤ 1000)
 → 建 import_jobs(source='bulk', ordering)
 → 逐 item 校验 envelope → 落 events(wal_offset 单调)+ idempotency 去重
 → 每条 enqueue extract job(jobs 表)
 → 返回 202 {batch_id(import_id), accepted, lifecycle_stream}
 → worker 异步抽取,emit import_progress/import_complete
```
`ordering=strict_temporal`(默认,按 observed_at 排序)/ `batch_throughput`(乱序允许)。

### 4.9 Importers(Mem0/Zep/Letta/OpenAI/JSONL)

每个 importer = 一个映射函数(源格式 → Experience Envelope)+ 复用 bulk 路径:

| importer | 映射 |
|----------|------|
| `jsonl` | 每行 = envelope(减 scope),`scope_template` 用 `{user}` 等占位填 scope |
| `mem0` | `memory→content.text`, `timestamp→observed_at`, `metadata→labels+intent` |
| `zep` | zep facts → `content.kind="triple"`,**保留双时态**(直接落 facts,绕过抽取) |
| `letta` | letta blocks → `modality="document"`, block label → `context.intent` |
| `openai` | openai memory export → envelope |

全部返回 202 + `import_id` + lifecycle stream。进度走 import_jobs + lifecycle `import_progress/complete/error`。

**Zep 的特殊性**:双时态 facts 直接落,不经过抽取 LLM(信任源数据的 4 时间字段)。这是唯一的"跳过抽取直接写派生层"路径,实现时单独处理。

---

## 5. 修订的分阶段路线图(纳入 A/B 档)

| 阶段 | 内容 | 交付物 | 新增(vs 原 HANDOFF) |
|------|------|--------|----------------------|
| **0** | 完整 schema DDL(8 主表 + 2 辅 + **5 新表 + 2 列扩展**) + 假数据 + 冒烟(双时态/图遍历/链接/隔离/队列/**blobs 去重/vocab coerce/erasure refcount**) | `scripts/stage0/00–09`,验证报告 | +blobs/vocab/erasure 表 DDL 与冒烟 |
| **1** | config loader(YAML + 维度强校验)+ db engine + WAL/Events append + queue 基础 + **blobs 读写** + `?wait=captured` | 可 append event、上传/下载 blob | +blobs、+config 维度校验 |
| **2** | worker 框架(抢占 + 重试 + visibility timeout)+ lifecycle SSE(16 kind + LISTEN/NOTIFY)+ **`?wait=indexed/consolidated`** | worker 能跑 job,SSE 能推,wait= 能阻塞 | +?wait= 完整、+SSE 重连 |
| **3** | 抽取(structured output,**先验 R1**) + 实体链接 B over C + **vocabularies coerce** + episodes segmenter(causal_chain)+ beliefs 聚合 + **layer-direct 读(events/facts/timeline/beliefs)** | event→facts→beliefs 闭环 | +vocab coerce、+episodes 因果链、+layer-direct |
| **4** | 4 通道检索 + RRF + rerank + **StratifiedPack 装配(context_block LLM / provenance / budgets)** + `/recall` + `/recall/stream` + **`/beliefs/why` LLM narrative** | recall 端点可用 | +Pack 装配、+recall/stream、+beliefs/why |
| **5** | `/answer`(routing + answer LLM + 可选 verifier + pack 复用)+ **`/forget` 双轨**(recorded_to 软关)+ **`/erasures` 四阶段** | 端到端写/读/答/忘闭环 | +answer 完整、+forget 双轨、+erasures |
| **6** | **`/experience/bulk` + 5 importers + `/export`** | 批量灌数据 + 迁移 | +bulk、+importers、+export(A 档) |
| **7**(推迟) | memory evolution(methylation/consolidation)+ temporal-phrases NL + admin/metrics + HyDE/multihop/salience + question-type LLM 路由 | C 档增强 | 登记项,按需 |

**R1 硬门**(03 §12):Stage 3 起始先用 10 行脚本验 Minimax-M3 对 `json_schema` 的支持,fallback 链已备(json_schema → json_object+prompt → 纯 prompt+修复)。

---

## 6. LLM 调用点全清单 → config 映射

| 调用点 | 阶段 | 配置段 | 用途 | 频率 |
|--------|------|--------|------|------|
| 实体/三元组抽取 | 3 | `llm.extraction` | event → facts 三元组(structured output) | **最高**(每 event) |
| 实体链接灰区判定 | 3 | `llm.extraction`(复用) | B over C 的灰区复用/新建判定 | 中(仅灰区) |
| Beliefs 聚合 | 3 | `llm.synthesis` | facts → probabilistic beliefs | 中 |
| StratifiedPack context_block | 4 | `llm.synthesis` | recall 叙述性摘要带引用 | 高(每 recall) |
| `/beliefs/why` narrative | 4 | `llm.synthesis` | 支持图叙述渲染 | 按需 |
| `/answer` 生成 | 5 | `llm.answer` | 带引用回答 | 高(每 answer) |
| `/answer` verifier | 5 | `llm.verifier`(可选) | 幻觉校验 | 低(默认关) |
| Understanding 合成 | 7 | `llm.synthesis` | concept 合成 | C 档,推迟 |

**MVP 配置**:01 已定 extraction/answer/synthesis 三段(测试期同模型)。**新增 `llm.verifier`(默认 disabled)**。维度强校验(embedding.dimension == vector(1024))在 Stage 1 启动时强制。

---

## 7. C 档:显式登记的推迟项(原 spec 缺登记)

> 原 specs 把这些"不提",将来会变成"我们忘了"。此处**显式登记为已知推迟**,Stage 7 按需。

| 功能 | CortexDB 行为 | 推迟理由 | 升级位 |
|------|--------------|----------|--------|
| memory evolution | methylation(长期不访问降权/剪枝)+ consolidation(同实体多 fact 合并)调度 | 个人工具短期不急;`events.access_count` 列已建(§2.5),数据先攒 | Stage 7 调度 job + 衰减算法 |
| temporal-phrases NL | `temporal.natural="last week"` → 日期区间,可注册自定义短语 | MVP 用显式 `as_of`/`valid_during` | Stage 7 解析器 + `temporal_phrases` 表 |
| question-type 路由(LLM 版) | LLM 分类 query 类型影响检索参数 | MVP 用规则启发式(§4.5) | Stage 7 LLM 路由 |
| HyDE / multihop / salience | 高级检索阶段 | `02 §10` 已说"MVP 先 4 通道";salience 有 `access_count` 数据基础 | Stage 7 |
| admin/metrics | Prometheus 指标、健康检查 | MVP 给 `/healthz` 即可 | Stage 7 |
| entity enrichment LLM | 跨会话实体消歧增强 | B over C 已覆盖单次链接 | Stage 7 |

---

## 8. 概念性裁定(用户 2026-06-18 确认)

1. **三槽身份**:**保留 caller / observed_actor / subject 三槽语义**。`03` 的 events 表已把它们存 TEXT,无需改 schema。实现规则:observed_actor 默认 = caller,subject 默认 = observed_actor;若 envelope 显式传 observed_actor ≠ caller(或 subject ≠ observed_actor),**简化校验——只要 API key 对该 scope 有 `scope.write` 权限即放行**(不引入 `on_behalf_of` / `about_other` 独立能力门,与"静态 key"授权一致)。这样"代写/记录关于他人"的语义保留,授权仍单层。
2. **scope 视图**:**MVP 实现 `structured`**(只返 facts+beliefs,跳 events/episodes),作为 `/answer` 与轻量 recall 的快路径(白皮书 p50 ~80ms vs holistic ~500ms)。`local`/`holistic`/`descend`(`03` 已实现)不变。**`granular`(per-event-shape)推迟**至 Stage 7。

---

## 9. 运行时风险登记(在 03 §12 基础上更新)

| # | 盲区 | 验证阶段 | Fallback |
|---|------|----------|----------|
| R1 | Minimax-M3 对 `json_schema` 支持 | **Stage 3 起始** | json_schema→json_object+prompt→纯 prompt+修复(03 §12,不变) |
| R2 | Prism rerank `threshold=0.1` 分数语义 | Stage 4 | 改排序权重不硬过滤(03 §12,不变) |
| R3 | jina-v5 别名召回质量 | Stage 3 | 调阈值/换模型,schema 不改(03 §12,不变) |
| **R4**(新) | blobs inline BYTEA 在大文件下的性能/膨胀 | Stage 1 | 按大小阈值切 `storage='file'`(表已留 external_path) |
| **R5**(新) | erasures 物理删 + `array_remove(supports)` 在大表上的锁/耗时 | Stage 5 | 分批 + 异步化(phase 内分页处理);必要时降级为 redact |
| **R6**(新) | `?wait=consolidated` 默认 30s 超时是否够(beliefs 聚合慢) | Stage 2 | 超时降级为 async 返回 202(不报错) |

---

## 10. 决策状态

§8 的 Q1(三槽身份)、Q2(structured 视图)**均已于 2026-06-18 用户确认**(见 §8)。本文档无其他待确认项,可进入用户 review → Stage 0。

---

*本文件批准后,与 `03` 共同构成 schema DDL(含新表)的设计依据,进入 Stage 0 完整冒烟。本文不修改 `01` 的选型与 `03 §11` 的 7 项锁定决策。*
