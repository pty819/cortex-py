# 08 — 与 CortexDB 官方宣称的逐模块差距分析

> **方法**:逐模块对照 `docs/reference/` 原文(2 个 Explore agent 抽取全部精确宣称)+ 我们实际代码(grep 路由/能力确认)。
> **基准日**:2026-06-18。诚实量化,不夸大不缩水。
> 官方文档自身存在内部矛盾(per-category 分数、reranker 是否含、抽取模型),已标注。

---

## 0. 一句话结论

**架构地基命中了官方消融表里全部 5 个高价值驱动项(Facts -22.4pp / Bi-temporal -12.8pp / Graph -6.4pp / HNSW -7.7pp / BM25 -5.6pp——我们全有),所以理论上限在正确量级;但缺了把 ~80% 推到 93.8% 的"打磨层"(HyDE/multihop/salience/额外通道 + Opus 级 answer 模型 + Understanding 层),且——关键——零基准测量,质量未经量化。** 安全/集群/存储调优/多模态按 spec 显式 YAGNI 跳过。

## 1. 总览:命中率

| 维度 | 官方宣称 | 我们 | 状态 |
|---|---|---|---|
| 五层记忆 | Events/Episodes/Facts/Beliefs/Understanding | Events/Episodes/Facts/Beliefs/(Understanding 缺) | **4/5** |
| 双时态 | 4 字段 + 4 查询模式 | 4 字段 + as_of/valid_during/timeline(as_known/include_superseded 缺) | **核心有,查询模式 2/4** |
| 混合检索通道 | 6 通道 | 3 通道(vector/BM25/graph)+ rerank | **3/6 通道** |
| 检索高级阶段 | HyDE/multihop/salience/entity-vector-seed/query-routing | 全无 | **0/5** |
| RRF 融合 | k=60 | k=60 | ✅ |
| StratifiedPack | layers/context_block/provenance/diagnostics/budgets/citation_mode/stream | layers/context_block/provenance/diagnostics(budgets/citation_mode/stream 缺) | **~60%** |
| ?wait= 同步 | captured/indexed/consolidated 阻塞 | 全无(只 202) | **0/3** |
| LLM 调用点 | 4 站(extract/answer/verifier/enrichment),各异模型 | 1 模型(Minimax-M3)通吃,verifier 关,无 enrichment | **1/4 站,质量未测** |
| Benchmark | 93.8% LME-S / 86.9% LoCoMo + 消融表 | **0 测量** | **未量化** |
| 安全/集群/存储调优 | PASETO+4层/集群/HNSW调优/加密/备份/多模态 | 静态key/单机/pgvector默认/无/无/无 | **显式 YAGNI 跳过** |
| MCP | 16 tools(stdio) | 23 tools(stdio + streamable-http 多人) | **超过**(我们更全) |

---

## 2. 逐模块详查

### 2.1 混合检索 —— 差距最实质的功能区

**官方**(recall-tuning + benchmark 消融):
- **6 通道**:Vector(HNSW)/ Fulltext(BM25+WordNet)/ Entity-name(精确+模糊)/ Synonym / Graph BFS / Temporal(recency+decay)
- 编译常量:top_k=40(单)/160(多)、rerank_pool=25/40、RRF_k=60、graph_weight=0.20、max_entities=48/edges=512/episodes=256
- 可选阶段:HyDE(~250ms,-1~2pp)、multihop planner(~400ms,-1~3pp)、salience(weight 0.10)、entity-vector-seed、query routing
- 延迟预算:默认多 session ~900ms,voice 单 session ~180ms
- 消融:去 graph -6.4、去 HNSW -7.7、去 BM25 -5.6、去 Cohere rerank -0.2

**我们**:
- **3 通道**:pgvector 向量 / tsvector BM25 / 递归 CTE 图 + prism rerank + RRF(k=60 ✓)
- 无 HyDE / multihop / salience / entity-name / synonym / temporal-decay / query-routing
- top_k=40 ✓(单),无单/多 session 区分;无 rerank_pool 上限概念(直接 rerank 全部)

