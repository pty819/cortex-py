# 01 — 技术选型与架构决策

> **状态**:已锁定(经 brainstorming 全流程逐项确认)
> **不可推翻**:除非用户明确要求重开某项讨论
> **日期**:2026-06-18

---

## 1. 项目定位

### 目标
复刻并简化实现 [CortexDB](https://cortexdb.ai/docs/) 的 agent 记忆系统,定位为**个人/小团队可用**的长期记忆层。

### 核心关注
**知识图谱**(Facts + Beliefs + 图遍历)。这是整个系统最有价值的部分,也是设计与实现的重点投入方向。

### 明确不做(YAGNI)
- ❌ 集群(gossip / 一致性哈希 / 多节点复制)
- ❌ 企业安全(加密 at rest / TLS / RBAC / SIEM / DSAR 工作流)
- ❌ Benchmark 复现(LongMemEval-S / LoCoMo)
- ❌ Understanding 层的完整 LLM 概念合成(MVP 最简版或跳过)
- ❌ PASETO token 签名 / 4 层能力栈

---

## 2. 五层记忆模型取舍

CortexDB 原版五层:Events → Episodes → Facts → Beliefs → Understanding。

| 层 | 本项目 | 理由 |
|----|--------|------|
| **Events** | ✅ 完整做 | WAL 是唯一真相源,所有派生层的基础 |
| **Episodes** | ✅ 完整 segmenter | 保留会话上下文;策略接口可替换(时间窗规则起步,留 LLM 判定升级位) |
| **Facts** | ✅ 完整做 | **图谱核心**——双时态三元组 |
| **Beliefs** | ✅ 完整做 | **图谱核心**——probabilistic claims + supports 链 |
| **Understanding** | ⚠️ 最简版/跳过 | 最贵最慢(每 topic 一次 LLM),非图谱核心,MVP 阶段收益不抵成本 |

---

## 3. 锁定的技术选型

| 维度 | 选型 | 决策理由 |
|------|------|----------|
| **语言** | Python | 用户指定 |
| **Web 框架** | FastAPI | 用户指定 |
| **ORM** | SQLModel / SQLAlchemy | 用户指定,混用由实现时定 |
| **数据库** | PostgreSQL | 用户指定 + 提供连接串 |
| **向量检索** | pgvector(HNSW 索引) | 不引入第二个数据库,单 Postgres 存一切 |
| **全文检索** | Postgres `tsvector` + GIN 索引 | 替代 CortexDB 的 Tantivy BM25 |
| **图遍历** | 递归 CTE(`WITH RECURSIVE`) | 在 facts 表自连接做 BFS,2-3 跳,纯 SQL |
| **异步队列** | Postgres-as-queue(`SELECT FOR UPDATE SKIP LOCKED`) | **零新依赖**,任务状态与 facts/beliefs 同事务 |
| **LLM 客户端** | `openai` Python SDK(自定义 `base_url`) | 走 OpenAI 兼容接口,本地 Ollama 与云端通用 |
| **抽取 LLM** | structured outputs(JSON schema)吐三元组 | 比自由文本 + 正则解析可靠得多 |
| **答案 LLM** | 独立配置 | recall pack → 带引用答案 |
| **合成 LLM** | 独立配置(可关) | Understanding/Beliefs 合并 |
| **Reranker** | 标准 OpenAI rerank 端点 | 用户指定 |
| **Embedding** | 标准 OpenAI embedding 端点 | 用户指定 |
| **配置** | YAML 文件 | 三路 LLM + rerank + embedding 分开配 |
| **实体链接** | **B over C** | 见第 4 节 |
| **Episodes** | 完整 segmenter,策略接口可替换 | 见第 5 节 |
| **授权** | 静态 API key + scope 路径隔离 | 无 token 签名,scope 即权限边界 |

### 配置结构(YAML)

三路 LLM + rerank + embedding 分开配。具体字段命名见数据模型 spec。

**配置规范**(基于用户提供的实际服务配置):

| 字段 | 出现在 | 作用 | 必填 |
|------|--------|------|------|
| `provider` | 全部 | 仅标注 metadata,实际全走 OpenAI 兼容接口 | 是 |
| `api_key` | 全部 | bearer key(本地服务常用占位符如 `"local"`) | 是 |
| `api_base` | 全部 | **统一到 `/v1`,不含端点路径**(见下方 URL 规范) | 是 |
| `model` | 全部 | 模型名 | 是 |
| `temperature` | llm.* | 抽取用 0.0(确定性输出) | 否 |
| `timeout` | 全部 | 秒,请求超时 | 否 |
| `max_retries` | 全部 | 失败重试次数 | 否 |
| `max_concurrent` | embedding, llm | worker 并发上限(限流) | 否 |
| `dimension` | embedding | 输出维度,**必须等于 entities.embedding 的 vector(N)** | 是 |
| `threshold` | rerank | 分数低于此值丢弃 | 否 |

**URL 规范(1c 决策)**:`api_base` 永远只到 `/v1`,端点路径由 worker 拼:
- embedding: `f"{api_base}/embeddings"`
- rerank: `f"{api_base}/rerank"`
- llm(chat): `f"{api_base}/chat/completions"`

**维度陷阱警告**:`embedding.dimension` 必须等于 `entities.embedding` 的 `vector(N)`。启动时**强制校验**,不一致拒绝启动(避免 CortexDB 文档警告的"静默召回失败,全 0 结果")。

**实际服务配置**(用户提供,作为参考与测试默认值):

```yaml
# config/config.yaml
embedding:
  provider: jina
  api_key: local
  api_base: http://192.168.1.238:8000/v1   # 本地 MLX 服务,不含端点路径
  model: jina-embeddings-v5-text-small
  dimension: 1024                          # ⚠️ jina-v5-text-small 输出 1024 维
  max_concurrent: 10

rerank:
  provider: openai
  api_key: local
  api_base: http://192.168.1.238:8000/v1   # 同一本地服务,/rerank 由 worker 拼
  model: prism-qwen3.5-reranker-0.8b-optiq-5bpw
  threshold: 0.1

llm:
  extraction:
    provider: openai
    model: Minimax-M3
    api_key: sk-cp-NYG22b...               # 远程 Minimax(见下方"服务拓扑")
    api_base: https://api.minimaxi.com/v1
    temperature: 0.0
    timeout: 600
    max_retries: 2
    max_concurrent: 10
  answer:
    # MVP 阶段复用 extraction 配置(同一 VLM 端点)
    provider: openai
    model: Minimax-M3
    api_key: sk-cp-NYG22b...
    api_base: https://api.minimaxi.com/v1
    temperature: 0.0
    timeout: 600
    max_retries: 2
  synthesis:
    # MVP 阶段复用;Understanding 跳过,此段保留以备 Beliefs 聚合用
    provider: openai
    model: Minimax-M3
    api_key: sk-cp-NYG22b...
    api_base: https://api.minimaxi.com/v1
    temperature: 0.0
    timeout: 600
    max_retries: 2

database:
  url: postgresql://postgres:0prV2JrQ1uJSBHZ2@192.168.1.21:5432/postgres
api:
  key: ""  # 静态 API key(待用户填)
```

> **注**:YAML 里三路 LLM 分段保留(将来好分叉到不同模型),但 MVP 测试阶段三段填相同值(用户确认)。

### 服务拓扑(基于实际配置)

| 服务 | 位置 | 用途 |
|------|------|------|
| Jina v5 MLX Server | `http://192.168.1.238:8000` | 本地 embedding + rerank + chat proxy(三个 `/v1/*` 端点) |
| Minimax-M3 VLM | `https://api.minimaxi.com/v1` | 远程抽取/答案/合成 LLM |
| PostgreSQL | `192.168.1.21:5432` | 数据库 + pgvector + 队列 |

**注意**:本地 MLX 服务(`192.168.1.238`)同时提供 `/v1/embeddings`、`/v1/rerank`、`/v1/chat/completions` 三个端点(经 OpenAPI 文档确认)。但用户的 VLM 配置指向远程 Minimax,说明 VLM 走远程、embedding/rerank 走本地。worker 按各自配置的 `api_base` 调用,不假设三个服务同址。

---

## 4. 实体链接策略:B over C(图谱质量的命门)

### 问题
LLM 抽取时不知道"这个 Bob 是不是之前那个 Bob"。纯字符串归一(`{bob:"Robert Smith"}`)覆盖不了语义别名;纯 LLM 归一(prompt 爆炸)成本高;纯向量归一同名不同人会糊一起。

### 方案:分层链接(C 召回 + B 判定)

```
新抽取出的实体提到 "Bob"
   │
   ├─ C 层(向量召回):pgvector 在 entity 表查 top-5 最近邻候选
   │                  cosine > 0.85 → 直接合并(省 LLM)
   │                  cosine < 0.30 → 直接新建(省 LLM)
   │                  0.30 ~ 0.85   → 灰区,走 B 层
   │
   └─ B 层(LLM 判定):把"event 原文 + 候选实体 + 候选描述"
                       喂抽取 LLM,在 structured output 里判定
                       复用哪个 / 新建
```

### 为什么是 B over C 而非单方案

| 维度 | 纯 B | 纯 C | **B over C** |
|------|------|------|-------------|
| 别名(Bob=Robert) | ✅ | ✅ | ✅ |
| 同名不同人(Acme×2) | ✅ | ❌ 糊一起 | ✅ |
| Prompt 成本 | ❌ 爆炸 | — | ✅ 只喂 K 个候选 |
| 可解释 | 中 | 低 | ✅ LLM 给理由 |

### schema 影响
entity 表必须带 `embedding` 列(pgvector),支持向量召回。这一结构同时兼容纯字符串归一(A,降级)和纯 LLM 归一(B,升级),不会因为链接策略演进而推翻 schema。

---

## 5. Episodes 策略:完整 segmenter + 可替换接口

### 落地
- event 表带 `episode_id` 字段
- 独立的异步 segmenter job(走 Postgres-as-queue)
- segmenter 策略是**可替换接口**:
  - 起步:时间窗规则(30 分钟无新事件则封存)+ `preceded_by` 链
  - 升级位:LLM 判定 episode 边界

### 为什么完整做而非轻量标记
用户明确选择方案 C(完整 segmenter),保留 episode 作为图谱上下文的一等公民——因果链、会话级聚合检索都依赖它。

---

## 6. 授权模型:静态 API key + scope 路径

### 落地
- YAML 配一个静态 API key
- 每个请求带 `Authorization: Bearer <key>` + `X-Cortex-Actor: <actor>`
- **不校验 token 签名**,key 对就放行
- **scope 路径强制**:所有读写必须指定 scope(如 `org:acme/dept:eng/user:alice`)
- scope 是权限边界:能写到这个 scope 的 key 就能读写这个 scope 的记忆

### 为什么不做 PASETO / 能力栈
- 个人/小团队场景,身份认证不是痛点
- 图谱隔离靠 scope 路径过滤(recall 和图遍历的强制条件),不靠身份
- 将来要上多用户,API key 换 PASETO 是局部改动,scope 模型不动

### scope 隔离的强制性
**没有 scope 隔离,所有用户的 "Acme" 会糊在一个图里。** 这是图谱质量的底线。即使授权简化,scope 过滤在 SQL 层强制:
- 所有查询 `WHERE scope = $1` 或 `WHERE scope LIKE $1 || '/%'`(descend)
- 图遍历 CTE 内部也带 scope 过滤

---

## 7. 编排方案:分层构建 + 强制阶段 0

### 方案选择
**方案 2(分层构建)**:先打牢存储和数据模型,再做异步管线,再做抽取/链接,最后检索+API。

### 方案 2 的风险与缓解
**风险**:前 3-4 周"没有能跑的东西",且 schema 在没看到真实召回数据前难一次做对。Facts/实体链接的 schema 和图遍历的 CTE 性能,是两个最难一次设计对的点。

**缓解:强制阶段 0**——在写任何 Python 业务代码前,用纯 SQL + 假数据验证 schema 能否撑住图遍历和实体链接。schema 不对改的是 SQL 脚本,不是代码库。

### 分阶段路线

| 阶段 | 内容 | 预估 | 交付物 |
|------|------|------|--------|
| **0** | schema DDL + 假数据 + 图遍历 CTE 冒烟 + 向量召回 SQL 验证 | 3-5 天 | `scripts/stage0/`,验证报告 |
| **1** | WAL + Events 层 + Postgres queue 基础设施 | 1-2 周 | 可 append event,job 表可用 |
| **2** | 异步管线框架(worker 循环 + SSE + job 状态机) | 1 周 | worker 能跑 job,SSE 能推 |
| **3** | 抽取(structured outputs)+ 实体链接(B over C)+ beliefs 聚合 | 1-2 周 | event → facts → beliefs 闭环 |
| **4** | 检索(4 通道:BM25/tsvector + pgvector + 图遍历 + rerank + RRF) | 1 周 | recall 端点可用 |
| **5** | FastAPI 端点(experience/recall/answer/forget)+ answer LLM | 1 周 | 端到端闭环 |

### 阶段 0 的验证目标
1. **图遍历性能**:递归 CTE 在 1 万条 facts 上做 2-3 跳 BFS,200ms 还是 2s?
2. **实体链接 schema**:entity 表能否支持"向量召回 top-K + 规范化 name + 别名 + 合并/分裂"?
3. **双时态超替**:新 fact 到达闭合旧 fact 的 `valid_to`,timeline 查询正确?
4. **索引设计**:subject_id/object_id/predicate/scope 上需要哪些索引?

---

## 8. 与 CortexDB 原版的偏差总结

| 维度 | 原版 | 本项目 | 偏差理由 |
|------|------|--------|----------|
| 图遍历 | 原生 BFS(自建,Rust) | 递归 CTE(Postgres) | 单库,纯 SQL,2-3 跳够用 |
| 任务队列 | 自建 lifecycle stream | Postgres-as-queue | 零依赖,同事务 |
| BM25 | Tantivy | Postgres `tsvector` | 单库 |
| 向量 | HNSW(自建) | pgvector HNSW | 单库 |
| 实体链接 | 未明示 | B over C | 图谱质量命门,显式设计 |
| 授权 | PASETO + 4 层能力栈 | 静态 API key + scope | 个人/小团队,YAGNI |
| 集群 | gossip + 一致性哈希 | 单机 | YAGNI |
| 企业安全 | 加密/TLS/RBAC/SIEM/DSAR | 无 | YAGNI |
| Understanding | LLM 概念合成 | 最简版/跳过 | 非图谱核心,MVP 砍 |
| 配置 | TOML + 100 env vars | YAML | 简单 |

---

## 9. brainstorming 决策溯源(每项选型的来龙去脉)

供新 agent 理解"为什么不是别的"。完整对话见会话历史,此处是精简溯源。

### 为什么递归 CTE 而非 Apache AGE?
AGE 更强(原生 Cypher 图引擎)但增加运维复杂度(装 PG 扩展),且 SQLModel 抽象不全覆盖。递归 CTE 是标准 SQL,纯 SQLModel 可表达,支撑 CortexDB 典型的 `max_hops=2` 图遍历足够。符合"先做出来"。

### 为什么 Postgres-as-queue 而非 Celery?
Celery 对 MVP 太重(多 broker + worker 进程)。FastAPI BackgroundTasks 太脆(进程重启丢任务)。Postgres `SKIP LOCKED` 是成熟模式,零新依赖,任务状态天然与 facts/beliefs 同事务,崩溃恢复 + SSE 通知都基于同一张表。用户确认 Redis 是顺手给的,不用。

### 为什么 openai SDK 而非 litellm?
litellm 对 MVP 是过度抽象。openai SDK 稳定、structured outputs 支持好、可指向任何 base_url(本地 Ollama / 云端通用)。

### 为什么抽取/答案/合成三路独立配?
三处调用的生命周期、成本、失败处理完全不同:抽取最频繁(要便宜快)、答案要强、合成可关。混在一起配置会导致"想换答案模型却影响了抽取"。

### 为什么 B over C 而非纯 A(字符串归一)起步?
用户明确"尤其关注知识图谱"。图谱质量的命门是实体链接。纯 A 会让"Acme"和"Acme Corp"断开,图遍历失效。B over C 第一版就上,schema 为它设计。

### 为什么静态 API key 而非 PASETO?
用户明确"安全真的不需要"。个人/小团队场景,身份认证不是痛点。scope 路径过滤提供图谱隔离(真正的底线),不依赖身份认证。

### 为什么方案 2(分层)而非方案 1(垂直切片)?
用户明确选方案 2。agent 表达了风险顾虑(schema 难一次做对),用户接受并同意加强制阶段 0 作为缓解。

---

*下一步:新 agent 呈现 [`02-research-notes.md`](02-research-notes.md)(前因后果)与 [`03-data-model.md`](03-data-model.md)(数据模型设计),逐节获用户批准。*
