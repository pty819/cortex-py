# 03 — 数据模型设计(阶段 0 核心)

> **状态**:待用户批准。本文档是阶段 0 DDL 的设计依据。
> **批准前不写任何实现代码**(包括 DDL)。
> **参考**:[`02-research-notes.md`](02-research-notes.md) 第 4/5/8 节(双时态/scopes/图谱)

---

## 设计哲学

1. **Facts 表是图谱的核心**——它同时承担"双时态三元组存储"和"图遍历的边表"两个角色,schema 必须同时服务两者
2. **Entity 表是实体链接的载体**——B over C 策略要求它支持向量召回 + 别名 + 合并/分裂
3. **scope 过滤是 SQL 层强制**——图谱隔离的底线,所有查询和 CTE 都带 scope 条件
4. **所有派生记录可从 Events 重建**——WAL 是唯一真相源,派生层 schema 要支持"丢掉重跑"
5. **Postgres 原生类型优先**——JSONB / ARRAY / tsvector / pgvector,减少 join,提升图遍历性能

---

## 表清单(8 张主表 + 2 张辅助)

| 表 | 角色 | 阶段 |
|----|------|------|
| `events` | WAL,Events 层,唯一真相源 | 阶段 0 |
| `entities` | 实体表,B over C 载体 | 阶段 0 |
| `entity_aliases` | 别名表(规范化辅助) | 阶段 0 |
| `facts` | **双时态三元组 + 图边**(核心) | 阶段 0 |
| `beliefs` | 概率断言 + supports 链 | 阶段 0 |
| `episodes` | 有界事件序列 | 阶段 0(轻量,segmenter 后做) |
| `jobs` | Postgres-as-queue 任务表 | 阶段 0 |
| `scopes` | scope 注册表(可选,auto-provision) | 阶段 0 |
| `audit_log` | 审计日志(简化版) | 阶段 0(可选) |
| `lifecycle_events` | SSE 事件源(job 状态变化) | 阶段 0 |

---

## 1. `events` 表(WAL / Events 层)

### 角色
唯一写入端点。不可变 append-only。所有派生层从此重建。

### Schema

```sql
CREATE TABLE events (
    -- 标识
    event_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wal_offset        BIGSERIAL UNIQUE NOT NULL,  -- 单调递增,WAL 位点

    -- 寻址
    scope             TEXT NOT NULL,              -- 'org:acme/dept:eng/user:alice'

    -- 内容(Experience Envelope)
    modality          TEXT NOT NULL,              -- conversation/document/tool_result/observation/feedback/imported
    content           JSONB NOT NULL,             -- {kind, role, text, ...} 判别联合体
    context           JSONB NOT NULL,             -- {observed_at, labels, intent, preceded_by, ...}

    -- 身份槽
    caller            TEXT NOT NULL,              -- 隐式来自 API key 关联的 actor
    observed_actor    TEXT NOT NULL,              -- 谁执行(默认 = caller)
    subject           TEXT,                       -- 关于谁(默认 = observed_actor)

    -- 时间(Events 只有 2 字段,不超替)
    observed_at       TIMESTAMPTZ NOT NULL,       -- 事件在世界中发生
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 系统获知

    -- 指令
    directives        JSONB,                      -- {extract, consolidate_into, confidence_floor, embed}

    -- 幂等
    idempotency_key   TEXT NOT NULL,

    -- 生命周期
    excluded_from_recall BOOLEAN NOT NULL DEFAULT false,  -- forget/cancel 后排除

    -- 修订约束
    UNIQUE (scope, idempotency_key)
);

-- 核心索引
CREATE INDEX idx_events_scope_observed ON events (scope, observed_at DESC);
CREATE INDEX idx_events_observed_at ON events (observed_at DESC);
CREATE INDEX idx_events_wal_offset ON events (wal_offset);
-- 全文检索(用于 BM25 通道)
CREATE INDEX idx_events_content_fts ON events USING gin (to_tsvector('english', content->>'text'));
```

### 决策点与 rationale

**1a. content 用 JSONB 还是拆字段?**
→ **JSONB**。Experience Envelope 是判别联合体(message/text/json/blob_ref/triple),字段随 kind 变。JSONB 灵活,且 `content->>'text'` 可直接建 tsvector。拆字段会导致 schema 随 kind 膨胀。