**差距 → 影响量级**(用官方消融外推):
- 缺 HyDE:复杂查询 -1~2pp(改写不匹配场景)
- 缺 multihop:多 session -1~3pp
- 缺 entity-name 通道:标识符/缩写精确命中弱(无量化,估计 -1~2pp)
- 缺 synonym + WordNet:同义改写弱(无量化)
- 缺 salience:"最近相关"类查询无近因加权(小,-0~1pp)
- **累计保守估计**:相对官方全管线,我们检索召回质量约 **-4 ~ -8pp**(在多 session / 改写重的查询上)。单 session 简单查询差距更小(那些阶段默认就不开)。
- 注:graph/vector/BM25/rerank/RRF 这些**高价值项我们都有**(消融 -5.6~-7.7pp 那几个),所以底子在。

### 2.2 LLM 调用点 —— 模型质量未测是最大未知

**官方**(llm-answer):
- 4 站,各配不同模型:抽取 `gpt-4o-mini`(便宜快)/ answer `claude-opus-4.6`(强)/ verifier `gpt-4.1`(异家族)/ enrichment(可选 `gpt-4o`)
- answer 模型 A/B(vs Opus 4.6):Sonnet -2pp、gpt-4o -3pp、gpt-4o-mini -8pp、gemini-2.0-flash -5pp
- 93.8% 用 Opus 4.6 答题 + (矛盾点)gpt-4o-mini 或 Opus 抽取

**我们**:
- **1 个模型(Minimax-M3)通吃** extraction/answer/synthesis;verifier 配置在但**关**;无 enrichment
- Minimax-M3 是推理模型(带 think),质量**完全未基准**

**差距 → 影响**:
- answer 用 Minimax-M3 而非 Opus 4.6 → **未知,可能 -3~-8pp 量级**(类比 gpt-4o -3 / gpt-4o-mini -8,但 Minimax-M3 未测)
- 抽取质量直接决定图谱质量(消融:去 Facts 层 -22.4pp)——我们的 mock 抽取是规则版,真 Minimax 抽取质量未量化
- 这块是**质量天花板的最大变量**:换 answer 模型 + 跑 benchmark 能立刻知道差多远

### 2.3 StratifiedPack / recall 响应

**官方**:`layers` + `context_block`(LLM 综述带 [n]) + `provenance.trail`(每步 kept) + `provenance.citations`(marker→{layer,id}) + `diagnostics.time_ms` + `budgets.max_tokens`(knapsack) + `per_layer_limits` + `citation_mode`(4 档) + `exclude_content` + `/v1/recall/stream`(逐层 SSE)

**我们**:layers ✓ + context_block ✓(真 synthesis) + provenance.trail ✓ + citations ✓ + diagnostics.time_ms ✓;**缺**:budgets knapsack、per_layer_limits、citation_mode、exclude_content、/recall/stream

**差距**:功能层 ~60%。knapsack 在大 pack 时会爆 token;无 stream 意味着 agent 不能"边来边用"。**影响:中等**(小规模无感,规模上来才痛)。

### 2.4 双时态 / 时间查询

**官方**:4 字段 + 4 模式(Now/As-of/As-known/History)+ temporal 块(as_of/valid_during/recorded_during/natural/reference_date)+ include_superseded + 自定义短语(expression=`+P6M..+P9M` 相对 anchor)+ 内置默认(today/yesterday/last week/this quarter)

**我们**:4 字段 ✓ + as_of ✓ + valid_during ✓ + timeline ✓ + temporal-phrases 注册/解析 ✓(expression=`-P7D..P0D`)+ 默认短语 ✓;**缺**:as_known 查询模式、recorded_during、include_superseded、reference_date 默认

**差距**:核心时间推理有(Now/As-of/History),As-known("当时我们怎么以为的")缺。**影响:小**(As-known 是审计向,日常 agent 用得少)。

### 2.5 Beliefs / 图谱

**官方**:`/beliefs` + **`/beliefs/why`**(support_graph: nodes{weight,summary}+edges{relation:extracted_from/contains/supported_by}+ LLM narrative,模型 claude-haiku-4-5)+ `/beliefs/build` + stance 5 枚举 + confidence_interval

**我们**:`/beliefs`(list)✓ + stance ✓ + confidence_interval ✓ + supports ✓;**缺:`/beliefs/why`(support_graph + narrative)、`/beliefs/build`**

