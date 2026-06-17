# cortex-py 交接文档

> 本文档是项目启动的**唯一权威上下文**。任何新加载的 coding agent 应先完整阅读本文件,再阅读 `docs/specs/` 下的设计文档,然后才开始工作。
>
> 本项目目标是**复刻并简化实现 CortexDB(cortexdb.ai)的 agent 记忆系统**,聚焦知识图谱,定位为个人/小团队可用。**不是**生产级、不支持集群、不做企业安全、不追求 benchmark 复现。

---

## 1. 项目定位

**复刻对象**:CortexDB v1 的核心记忆架构(见 https://cortexdb.ai/docs)。
**本项目范围**:个人/小团队可用,跑通核心闭环,**重点在知识图谱(Facts/Beliefs + 图遍历)**。
**明确不做**:集群、企业安全(加密/TLS/RBAC/SIEM)、benchmark 复现、Understanding 层的完整 LLM 概念合成(MVP 做最简版或跳过)。

### 五层记忆模型的取舍

CortexDB 原版五层:Events → Episodes → Facts → Beliefs → Understanding。

本项目取舍:
- **Events**:完整做(不可变 WAL,append-only)
- **Episodes**:完整 segmenter(异步 job + 因果链),但 segmenter 策略做成可替换接口(时间窗规则起步,留 LLM 判定升级位)
- **Facts**:完整做(双时态三元组)——**图谱核心**
- **Beliefs**:完整做(probabilistic claims + supports 链)——**图谱核心**
- **Understanding**:MVP 做最简版或跳过(最贵最慢,非图谱核心)

---

## 2. 已锁定的技术选型(不可推翻)

以下每一项都是和用户逐轮 brainstorming 后**明确确认**的决策。新 agent **不得更改**这些选型,除非用户明确要求重开讨论。

| 维度 | 选型 | 备注 |
|------|------|------|
| 语言 | Python | — |
| Web 框架 | FastAPI | — |
| ORM | SQLModel / SQLAlchemy | 二选一或混用,由实现时定 |
| 数据库 | PostgreSQL(已提供连接串) | pgvector 扩展 |
| 向量检索 | pgvector | 不引入第二个数据库 |
| 图遍历 | **递归 CTE**(`WITH RECURSIVE`) | 在 facts 表上自连接做 BFS,2-3 跳 |
| 异步任务队列 | **Postgres-as-queue**(`SELECT FOR UPDATE SKIP LOCKED`) | **不用 Redis**(用户确认 Redis 是顺手给的,忽略) |
| LLM 调用 | `openai` Python SDK,走 OpenAI 兼容接口 | 三路独立配置(抽取/答案/合成) |
| 抽取 LLM | structured outputs(JSON schema)吐三元组 | `{subject, predicate, object, valid_from, confidence}` |
| Reranker | 标准 OpenAI rerank 端点 | — |
| Embedding | 标准 OpenAI embedding 端点 | — |
| 配置 | **YAML 文件**,三路 LLM + rerank + embedding 分开配 | 不用 env/TOML |
| 实体链接 | **B over C**:pgvector 召回候选(K=5)→ 灰区(0.3~0.85)走 LLM 判定 → 阈值兜底(>0.85 直接合并,<0.3 直接新建) | entity 表带 embedding 列,抽取管线一等公民 |
| Episodes | **完整 segmenter**(异步 job),策略接口可替换 | 时间窗规则起步 |
| 授权 | **静态 API key + scope 路径隔离**,无 token 签名 | API key 配在 YAML,scope 路径(如 `org:acme/dept:eng/user:alice`)是权限边界 |
| 编排方案 | **方案 2:分层构建**,但**强制加阶段 0**(schema 脚手架 + 假数据 + SQL 冒烟测试) | 见下方阶段划分 |

---

## 3. 已提供的基础设施

- **PostgreSQL 连接串**(用户提供,仅本文件记录,勿外泄):
  - `postgresql://postgres:0prV2JrQ1uJSBHZ2@192.168.1.21:5432/postgres`
- **Redis**:用户确认"随便给的,不要理他"。**不使用 Redis**。

---

## 4. 分阶段实施路线(方案 2 + 阶段 0)

```
阶段 0(3-5 天)→ 阶段 1 → 阶段 2 → 阶段 3 → 阶段 4 → 阶段 5
   │
   └─ schema 脚手架 + 假数据 + SQL 冒烟测试
      (验证图谱 schema 能撑住图遍历 + 实体链接,再写任何业务代码)
```

| 阶段 | 内容 | 预估 |
|------|------|------|
| **0** | 完整 schema DDL + 假数据 INSERT + 图遍历 CTE 冒烟脚本 + 向量召回 SQL 验证 | 3-5 天 |
| **1** | WAL + Events 层 + Postgres queue 基础设施 | 1-2 周 |
| **2** | 异步管线框架(worker 循环 + SSE + job 表) | 1 周 |
| **3** | 抽取 + 实体链接(B over C)+ beliefs 聚合 | 1-2 周 |
| **4** | 检索(4 通道:BM25 + pgvector + 图遍历 + rerank + RRF) | 1 周 |
| **5** | FastAPI 端点(experience/recall/answer/forget)+ answer LLM | 1 周 |

> **⚠️ 上表是初版。经通读 56 篇原文的差距分析,`docs/specs/05-gap-closure.md` 已把路线图扩展为 Stage 0→7**,并纳入 A 档(blobs / 批量+导入 / vocabularies / erasures)与 B 档(StratifiedPack 装配 / forget·erasures 双轨 / `?wait=` / 层直读 / `/answer` 管线)。**以 05 §5 的修订路线图为准。**

### 阶段 0 的关键验证目标

在写任何 Python 业务代码前,用纯 SQL 验证以下问题:
1. **图遍历性能**:递归 CTE 在 1 万条 facts 上做 2-3 跳 BFS,是 200ms 还是 2s?
2. **实体链接 schema**:entity 表能否同时支持"向量召回 top-K + 规范化 name + 别名 + 合并/分裂"?
3. **双时态超替**:新 fact 到达时闭合旧 fact 的 `valid_to`,timeline 查询能否正确返回历史?
4. **索引设计**:subject_id/object_id/predicate/scope 上需要哪些索引支撑 CTE 和过滤?

如果阶段 0 发现 schema 不对,**改的是 SQL 脚本,不是代码库**。

---

## 5. 阶段 0 待设计的关键 schema 决策(尚未拍板)

新 agent 接手后,**第一步是把这些决策呈现给用户批准**,然后才能写阶段 0 的 DDL。这些决策是数据模型设计章节的核心:

1. **Facts 表结构**:
   - 三元组列怎么存(`subject_id` UUID FK → entities?还是裸字符串?)
   - 双时态 4 字段(`valid_from/valid_to/recorded_from/recorded_to`)的 NULL 语义
   - 超替链:用 `valid_to` 闭合 + 查询时过滤,还是物化一张 `fact_timelines` 表?
   - `supports` 数组怎么存(Postgres `ARRAY[uuid]`?还是单独的 `fact_supports` 关联表?)

2. **Entity 表结构(B over C 承载)**:
   - `id` / `canonical_name` / `aliases[]` / `embedding`(pgvector)/ `description` / `scope`
   - 合并/分裂时如何处理(软删除 + redirect?还是 facts 重指?)
   - embedding 用哪个字段算(canonical_name?name+description?)

3. **图遍历 CTE 的落点**:
   - 在 `facts` 表自连接(`subject_id` = `object_id`),还是单独物化一张 `graph_edges` 视图?
   - BFS 的终止条件(固定 max_hops?还是基于 valid_to 窗口?)

4. **Events/WAL 表**:
   - WAL offset 怎么生成(Postgres `BIGSERIAL`?还是单独序列?)
   - idempotency_key 的唯一约束(scope, idempotency_key)
   - content 存 JSONB 还是拆字段?

5. **Job 表(Postgres-as-queue)**:
   - `FOR UPDATE SKIP LOCKED` 的 ordering 字段(created_at?优先级?)
   - job 状态机(queued/running/completed/failed)的约束
   - dead letter 处理(重试次数上限?visibility timeout?)

6. **Scope 模型**:
   - scope 路径是字符串(`org:acme/dept:eng/user:alice`)还是拆成段表?
   - holistic/descend 遍历怎么用 SQL 表达(LIKE 前缀?递归 CTE?ltree 扩展?)

---

## 6. brainstorming 状态与流程要求

本项目经过完整的 brainstorming 流程(见 superpowers:brainstorming skill)。当前状态:

- ✅ 范围已定(B:个人/小团队,聚焦图谱)
- ✅ 全部技术选型已确认(见第 2 节)
- ✅ 编排方案已定(方案 2 + 阶段 0)
- ✅ **调研笔记已写**:`docs/specs/02-research-notes.md`
- ✅ **技术选型 spec 已写**:`docs/specs/01-technical-decisions.md`
- ✅ **数据模型 spec 已写**:`docs/specs/03-data-model.md`(8 表完整 schema + **7 决策点全部裁定**)
- ✅ **运行时风险登记**:`03-data-model.md` 第 12 节(3 项,对应阶段验证)
- ✅ **阶段 0 冒烟测试计划已写**:`docs/specs/04-stage0-smoke-test.md`
- ✅ **阶段 0 冒烟脚本已就绪**:`scripts/stage0/decision_probe.py`(待 Postgres 可达执行)
- ✅ **缺口闭合 spec 已写**:`docs/specs/05-gap-closure.md`(A 档纳入 + B 档设计 + 路线图扩展为 Stage 0→7;Q1 三槽身份 / Q2 structured 视图均已裁定)
- ✅ **阶段 0 冒烟执行**:`scripts/stage0/run_all.sh` 跑通,37 PASS / 0 FAIL(真实 PG 18.4 @ 192.168.1.21)。覆盖:双时态超替/timeline/as_of、递归 CTE 2-3 跳 BFS、pgvector 召回 + B over C 三阈值、scope 隔离 + holistic/descend/structured + LIKE vs ltree、Postgres-as-queue(SKIP LOCKED/priority/visibility timeout/死信)、blobs SHA-256 去重、vocabularies coerce(closed/open)、erasures 引用计数(redact vs delete + array_remove + blob 清理)。**无 psql 依赖**(走 psycopg2 + psql 变量预处理器)。
- ⬜ **用户 review specs**(下一步)
- ⬜ **阶段 0 执行**(待 Postgres `192.168.1.21` 可达)
- ⬜ 实现计划(writing-plans)

### 对新 agent 的流程要求

1. **先读本文件**,然后读 `docs/specs/` 下四份 spec(01→02→03→04 顺序)
2. **7 个数据模型决策点已全部裁定**(见 `03-data-model.md` 第 11 节),**不需要再问用户**。如有颠覆性实测证据再提请用户复议。
3. **3 个运行时风险已登记**(见 `03-data-model.md` 第 12 节),在对应阶段起始验证。
4. **执行阶段 0**:Postgres 可达后,跑 `scripts/stage0/decision_probe.py` 验证图遍历/scope 性能;按 `04-stage0-smoke-test.md` 补全假数据 + 双时态/链接/隔离冒烟脚本。
5. 阶段 0 通过后,转入 writing-plans 制定阶段 1-5 的实现计划。
6. **硬性约束:阶段 0 冒烟通过前,不写 Python 业务代码**

**这是 brainstorming skill 的 HARD-GATE。**

---

## 7. 关键风险提醒(给新 agent)

1. **实体链接是图谱质量的命门**。用户明确"尤其关注知识图谱"。MVP 不要用纯字符串归一糊弄,B over C 策略是第一版就要上的。阶段 0 的 schema 必须为 B over C 设计(entity 表带 embedding + 灰区判定支持)。

2. **图谱隔离靠 scope 路径**。没有 scope 隔离,所有用户的 "Acme" 会糊在一个图里。授权虽简化(静态 API key),但 scope 路径过滤是 recall 和图遍历的强制条件。

3. **Postgres-as-queue 不要被 Redis 诱惑**。用户明确确认不用 Redis。即使用户提到 Redis,也要确认是不是改主意——本项目的队列是 Postgres。

4. **阶段 0 是方案 2 的保险**。用户选了分层构建(先打地基),这有"schema 过度设计、召回质量晚验证"的风险。阶段 0 用假数据 + SQL 冒烟测试提前暴露 schema 问题,**不要跳过阶段 0 直接写 Python**。

5. **CortexDB 文档本地存档**。完整的 56 页文档全文(排除 connectors)已抓取并存档于 `/tmp/cortex_pages/`(注:这是上一个 agent 会话的临时文件,可能已清理)。新 agent 如需查阅 CortexDB 原始设计,访问 https://cortexdb.ai/docs/ 重新抓取。关键参考:
   - 架构白皮书:https://cortexdb.ai/docs/research/architecture-whitepaper
   - Recall Tuning(检索管线完整披露):https://cortexdb.ai/docs/operations/recall-tuning
   - Facts API:https://cortexdb.ai/docs/api-reference/facts
   - Beliefs API:https://cortexdb.ai/docs/api-reference/beliefs

---

## 8. Python 项目结构(建议,待用户确认)

```
cortex-py/
├── pyproject.toml
├── config/
│   └── config.yaml          # 三路 LLM + rerank + embedding 配置
├── docs/
│   ├── HANDOFF.md           # 本文件
│   └── specs/               # 设计文档(待写)
├── src/
│   └── cortex/
│       ├── __init__.py
│       ├── config.py        # YAML 加载
│       ├── db.py            # SQLModel/SQLAlchemy engine + session
│       ├── models/          # 数据模型(Events/Facts/Entities/Beliefs/Jobs/...)
│       ├── wal/             # Events 层 + append
│       ├── extraction/      # 抽取 + 实体链接(B over C)
│       ├── graph/           # Facts/Beliefs + 递归 CTE 遍历
│       ├── retrieval/       # BM25 + pgvector + 图 + RRF + rerank
│       ├── worker/          # Postgres-as-queue worker
│       └── api/             # FastAPI 路由
├── scripts/
│   └── stage0/              # 阶段 0 的 DDL + 假数据 + 冒烟脚本(待写)
└── tests/
```

---

## 9. 参考实现细节(CortexDB 原版,用于对照)

### 双时态 4 字段(每条派生记录)
- `valid_from` / `valid_to`:在世界中何时为真(valid_to = null 表示开放)
- `recorded_from` / `recorded_to`:系统何时获知(recorded_to = null 表示当前)
- Events 只有 `observed_at` + `recorded_at`(原子,不超替)

### 超替语义
新证据到达 → 旧 fact 的 `valid_to` 闭合为新的 `valid_from`,旧记录保留 → timeline 查询返回完整值演变历史。

### 图遍历查询(CortexDB 原版示例)
```json
{
  "query": "who's negotiating the Acme renewal",
  "graph": {
    "seed_entities": ["ent_acme_corp"],
    "max_hops": 2,
    "predicates": ["owns","has_status","works_at"],
    "as_of": "2026-05-15T00:00:00Z"
  }
}
```

### 4 通道混合检索 + RRF
- BM25(Tantivy)→ 本项目用 Postgres 全文检索 `tsvector` 替代
- HNSW 向量 → 本项目用 pgvector HNSW 索引
- 图遍历 → 本项目用递归 CTE
- Cross-encoder rerank → 本项目用 OpenAI rerank 端点
- 融合:Reciprocal Rank Fusion(k=60)

### Experience Envelope(统一写入载荷)
判别联合体,覆盖 message/text/json/blob_ref/triple。强制 idempotency_key(≤64 字符)。三个身份槽:caller / observed_actor / subject。

### 实体链接 B over C 流程
```
新抽取出的实体提到 "Bob"
   ├─ C 层:pgvector 在 entity 表查 top-5 最近邻
   │        → [ent_bob_eng, ent_bob_sales, ent_robert_smith, ...]
   └─ B 层:把"event 原文 + 候选 + 候选描述"喂抽取 LLM
            structured output 里判定:复用哪个 / 新建
   阈值兜底:cosine > 0.85 直接合并(省 LLM);< 0.3 直接新建;中间灰区走 LLM
```

---

*本交接文档由 brainstorming 会话生成。下一步:新 agent 呈现数据模型设计章节,逐节获用户批准。*
