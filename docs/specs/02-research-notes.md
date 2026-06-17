# 02 — CortexDB 调研笔记:前因后果

> 本文档记录 CortexDB 原版设计的**核心理念、关键架构决策、及其背后的 rationale**,作为本项目复刻实现的"为什么"参考。
>
> 所有内容来自 `docs/reference/` 下 56 篇原文,重点参考 [`research/res_arch.txt`](../reference/research/res_arch.txt)(架构白皮书)与 [`operations/ops_recall-tuning.txt`](../reference/operations/ops_recall-tuning.txt)(检索管线)。

---

## 1. 核心理念

CortexDB 由 Apache Cassandra 联合作者 Prashant Malik 主导。核心立场一句话:

> **"记忆不是 prompt 的附属品,而是需要数据库级工程 rigor 的一等公民。"**

它明确反对的范式:**"把数据先喂 LLM 重写一遍再存储"**(Mem0/LangMem 的做法)。理由:每次重写都是有损翻译,原始信号一旦丢失,未来更好的模型也无法恢复。

CortexDB 的立场:**log is sacred, views are disposable**(日志是神圣的,视图是一次性的)。

### 五条设计约束(驱动整个架构)

1. **写路径零信息损失**——原始 bytes 原样保留,摘要/结构化全部下游异步,可重跑
2. **双时态正确性**——必须能同时回答"现在什么是真的"和"昨天 02:14 我们知道什么"
3. **Scope 是一等概念**——多租户/多用户/多 agent 归约为同一个原语
4. **基于能力的授权**——每次拒绝指明哪层、缺哪个能力,不允许模糊 403
5. **默认异步,可同步完成**——写入 <10ms 返回 202,需要时 `?wait=indexed` 阻塞

### 本项目继承度

| 约束 | 继承 | 备注 |
|------|------|------|
| 写路径零信息损失 | ✅ 完整继承 | WAL 是灵魂 |
| 双时态正确性 | ✅ 完整继承 | Facts 的 4 时间字段 |
| Scope 一等概念 | ✅ 完整继承 | 图谱隔离靠它 |
| 能力授权 | ❌ 简化 | 静态 API key + scope,不做 4 层栈 |
| 默认异步 | ✅ 完整继承 | Postgres-as-queue |

---

## 2. 五层记忆模型(为什么是五层)

### 核心论点
**单层记忆系统必然失败。** 不同认知任务需要不同抽象层级的记忆。单层设计(如纯向量库)把原始观察("用户点了取消")和概率结论("用户不喜欢 UI")塞进同一个向量空间,破坏了置信度作为一等指标的能力。

### 五层

| 层 | 形状 | 由谁构建 | 置信度 | 延迟 | 检索用途 |
|----|------|----------|--------|------|----------|
| **Events** | 不可变 append-only(`event_id`, payload, scope, `observed_at`, `recorded_at`) | 同步写入 | 1.0 | <10ms | 逐字引用、审计、重放 |
| **Episodes** | 有界相关事件序列(`episode_id`, member events, summary, 因果链) | segmenter 异步 | 1.0 | 秒级 | 会话/对话上下文 |
| **Facts** | 双时态三元组(`subject`,`predicate`,`object` + 4 时间字段) | LLM extractor 异步 | 0-1 | 5-30s | 结构化 Q&A、时间线 |
| **Beliefs** | 概率断言 + `supports:[fact_id,...]` + 置信区间 | aggregator 异步 | 0-1 | 分钟级 | "你目前怎么看 X" |
| **Understanding** | 高阶综合摘要 + related 图 | LLM synthesizer 异步 | 隐式 | 分钟-小时 | 长程概览 |

### 关键性质
- **每层都可独立寻址、可查询**(不是纯物化视图)
- **除 Events 外,每个派生记录携带 `supports` 链**指回产生它的不可变事件——证据可追溯、"诚实遗忘"的基础
- **Facts 是最大单一贡献者**(消融:去掉 Facts 层 LongMemEval-S 掉 22.4pp)
- **知识图谱不是独立层**——是 Facts(边)+ Beliefs(带证据节点)的涌现视图

### 本项目继承
Events/Episodes/Facts/Beliefs 完整做,Understanding 最简版/跳过。