**差距**:`why` 是图谱"可解释性"卖点,我们没建。**影响:中**(agent 问"你为什么这么想"答不了,但 list 能给原始 belief)。

### 2.6 Episodes

**官方**:async 由 consolidator 建 + 手动 build + title(LLM) + overlapping 过滤 + with_causal_chain + causal relation `follows`

**我们**:手动 build ✓ + 30min 时间窗 + causal_chain(preceded_by → `precedes`)✓ + actors ✓;**缺**:LLM title、overlapping 过滤、async consolidator 调度(只手动)、`follows` relation(我们用 `precedes`)

**差距**:基本功能有,缺自动化调度和元数据。**影响:小-中**。

### 2.7 Memory evolution

**官方**:**注意——feature 文档 `f_memory-evolution.txt` 只写了 KG 演化(uses→previously_used 转换、adaptive types、contradiction 检测),没写 methylation 调参**;methylation 的旋钮在 recall-tuning ops 文档(168h inactivity / min_access 10 / util_ratio 0.30 / consolidation min_age 24h / max_surprise 0.5 / scheduler 间隔)+ V2 "sleep" 阶段

**我们**:methylation(简单:access_count=0 + age)✓ + consolidation(同三元组去重)✓;**缺**:utility-ratio、surprise score、uses→previously_used 谓词转换、adaptive entity types、contradiction 检测、scheduler 周期化、V2 sleep

**差距**:我们有"骨架"(会剪枝/会去重),缺"智能判定"(util ratio/surprise)。**影响:中**(长期图谱膨胀治理,短期无感)。

### 2.8 Erasures / Forget

**官方**:`/forget`(derived_only/redact_events,cascade 语义)+ `/erasures`(4 阶段:enumerate→refcount→delete→cleanup,preview/manifest 24h,跨 workspace+legal_hold)

**我们**:forget(derived_only 软关 recorded_to + redact_events)✓ + erasures 4 阶段 ✓(preview/manifest/execute + array_remove + blob refcount);**缺**:cross_workspace、legal_hold、manifest stale 409 完整校验、preview 的 estimated_duration

**差距**:核心删除链路完整,缺企业级边角。**影响:小**(单机个人用不到 cross-workspace)。

### 2.9 Understanding 层 —— 整层缺

**官方**:LLM 概念合成(per-topic,claude-opus-4.6,每 scope 几十~几百次 completion)+ related 图(specializes/generalizes/contrasts/co_occurs/causes)+ coverage + `/synthesize`(202 async)

**我们**:**完全未实现**(spec 显式 MVP 跳过/最简版,实际 0)

**差距**:整层。**影响**:这是五层里"长程概览"用,benchmark 里 Understanding 贡献未单列消融(论文说"basic" shipped),所以**对 LME-S 数字影响不明但非最大项**。对"给我讲讲这个 scope 的全貌"类查询是硬缺。

### 2.10 配置 / Profiles

**官方**:TOML + ~100 env + 14 节 + 启动校验 + **7 套 profile**(Benchmark/Max-Recall/Voice/Batch/Cost/Enterprise/Self-host)+ `/admin/config`

**我们**:YAML + env 覆盖 + dim 校验;**1 套固定配置**,无 profile 切换

**差距**:功能够跑,无场景化预设。**影响:小**(手动改 YAML 能达到同样效果,只是没有"一键 Voice 模式")。

### 2.11 存储 / 引擎 / 集群

**官方**:RocksDB WAL(checksum chain)+ HNSW(M/ef_construction/ef_search/quantization 可调)+ block_cache + replication_factor + 一致性哈希集群 + blob(local/s3/gcs/azure)+ checkpoint 备份 + 内容模态处理器(image gpt-4o / audio whisper / video ffmpeg)

**我们**:Postgres BIGSERIAL WAL + pgvector HNSW(**默认参数,未暴露调优**)+ inline blob + 单机 + 无备份 + 无模态处理器

**差距**:大,但**全是 spec 显式 YAGNI**(不做集群/企业/复现 benchmark)。HNSW 默认参数对 10K-100K 事件够用。**影响:小**(个人/小团队规模),规模上来或要调优时是债。

### 2.12 安全 / 合规