**1b. WAL offset 用 Postgres `BIGSERIAL`?**
→ **是**。CortexDB 原版用 append-only 文件 + checksum chain,本项目简化为 Postgres 的 `BIGSERIAL` + 事务保证。单调递增,job 表用它做"从哪个 offset 开始处理"的游标。

**1c. scope 用 TEXT 还是拆段表?**
→ **TEXT**。scope 路径 `org:acme/dept:eng/user:alice` 作为整串存储。holistic/descend 遍历用 `LIKE` 前缀(holistic:查所有前缀;descend:查所有后缀)。拆段表会增加 join 成本,且 scope 段数 ≤8,LIKE 足够。
- **备选**:Postgres `ltree` 扩展(原生层级路径类型,有专门索引)。阶段 0 冒烟测试对比 LIKE vs ltree 性能。

**1d. excluded_from_recall 字段?**
→ **是**。forget/cancel 后,原始 event 不删(保留 refcount 完整性),但标记排除。recall 和图遍历都 `WHERE excluded_from_recall = false`。

**1e. 全文检索用 tsvector?**
→ **是**。`to_tsvector('english', content->>'text')` + GIN 索引,替代 CortexDB 的 Tantivy BM25。单库,零依赖。中文等非英语需配对应 language 或用 `simple`。

---

## 2. `entities` 表(B over C 载体)

### 角色
实体规范化 + 向量召回 + 合并/分裂。

### Schema

```sql
CREATE EXTENSION IF NOT EXISTS vector;  -- pgvector

CREATE TABLE entities (
    -- 标识
    entity_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- 寻址
    scope              TEXT NOT NULL,              -- 实体作用域(图谱隔离)

    -- 规范化
    canonical_name     TEXT NOT NULL,              -- 规范名('Robert Smith')
    entity_type        TEXT,                       -- person/org/service/project/...(可空,允许发现型)
    description        TEXT,                       -- LLM 生成的一句话描述(用于 B 层判定 prompt)

    -- 向量(B over C 的 C 层)
    embedding          vector(1536),              -- pgvector,由 canonical_name+description 计算

    -- 合并/分裂
    merged_into        UUID REFERENCES entities(entity_id),  -- 非空 = 已合并到目标
    merge_confidence   FLOAT,                      -- 合并时的置信度

    -- 时间
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 约束
    -- (scope, canonical_name) 唯一,但合并后旧记录的 canonical_name 保留
    -- 查询时永远 WHERE merged_into IS NULL
);

-- 向量召回索引(HNSW)
CREATE INDEX idx_entities_embedding ON entities USING hnsw (embedding vector_cosine_ops);
-- 规范名查找
CREATE INDEX idx_entities_scope_name ON entities (scope, canonical_name) WHERE merged_into IS NULL;
-- 类型过滤
CREATE INDEX idx_entities_scope_type ON entities (scope, entity_type) WHERE merged_into IS NULL;
```

### 决策点与 rationale

**2a. embedding 维度 1536?**
→ **是**(配合 `text-embedding-3-small`)。YAML 可配,改模型时同步改。这与 `vector_dimensions` 必须一致(见 [`01-technical-decisions.md`](01-technical-decisions.md) 配置章节的"维度陷阱"警告)。

**2b. 合并用 `merged_into` 软引用?**
→ **是**。合并时:`entities.merged_into = target_id`,不删行。所有查询带 `WHERE merged_into IS NULL` 过滤活实体。facts 表的 subject_id/object_id 不改——查询时用一个 resolve 函数(CASE WHEN merged_into IS NOT NULL THEN merged_into ELSE entity_id)解析到规范实体。
- **备选**:合并时重写所有 facts 的 subject_id/object_id。代价是 facts 表大批量更新(破坏双时态不可变性),拒绝。

**2c. entity_type 允许 NULL?**
→ **是**。CortexDB 支持"发现型"实体类型(从数据模式涌现)。type 可空,抽取时可填可不填。

**2d. description 字段?**
→ **是**。B over C 的 LLM 判定需要"候选实体的简短描述"做语义判断。抽取时 LLM 顺带生成,合并时取保留方。