---

## 3. 事件溯源(为什么不用 LLM 摘要写)

### 问题
AI agent 跨会话丢上下文。标准修复:用 LLM 把历史对话压成摘要,检索时取摘要。**这是有损的**——每次重写都是翻译,每次翻译丢信息。等 agent 回忆三段对话前客户说的话,它回忆的是"LLM 对 LLM 解读的解读"。

### CortexDB 的立场
原始 event 是唯一真相源。摘要、嵌入、知识图谱都是派生 artifact:**异步构建、可替换、永不在写路径上**。明年出了更好的嵌入模型?从原始 event 重新嵌入。Mem0 做不到——原始数据已丢。

### 写路径
写入只做一件持久的事:append 一个 event 到 WAL,~5ms 返回 202。其余全部异步:
- 嵌入
- 实体抽取
- fact 合并
- belief 修订
- understanding 合成

### 四个保证(派生视图是 `(events, derivation_version)` 的确定性函数)
1. **可重现**:同日志两次重放,给定同 extractor 版本,派生状态字节相同
2. **可逆**:坏的合并运行,丢掉派生视图重放即可,无数据损失
3. **可审计**:每个 fact/belief/合成段落携带 `supports:[event_id,...]`
4. **合规**:GDPR 遗忘可原地 redact 或硬删 event,再重派生下游一切

### 本项目继承
完整继承。WAL append 是唯一写操作,所有 LLM 工作异步。Postgres `BIGSERIAL` 作 WAL offset。

---

## 4. 双时态模型(为什么两轴时间不可商量)

### 问题
大多数向量库和单轴记忆系统在新事实到达时**覆盖**旧数据。单轴更新破坏 agent 的历史上下文,无法回答"上个月你对这个用户是怎么想的?"

单轴系统要么答历史真相,要么答历史认知,**不能两者都答**。

### 双时态
两条正交时间轴:

| 字段 | 含义 |
|------|------|
| `valid_from` | 该断言在世界中**开始为真**的时刻 |
| `valid_to` | 该断言**不再为真**的时刻(null = 开放) |
| `recorded_from` | 系统**获知**该断言的时刻 |
| `recorded_to` | 系统**停止相信**它的时刻(null = 当前) |

Events 只有 `observed_at` + `recorded_at`(原子,永不超替)。

### 四种查询模式(都是直接 typed-store 查找,不调 LLM、不扫描)
- **Now**:`valid_to IS NULL AND recorded_to IS NULL`(当前状态 + 当前认知)
- **As-of**:`valid_from <= t < valid_to`(t 时刻什么是真的)
- **As-known**:`recorded_from <= t < recorded_to`(t 时刻系统相信什么)
- **History**:无界超替链全历史

### 超替语义(RECONCILE 阶段)
新证据到达时,旧 fact 的 `valid_to`(可能还有 `recorded_to`)被关闭为新的 `valid_from`,**链被保留**——`GET /facts/timeline` 返回完整值演变历史。

### 本项目继承
完整继承。Facts 表 4 时间字段,超替不覆盖,timeline 查询是图谱时间推理的基础。

---

## 5. Scopes(为什么层级命名空间)

### 问题
传统向量库把命名空间拆成多个不相交概念(各有独立访问模型):tenant、namespace、workspace。这种碎片化造成严重安全风险和不清的数据泄露边界。重叠的访问控制系统不可避免地导致企业 agent 把私有数据召回进共享对话。

### CortexDB 方案
用**单一层级原语**:delimited path of `type:id` segments。
```
org:acme/dept:eng/team:platform/user:alice
```
- 段格式 `type:id`,`type` 来自小枚举(org/dept/team/user/agent/service/system/ws),`id` 自由形式
- 最左段最外层,层级继承
- ≤ 8 段,每段 ≤ 64 字符

### 三种语义
1. **寻址**:每个 experience 写到一个 scope
2. **读语义**:holistic(向上遍历祖先)/ descend(向下遍历后代)/ local / granular
3. **策略**:能力可在任何节点授予,沿路径继承

### 本项目继承
完整继承 scope 路径模型。三种遍历视图(local/holistic/descend)。授权简化(静态 key),但 **scope 路径过滤是 SQL 层强制**——这是图谱隔离的底线。