**官方**:~80 字段——PASETO v4 + 4 层能力栈 + policy/effective + 审计链(SHA-256) + 加密 at rest + TLS/mTLS + RBAC/OIDC + rate limit + breach detection + data residency + consent + classification + DSAR + SIEM + 备份

**我们**:静态 API key + scope 路径隔离。**零企业安全**。

**差距**:完全缺,但**spec 显式 YAGNI**。**影响:无**(定位个人/小团队,不做企业)。

### 2.13 Benchmark 复现 —— 最大的"未量化"

**官方**:LongMemEval-S 93.8%(469/500)+ LoCoMo 86.9% + 消融表 + 复现命令 + 硬件无关性 + 方差 ±0.31pp(6 次复测)

**我们**:**0**。从未在任何标准基准上跑过。

**差距**:不是"差几 pp",是**根本不知道差几 pp**。这是回答"离它们自称的效果差多远"的核心未知数。

---

## 3. 我们超过官方的地方(诚实标注)

1. **MCP**:官方 16 tools(stdio);我们 23 tools + **streamable-http 多人共享 + per-user scope 隔离**(官方 stdio 单人)。
2. **实体链接 B over C**:官方文档**未描述**链接策略(只说"别名解析");我们显式设计了向量召回+阈值+LLM 灰区判定——这是图谱质量命门,我们比官方文档更明确。
3. **递归 CTE 图遍历**:官方自建原生 BFS(Rust);我们用标准 SQL 递归 CTE——不是"超过",是**更简单等价**(实测 2-3 跳毫秒级)。

## 4. 官方文档自身的矛盾(对标时要注意)

agent 抽取发现官方文档内部不一致,意味着"它们自称的"本身不 fully coherent:
1. **LME-S per-category 分数**:研究论文(56/56=100% 等)≠ ops 页(91/100=91% 等),两套对不上同一个 469/500。
2. **LME-S 跑一次成本**:论文 $49.69(含 Opus 抽取 $18.42)≠ ops 页 ~$6(gpt-4o-mini 抽取)。
3. **93.8% 是否含 Cohere rerank**:benchmark profile 说用编译默认(reranker 默认关),但论文把 Cohere 列为组件并消融(-0.2pp)。
4. **抽取模型**:架构/论文说 Opus 4.6 抽取,ops llm-answer 说默认 gpt-4o-mini。

所以"官方宣称的 93.8%"本身配置描述有出入——复现时以实际跑通为准。

## 5. 优先级建议(若要追平)

按 **性价比 = 影响pp / 工作量**:

| 优先级 | 补什么 | 预期收益 | 工作量 |
|---|---|---|---|
| **P0** | 跑一次 LongMemEval-S(哪怕子集) | **量化当前差距**(消除最大未知) | 中(接基准数据集+judge) |
| **P1** | answer 换 Opus 级模型(或测 Minimax 真实水平) | 可能 +3~8pp | 小(改 config) |
| **P1** | 补 HyDE + multihop | +2~5pp(多 session) | 中 |
| **P2** | 补 /beliefs/why(support_graph + narrative) | 可解释性 | 中 |
| **P2** | 补 StratifiedPack budgets knapsack + /recall/stream | 规模化 | 中 |
| **P2** | 补 ?wait= 同步模式 | API 完整性 | 小 |
| **P3** | Understanding 层 | 长程概览 | 大 |
| **P3** | memory evolution 智能判定(util/surprise) | 长期治理 | 中 |
| **跳过** | 集群/企业安全/存储调优/多模态 | spec YAGNI | — |

## 6. 最终判定

**离"自称效果"(93.8%)的距离 = 一个未知数 + 一个可估计的下界**:
- **未知数**:零基准 → 不知道现在多少分。
- **下界估计**:我们命中全部高价值消融项(底子在),但缺 ~5 个检索打磨阶段 + 用未测的 Minimax 答题 + 无 Understanding 层。**乐观**:单 session 简单查询可能 85-90%(那些高级阶段默认不开);**保守**:多 session 改写重查询可能 75-85%。
- **追平路径清晰**:P0 跑基准 → P1 换模型+补 HyDE/multihop → 复测。估计 2 个迭代周期能从"未知"到"知道差几 pp"。

**架构层面我们不落后**(设计同源、高价值项全有);**工程打磨和量化验证**是真实差距。