**2e. scope 字段?**
→ **必填**。实体 scope 绑定(图谱隔离)。同一 "Acme" 在不同 scope 是不同实体。这与 facts.scope 一致。

---

## 3. `entity_aliases` 表

### 角色
辅助字符串归一(A 策略降级位)+ 别名管理。

### Schema

```sql
CREATE TABLE entity_aliases (
    alias_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     UUID NOT NULL REFERENCES entities(entity_id),
    alias         TEXT NOT NULL,               -- 'Bob' / 'robert@acme.com' / 'B. Smith'
    alias_type    TEXT,                        -- nickname/email/abbreviation/full_name/...
    scope         TEXT NOT NULL,               -- 与 entity 同 scope
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, alias)
);

CREATE INDEX idx_aliases_scope_alias ON entity_aliases (scope, alias);
CREATE INDEX idx_aliases_entity ON entity_aliases (entity_id);
```

### rationale
B over C 的第一道过滤:新实体提到 "Bob" → 先在 aliases 表精确查 → 命中则直接拿 entity_id(省向量召回)→ 未命中再走向量召回 + LLM 判定。这是纯字符串归一的降级位,也用于手工维护的别名。

---

## 4. `facts` 表(**图谱核心**:双时态三元组 + 图边)

### 角色
双时态三元组存储。**同时是图遍历的边表**(递归 CTE 在此自连接)。

### Schema

```sql
CREATE TABLE facts (
    -- 标识
    fact_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- 寻址
    scope            TEXT NOT NULL,              -- 图谱隔离

    -- 三元组(subject/predicate/object)
    -- subject 和 object 都是 entity_id(规范化)
    subject_id       UUID NOT NULL REFERENCES entities(entity_id),
    predicate        TEXT NOT NULL,              -- 'works_at' / 'upgraded' / 'deal_stage' / ...
    -- object 可以是 entity 引用,也可以是字面值
    object_type      TEXT NOT NULL,              -- 'entity' | 'literal'
    object_entity_id UUID REFERENCES entities(entity_id),  -- object_type='entity' 时
    object_value     JSONB,                      -- object_type='literal' 时 {datatype, value}

    -- 双时态 4 字段
    valid_from       TIMESTAMPTZ NOT NULL,
    valid_to         TIMESTAMPTZ,                -- NULL = 开放(当前为真)
    recorded_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to      TIMESTAMPTZ,                -- NULL = 当前(系统仍相信)

    -- 置信度
    confidence       FLOAT NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),

    -- 证据链(指回 Events)
    supports         UUID[] NOT NULL DEFAULT '{}',  -- [event_id, ...]

    -- 抽取元数据
    extracted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    extraction_model TEXT,                       -- 哪个抽取模型产出

    -- 约束
    CHECK (object_type = 'entity' AND object_entity_id IS NOT NULL
        OR object_type = 'literal' AND object_value IS NOT NULL)
);

-- 图遍历核心索引(递归 CTE 在此自连接)
CREATE INDEX idx_facts_subject ON facts (scope, subject_id, predicate) WHERE valid_to IS NULL AND recorded_to IS NULL;
CREATE INDEX idx_facts_object ON facts (scope, object_entity_id, predicate) WHERE valid_to IS NULL AND recorded_to IS NULL;
-- 时间线查询
CREATE INDEX idx_facts_subj_pred_valid ON facts (scope, subject_id, predicate, valid_from) WHERE recorded_to IS NULL;
-- 置信度过滤
CREATE INDEX idx_facts_scope_conf ON facts (scope, confidence DESC) WHERE valid_to IS NULL AND recorded_to IS NULL;
```

### 决策点与 rationale

**4a. object 用 entity_id 还是统一存字面值?**
→ **双模式**(`object_type` 判别)。事实的 object 既可能是实体引用("Bob works_at AcmeCorp"——object 是 entity),也可能是字面值("Acme deal_stage = 'signed'"——object 是 string)。CortexDB 原版也是双模式。用 JSONB 存字面值(`{datatype:'string', value:'signed'}`)。

**4b. supports 用 `UUID[]` 数组还是关联表?**
→ **`UUID[]` 数组**。理由:
- supports 是 fact 的固有属性(谁产生的),不需要对 supports 本身做查询/更新
- 数组减少 join,facts 表已是大表,少一个 join 提升图遍历性能
- 缺点:无法高效"反向查哪些 fact 引用了某 event"——但这个查询不常见,需要时建一个物化视图