---

## 6. Experience Envelope(为什么统一载荷)

### 问题
大多数向量库和碎片化记忆框架强迫开发者同时处理多个 ingest 格式。消息、工具输出、用户观察以完全不同方式存储。碎片化 ingest 导致碎片化检索。

### 方案
单一结构化载荷覆盖所有数据类型。判别联合体(在 `content.kind` 上):
- `message`(对话轮次)
- `text`(自由文本观察)
- `json`(结构化工具输出)
- `blob_ref`(引用已上传 blob)
- `triple`(直接 fact 插入)

### 三个身份槽
- **Caller**:隐式来自 token
- **Observed actor**:谁执行(≠ caller 需 `scope.write.on_behalf_of`)
- **Subject**:关于谁(≠ observed_actor 需 `scope.write.about_other`)

### 强制 idempotency_key
同 key + 同 body = 幂等无操作;同 key + 不同 body = 409。

### 本项目继承
完整继承 envelope 结构。modality 触发不同抽取管线(conversation/document/tool_result/observation/feedback/imported)。

---

## 7. 异步生命周期(为什么写路径无 LLM)

### 问题
标准记忆系统在写路径直接处理抽取。阻塞架构让 agent 等 LLM 生成摘要或嵌入文本。agent 在对话中途冻结,仅仅因为被数据库 ingest 阻塞。

### 方案
完全解耦 ingest 和抽取。async-by-default。SSE 通知应用数据流过管线。

### 六个原子操作(可观测,非独立端点)
Capture → Index → Update → Consolidate → Forget → Compress
(注意:Retrieve 不在其中,它是请求/响应,不是生命周期事件)

### 同步写入选项 `?wait=`
| 值 | 返回时机 | 典型延迟 |
|----|----------|----------|
| (省略) | WAL append | ~5ms (202) |
| `captured` | WAL fsync | ~10ms |
| `indexed` | BM25 + HNSW insert | ~100-500ms |
| `consolidated` | Beliefs/Understanding | ~500-3000ms |

### 本项目继承
完整继承。Postgres-as-queue 的 job 表 + worker 循环实现六操作。SSE lifecycle 事件从 job 状态变化派生。`?wait=` 通过客户端轮询 job 表实现。

---

## 8. 知识图谱(为什么是涌现的)

### 论点
纯向量库把 fact 当孤立文本碎片。向量-only 架构在 agent 问关系问题("谁拥有这个服务"、"这个 incident 前什么变了")时崩溃。单通道方法漏掉尚未文本链接的 fact。

### 方案
知识图谱是**不可变事件日志的涌现属性**。实体图完全从底层事件流派生:
- 实体随新记录观察而涌现
- 关系边随新 predicate ingest 而形成
- 图遍历在 recall 时沿实体边找因果关联

### 图的样子
实体是类型化节点(person/org/service/project/incident/...)。边是从 event 抽取的 predicate,携带与 Facts 相同的双时态有效窗口。

```
(person: Priya Rao) ──[works_at, valid 2024-01→]──→ (org: Acme Corp)
     │
     └─[owns, valid 2026-02→]──→ (project: Q3 Renewal) ─[has_status, "negotiating"]
```

### 图遍历查询示例
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
遍历尊重层级 scope(读不到 `org:acme/dept:sales` 的 agent 永远看不到那里的边)和双时态有效(`as_of` 跳过 `valid_to` 已闭的边)。

### 本项目继承(重点)
- Facts 表同时承担"双时态三元组"和"图边"两个角色
- 图遍历用**递归 CTE**(在 facts 表自连接,`subject_id` ↔ `object_id`)
- CTE 内部强制 scope 过滤和双时态过滤
- Beliefs 的 `supports` 链是另一种图(Belief → Fact → Event),`GET /beliefs/why` 遍历它

### 本项目增强(原版未明示)
**实体链接 B over C**(见 [`01-technical-decisions.md`](01-technical-decisions.md) 第 4 节)。原版文档未明确实体消歧策略,本项目显式设计为分层链接,作为图谱质量的关键投入。

---

## 9. 混合检索(为什么 4 通道)

### 论点
单通道检索把 agent 限制在一种问题上。纯向量把标识符抹平(查具体客户返回 churn 讨论而非字面记录)。纯词法完全漏改写。

### 4 通道(原版)
| 通道 | 工具 | 解决什么 |
|------|------|----------|
| BM25 | Tantivy | 精确词项、标识符、缩写 |
| HNSW 向量 | 自建 | 概念相似、改写 |
| 图遍历 | 原生 BFS | 连接上下文(既不命中词也不命中向量的因果关联) |
| Cross-encoder 重排 | Cohere rerank-v3.5 | top ~25-40 候选按 query-candidate 对精确评分 |

### 融合
**Reciprocal Rank Fusion(RRF, k=60)** 合并各通道排名列表——无需逐通道权重调优,容忍不同通道分数分布差异。重排在融合后 top-k 上做。

### 消融实验贡献(各组件)
- 去 async fact 抽取:**-22.4pp**(最大)
- 去双时态 Facts 层:**-12.8pp**
- 去图遍历:**-6.4pp**
- 去 HNSW:**-7.7pp**
- 去 BM25:**-5.6pp**
- 去 Cohere 重排:-0.2pp

### 本项目继承
4 通道全做,但工具替换:
- BM25:Postgres `tsvector` + GIN 索引(替代 Tantivy)
- 向量:pgvector HNSW(替代自建)
- 图遍历:递归 CTE(替代原生 BFS)
- 重排:OpenAI rerank 端点(替代 Cohere)
- 融合:RRF(k=60,与原版一致)

### 编译常量(原版,本项目可参考)
来自 `cortex-coordinator/src/recall.rs`(原版 Rust 源码):
- `RETRIEVAL_TOP_K` = 40(单会话)/ 160(多会话)
- `RERANK_POOL` = 25 / 40
- `RRF_K` = 60.0
- `GRAPH_WEIGHT` = 0.20
- `GRAPH_RETRIEVAL_MAX_ENTITIES/EDGES/EPISODES` = 48/512/256

本项目这些作为 YAML 可配或代码常量,初版用原版默认值。

---

## 10. 检索管线全貌(原版 recall-tuning 文档)

来自 [`operations/ops_recall-tuning.txt`](../reference/operations/ops_recall-tuning.txt)。这是实现检索层的最详细参考。

```
Query
 ├─► Query routing → question_type (single-session-user/multi-session/...)
 ├─► (可选) HyDE multiquery → N 个假设段落,embed 后作为额外 query 向量
 ├─► (可选) Multihop query planner → M 个后续查询
 ├──► 并行 6 通道:
 │     Vector(HNSW) / Fulltext(BM25+WordNet) / Entity-name(精确+模糊)
 │     Synonym / Graph BFS / Temporal(近因窗+衰减)
 ├──► RRF 融合 → 候选列表
 ├──► (可选) Cross-encoder 重排 top ~25-40
 └──► 组装 StratifiedPack(引用、beliefs、episodes)
```

### 各阶段 p50 延迟(原版默认配置,~100K 事件 scope)
- query embedding ~50ms(必须)
- HyDE multiquery ~250ms(可选)
- multihop planner ~400ms(可选)
- vector+fulltext+KG 并行 ~80ms(必须)
- RRF fusion ~2ms
- cross-encoder rerank ~150ms(可选)
- pack 组装 ~30ms
- **总计:多会话默认 ~900ms;voice profile 单会话 ~180ms**

### 本项目 MVP 检索管线(简化)
MVP 阶段先做核心 4 通道(向量 + 全文 + 图 + rerank),HyDE/multihop/synonym/entity-name/temporal-decay 这些可选阶段后续迭代加。salience 权重也后续。

---

## 11. StratifiedPack(为什么跨层合并)

### 论点
`/v1/recall` 是标准读路径(不是层直读),因为它跨五层合并。单一结构化响应融合"即时原始观察 + 授权的长期概念理解"。

### 结构
```json
{
  "pack_id": "pack_01HX...",
  "layers": {"events":[...],"episodes":[...],"facts":[...],"beliefs":[...],"understanding":[...]},
  "context_block": "...",  // 叙述性上下文块,带 [1][2] 引用标记
  "provenance": {
    "trail": [{"step":"plan","filter":"...","kept":318},...],
    "citations": {"[1]":{"layer":"fact","id":"fact_01HX..."}, ...}
  },
  "diagnostics": {"time_ms":{...}, "policy_attribution":[...]}
}
```