**4c. 超替链怎么实现?**
→ **`valid_to` 闭合 + 查询过滤**,不物化 timeline 表。新证据到达时:
1. 找到当前活 fact(`WHERE subject_id=? AND predicate=? AND valid_to IS NULL AND recorded_to IS NULL`)
2. 把它的 `valid_to` 设为新 fact 的 `valid_from`
3. 插入新 fact
- timeline 查询:`SELECT * FROM facts WHERE subject_id=? AND predicate=? ORDER BY valid_from`(返回所有历史版本)
- **不物化 timeline 表**:增加写入复杂度,且 timeline 查询用现有索引足够快

**4d. 图遍历 CTE 在此表自连接?**
→ **是**。`WITH RECURSIVE` 在 `facts` 表上做 BFS:
```sql
-- 伪代码:从 seed 实体出发,沿 predicate 边走 max_hops 跳
WITH RECURSIVE graph_walk AS (
    -- 起点:seed 实体的出边
    SELECT subject_id, object_entity_id AS current_node, predicate, 1 AS hop
    FROM facts
    WHERE subject_id = $seed AND scope = $scope
      AND valid_to IS NULL AND recorded_to IS NULL
      AND (cardinality($predicates) = 0 OR predicate = ANY($predicates))
    UNION ALL
    -- 递归:沿 current_node 的出边继续
    SELECT f.subject_id, f.object_entity_id, f.predicate, gw.hop + 1
    FROM facts f
    JOIN graph_walk gw ON f.subject_id = gw.current_node
    WHERE f.scope = $scope
      AND f.valid_to IS NULL AND recorded_to IS NULL
      AND gw.hop < $max_hops
      AND (cardinality($predicates) = 0 OR f.predicate = ANY($predicates))
)
SELECT DISTINCT ON (current_node) current_node, hop, predicate
FROM graph_walk ORDER BY current_node, hop;
```
**CTE 内部强制 scope 过滤和双时态过滤**(WHERE valid_to IS NULL)——这是图谱隔离和时间推理的底线。

**4e. object_entity_id 上的索引?**
→ **必建**。图遍历既走出边(subject→object),也走入边(object→subject)。两个方向都要索引。

**4f. 部分索引 `WHERE valid_to IS NULL AND recorded_to IS NULL`?**
→ **是**。绝大多数查询只关心"当前为真"的 fact。部分索引让活 fact 的查询走小索引,大幅提速。

---

## 5. `beliefs` 表

### 角色
概率断言 + supports 链。`GET /beliefs/why` 遍历支持图。

### Schema

```sql
CREATE TABLE beliefs (
    belief_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope          TEXT NOT NULL,

    -- 断言(关于某实体的概率主张)
    about_entity_id UUID NOT NULL REFERENCES entities(entity_id),
    stance         TEXT NOT NULL CHECK (stance IN ('supports','likely_true','uncertain','likely_false','contradicts')),
    claim          TEXT NOT NULL,              -- 自然语言主张
    confidence     FLOAT NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    confidence_interval JSONB,                  -- [low, high]

    -- 证据链(指回 Facts 和 Episodes)
    supports       UUID[] NOT NULL DEFAULT '{}',  -- [fact_id/episode_id, ...]

    -- 双时态
    valid_from     TIMESTAMPTZ NOT NULL,
    valid_to       TIMESTAMPTZ,
    recorded_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to    TIMESTAMPTZ,

    last_revised_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (confidence_interval IS NULL OR jsonb_typeof(confidence_interval) = 'array')
);

CREATE INDEX idx_beliefs_scope_about ON beliefs (scope, about_entity_id) WHERE valid_to IS NULL AND recorded_to IS NULL;
CREATE INDEX idx_beliefs_scope_conf ON beliefs (scope, confidence DESC) WHERE valid_to IS NULL AND recorded_to IS NULL;
```

### rationale
- `supports` 是 UUID 数组,可含 fact_id 或 episode_id(混合)。`GET /beliefs/why` 用这些 id 去 facts/episodes 表取详情,组装成支持图。
- stance 枚举与 CortexDB 原版一致。
- 双时态与 facts 相同,beliefs 也超替(新证据修订旧 belief,闭合 valid_to)。