### 本项目继承
完整继承 StratifiedPack 结构。`pack_id` 可被 `/answer` 通过 `use_pack_id` 复用(60s TTL)跳过 recall。

---

## 12. 存储分层(原版)

| 层 | 后端 | 崩溃语义 |
|----|------|----------|
| Events (WAL) | append-only 文件 + checksum chain | 202 返回即 fsync |
| Episodes | RocksDB column family | 可从 WAL 重建 |
| Facts | Typed FactStore on RocksDB | 可从 Events + LLM 抽取重放 |
| Beliefs | Aggregator output on RocksDB | 可从 Facts 重建 |
| Understanding | Concept store on RocksDB | 可从 Beliefs + LLM 重建 |
| Blobs | SHA-256 内容寻址 | 直接文件存储 |

**WAL 是唯一真相源。** 每个派生层都能从它重建——索引损坏是运维烦恼,不是数据丢失。

### 本项目继承(简化)
全部存 Postgres。没有 RocksDB、没有 checksum chain(WAL 用 Postgres `BIGSERIAL` + 事务保证)。派生层仍可从 Events 重建(重跑抽取 job)。Blobs MVP 可跳过或存文件系统。

---

## 13. 性能基准(原版,仅供参考)

### LongMemEval-S:93.8%(469/500)
| 类别 | 分数 |
|------|------|
| single-session-assistant | 100% |
| knowledge-update | 97.4% |
| single-session-user | 95.7% |
| single-session-preference | 93.3% |
| temporal-reasoning | 91.7% |
| multi-session | 90.2% |

### 运行特征
- 写 p50 4ms / p99 12ms / 错误率 0.00%
- 异步抽取完成 p50 18s
- recall p50(holistic 4KB)489ms
- answer p50(Opus 4.6)3.2s

### 本项目不追求复现这些数字
明确不做 benchmark。这些数字仅作为"架构是否合理"的参考——如果我们自己的系统在类似规模下数量级偏离(比如 recall p50 > 5s),说明架构有问题。

---

## 14. 与典型替代方案的差异(原版对比表)

| 维度 | Mem0 | Zep | Pinecone | Neo4j | **CortexDB(本项目继承)** |
|------|------|-----|----------|-------|--------------|
| 写路径 | LLM 摘要+抽取 | 对话存储+sync 抽取 | 向量 upsert | — | **WAL append only** |
| 原始保留 | 否 | 部分 | 否 | — | **是(事件日志)** |
| 派生可重建 | 否 | 部分 | 否 | — | **是(从日志重派生)** |
| 时间建模 | 单时间戳 | 单 | 单 | — | **双时态 4 字段** |
| 抽象层 | 1 层 | Facts only | 1 层 | 图 | **5 层可寻址** |
| 检索 | 单稠密向量 | 稠密+部分词 | 单稠密 | 文本+图 | **4 通道+RRF+重排** |
| 命名空间 | 扁平 | 扁平 | 扁平 | — | **层级 Scopes** |
| 图遍历 | 无 | 无 | 无 | 原生 | **递归 CTE** |

---

## 15. 给实现者的关键提醒

1. **实体链接是图谱质量的命门**。本项目 B over C 第一版就上,不用纯字符串归一糊弄。
2. **图谱隔离靠 scope 路径**。即使授权简化,scope 过滤在 SQL 层强制——否则所有用户的 "Acme" 糊在一个图里。
3. **双时态是时间推理的基础**。不要为了"简化"把 4 时间字段压成 1 个——超替链和 timeline 查询依赖它。
4. **写路径无 LLM**。任何把 LLM 调用放到 `experience` 同步路径的诱惑都要拒绝——那会破坏 <10ms 写入和崩溃恢复。
5. **Facts 表是图谱的核心**。它同时是"双时态三元组存储"和"图遍历的边表",schema 设计要同时服务两个角色。

---

*本文档基于 `docs/reference/` 56 篇原文。如需查阅原始措辞,直接读对应 txt 文件。*