---

## 6. `episodes` 表(轻量,segmenter 后做)

### 角色
有界事件序列。阶段 0 先建表,segmenter 逻辑阶段 3 做。

### Schema

```sql
CREATE TABLE episodes (
    episode_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope           TEXT NOT NULL,
    title           TEXT,                       -- LLM 生成的标题
    event_ids       UUID[] NOT NULL DEFAULT '{}',  -- [event_id, ...]
    actors          TEXT[] NOT NULL DEFAULT '{}',
    causal_chain    JSONB,                      -- [{from, to, relation}, ...]

    -- 双时态
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,                -- NULL = 未封存
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,
    recorded_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_to     TIMESTAMPTZ,

    sealed          BOOLEAN NOT NULL DEFAULT false  -- segmenter 封存
);

CREATE INDEX idx_episodes_scope_time ON episodes (scope, started_at DESC) WHERE recorded_to IS NULL;
CREATE INDEX idx_episodes_actors ON episodes USING gin (actors);
```

### rationale
- event_ids 数组,不在 events 表加 episode_id(避免 events 表频繁更新)。阶段 0 此表为空,仅验证 schema。
- segmenter 策略接口:时间窗规则(30 分钟无新事件)+ preceded_by 链。升级位:LLM 判定边界。

---

## 7. `jobs` 表(Postgres-as-queue)

### 角色
异步任务队列。`SELECT FOR UPDATE SKIP LOCKED` 抢任务。

### Schema

```sql
CREATE TABLE jobs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        TEXT NOT NULL,              -- 'extract'/'segment'/'consolidate'/'synthesize'/'embed'
    scope           TEXT NOT NULL,

    -- 关联(任一)
    event_id        UUID REFERENCES events(event_id),
    batch_id        UUID,                       -- 批量写入

    -- 状态机
    status          TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','completed','failed','cancelled')),
    attempts        INT NOT NULL DEFAULT 0,
    max_attempts    INT NOT NULL DEFAULT 3,

    -- 调度
    priority        INT NOT NULL DEFAULT 0,     -- 高优先级先抢
    run_after       TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 延迟执行/退避
    locked_by       TEXT,                       -- worker 标识
    locked_at       TIMESTAMPTZ,                -- visibility timeout 用

    -- 结果
    payload         JSONB,                      -- 任务输入
    result          JSONB,                      -- 任务输出
    error           TEXT,                       -- 失败原因

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

-- queue 抢任务核心索引
CREATE INDEX idx_jobs_queue ON jobs (priority DESC, run_after, created_at)
    WHERE status = 'queued';
CREATE INDEX idx_jobs_event ON jobs (event_id) WHERE status IN ('queued','running');
```

### Worker 抢任务 SQL
```sql
-- 原子抢一个任务
UPDATE jobs SET
    status = 'running',
    locked_by = $worker_id,
    locked_at = now(),
    started_at = now(),
    attempts = attempts + 1
WHERE job_id = (
    SELECT job_id FROM jobs
    WHERE status = 'queued' AND run_after <= now()
    ORDER BY priority DESC, run_after, created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *;
```

### 决策点与 rationale

**7a. ordering 字段?**
→ **priority DESC, run_after, created_at**。优先级先,然后到期的,最后 FIFO。`run_after` 支持退避(失败后 `run_after = now() + backoff`)。

**7b. dead letter / 重试?**
→ **attempts 上限 + visibility timeout**。`max_attempts=3`,超限标 failed。`locked_at` + 后台扫描把超时(比如 5 分钟)的 running 任务重置为 queued(visibility timeout,防 worker 崩溃)。

**7c. payload 用 JSONB?**
→ **是**。不同 job_type 的输入结构不同(extract 带 event 内容,segment 带 scope 时间窗)。JSONB 灵活。

---

## 8. `scopes` 表(可选,auto-provision)

### 角色
显式注册的 scope(设 members/retention)。auto-provision 的 scope 不在此表(写入时自动建)。

### Schema

```sql
CREATE TABLE scopes (
    scope_path      TEXT PRIMARY KEY,           -- 'org:acme/dept:eng/user:alice'
    parent_path     TEXT REFERENCES scopes(scope_path),  -- 'org:acme/dept:eng'
    members         JSONB NOT NULL DEFAULT '[]',  -- [{actor, role}, ...]
    policies        JSONB NOT NULL DEFAULT '{}',  -- {retention, default_view}
    auto_provisioned BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_scopes_parent ON scopes (parent_path);
CREATE INDEX idx_scopes_auto ON scopes (auto_provisioned) WHERE auto_provisioned = true;
```

### rationale
MVP 阶段此表可选——scope 过滤靠 events/facts 的 scope 列 + LIKE,不依赖此表。此表用于显式注册(设 retention、查 members)。auto-provision 的 scope 只在首次写入时隐式存在(events.scope 有值即可)。

---

## 9. `lifecycle_events` 表(SSE 源)

### 角色
SSE 事件流的数据源。job 状态变化 = lifecycle 事件。

### Schema

```sql
CREATE TABLE lifecycle_events (
    lifecycle_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            TEXT NOT NULL,              -- 'captured'/'extracted'/'indexed'/'consolidated'/'forgotten'/'lagging'
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    scope           TEXT NOT NULL,
    event_id        UUID REFERENCES events(event_id),
    batch_id        UUID,
    job_id          UUID REFERENCES jobs(job_id),
    payload         JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_lifecycle_scope_ts ON lifecycle_events (scope, ts DESC);
CREATE INDEX idx_lifecycle_event ON lifecycle_events (event_id);
```

### rationale
SSE 端点 `GET /lifecycle/stream` 查此表(带 `since_lifecycle_id` 断线续传)。CortexDB 原版的 SSE 帧类型(captured/extracted/indexed/...)对应此表的 kind。lifecycle 事件是 append-only,永不更新。

---

## 10. 关键查询模式(阶段 0 冒烟验证)

### 10.1 双时态 timeline 查询(超替链)
```sql
SELECT fact_id, object_value, valid_from, valid_to
FROM facts
WHERE scope = $1 AND subject_id = $2 AND predicate = $3 AND recorded_to IS NULL
ORDER BY valid_from;
```

### 10.2 图遍历 BFS(见 4d)

### 10.3 实体链接 C 层(向量召回)
```sql
SELECT entity_id, canonical_name, description,
       embedding <=> $query_embedding AS distance
FROM entities
WHERE scope = $1 AND merged_into IS NULL
ORDER BY embedding <=> $query_embedding
LIMIT 5;
```

### 10.4 StratifiedPack 跨层查询
```sql
-- 给定 query embedding 和 scope,并行取四层
-- events(全文+向量)、facts(图遍历)、beliefs(关于命中的实体)、episodes
-- 用 RRF 融合
```

### 10.5 scope 遍历(holistic 向上)
```sql
-- scope = 'org:acme/dept:eng/user:alice' 的 holistic 查询:
-- 查 alice 自己 + 所有祖先 scope
WITH scope_prefixes AS (
    SELECT unnest(string_to_array('org:acme/dept:eng/user:alice', '/')) AS seg
    -- 生成所有前缀:org:acme, org:acme/dept:eng, org:acme/dept:eng/user:alice
)
SELECT f.* FROM facts f WHERE f.scope = ANY($ancestor_scopes) ...;
```

---

## 11. 阶段 0 待用户批准的决策点汇总

新 agent 呈现此 spec 时,重点请用户确认:

1. **scope 用 TEXT + LIKE vs ltree 扩展**?(阶段 0 冒烟对比)
2. **facts.object 双模式(entity_id + 字面值 JSONB)** OK?
3. **supports 用 UUID[] 数组 vs 关联表**?(推荐数组)
4. **超替用 valid_to 闭合 + 查询过滤,不物化 timeline 表** OK?
5. **实体合并用 merged_into 软引用,facts 不改** OK?
6. **object_entity_id 和 subject_id 都建索引**(双向图遍历)OK?
7. **jobs 表 priority/run_after/visibility timeout 策略** OK?
8. **embedding 维度 1536 写死还是 YAML 配**?(推荐 YAML,但阶段 0 先写死 1536 跑通)

---

*批准此 spec 后,阶段 0 的 DDL + 假数据 INSERT + 冒烟 SQL 直接从此 schema 翻译。*
