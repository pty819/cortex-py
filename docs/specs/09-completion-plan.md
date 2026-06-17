# 09 — 补全计划(追平 CortexDB 自称效果)

> **目标**:把 `08-gap-vs-official.md` 列的缺口全部补上,最终跑 LongMemEval-S 量化与 93.8% 的距离。
> **原则**:不破坏已验证的 Stages 0-7 + MCP(每批后跑回归);优先高性价比;能并行并行。
> **不碰**:集群/企业安全/存储调优/多模态(spec 显式 YAGNI,不在本计划)。

## 依赖图 + 执行批次

```
批次 0(独立,全并行,无依赖):
  T27 ?wait= 同步        T28 /beliefs/why      T29 StratifiedPack 子项
  T30 双时态补全          T34 verifier+enrichment

批次 1(依赖批次0的 T29 recall 框架 + T30 时间):
  T31 3 检索通道(并入 RRF)   T32 检索高级阶段(HyDE/multihop/salience/seed/routing)

批次 2(独立,可与批次1并行):
  T33 Understanding 层(新表 + 合成 + related 图)

批次 3(依赖全部就绪):
  T35 LongMemEval-S 基准(量化)
```

## 逐项 PRD + 验收

### T27 — ?wait= 同步写路径
- `POST /experience?wait=captured|indexed|consolidated`
- captured: WAL commit 即返回(~10ms);indexed: 阻塞到 lifecycle kind=indexed;consolidated: 阻塞到 consolidated。超时(consolidated 30s)降级 202+stages_completed。
- 实现:worker 完成阶段 `NOTIFY cortex_stage, <json>`;experience handler `LISTEN` 阻塞到目标 kind 或超时。
- 验收:`wait=indexed` 阻塞直到 extracted/indexed 帧出现,返回 `stages_completed:["captured","extracted","indexed"]` + `elapsed_ms`;无 worker 时超时降级 202。

### T28 — /beliefs/why + /beliefs/build
- `GET /v1/beliefs/why?belief_id=`:遍历 belief.supports→facts→fact.supports→events;组 `support_graph.nodes[{id,type,weight,summary}]` + `edges[{from,to,relation}]`(relation: extracted_from/contains/supported_by);synthesis LLM 渲染 `narrative` + `narrative_model`。
- `POST /v1/beliefs/build {scope}`:手动触发 belief 聚合(复用 extraction.pipeline._aggregate_belief 全 scope 版)。
- 验收:有 supports 的 belief → why 返回 nodes/edges 非空 + narrative 非空(有 key 真 LLM,无 mock);build 后新 belief 出现。

### T29 — StratifiedPack 子项
- RecallRequest 加 `budgets{max_tokens, per_layer_limits}` + `citation_mode` + `exclude_content`。
- knapsack:按 per_layer_limits 硬上限填,再按 token 估算裁到 max_tokens(events 优先裁)。
- citation_mode:inline_with_markers(默认)/none/block_at_end/structured_only。
- exclude_content=true:只返 id+metadata,省 content/text。
- `POST /v1/recall/stream`:SSE 逐层(plan→layer×N→context_block→provenance→diagnostics→done)。
- 验收:max_tokens=500 时 pack 裁剪;stream 各层依次 emit;citation_mode=none 时无 [n] 标记。

### T30 — 双时态补全
- `GET /v1/facts` 加 `include_superseded`(返 recorded_to≤as_of 的历史版本)。
- recall/events 加 `recorded_during{from,to}` 过滤(recorded 轴)。
- as_known 查询:`recorded_from<=t<recorded_to`。
- 验收:as_of+include_superseded 返回超替链全版本;recorded_during 过滤出系统在某时段知道的。

### T31 — 3 检索通道
- **entity-name**:精确(canonical_name/alias 精确匹配 query 实体名)+ 模糊(pg_trgm similarity)。命中的实体→其 facts。
- **synonym**:同义词表(predicate/value 同义扩展,如 own→possess);召回时扩展 query 词项。
- **temporal-decay**:recency 窗(近 N 天)+ 时间衰减权重,与 RRF 分数结合。
- 全部并入 RRF(变 6 通道)。config.retrieval 加开关。
- 验收:查 "Acme"(entity-name)命中 Acme 实体 facts;synonym 表加 own→possess 后查 possess 命中;近事件在 temporal 通道靠前。

### T32 — 检索高级阶段
- **HyDE**:LLM 生成假设回答段落→embed→作额外 query 向量(可配 passages 数)。
- **multihop**:LLM 生成 M 个后续查询,并行检索后并入 RRF。
- **salience**:events.access_count 作 prior 加权(0~0.2)。
- **entity-vector-seed**:query 实体名→其 entity embedding 作额外 query 向量。
- **question-type routing**:规则版(有无 as_of/多实体信号)分类 single/multi,影响 top_k(40/160)+rerank_pool。
- config.retrieval.advanced 开关(默认关,benchmark 时开)。
- 验收:开 HyDE 后改写查询命中原本不命中的;routing 把多 session query 用更大 top_k。

### T33 — Understanding 层
- 新表 `concepts(concept_id, scope, name, topic, version, summary, supports[], related jsonb, confidence, valid_from)`。
- `POST /v1/understanding/synthesize {scope, topics?}` → 202 + job_id;worker job_type=synthesize:每 topic 一次 synthesis LLM 调用→产 concept(name/summary/supports/related/confidence)。
- `GET /v1/understanding?scope=&topic=`;`GET /{id}`;`GET /{id}/related?relation=&depth=`(5 枚举:specializes/generalizes/contrasts/co_occurs/causes);`GET /coverage`(concept_count/by_topic/synthesis_lag)。
- 验收:synthesize 后 concepts 表有行;related 图可遍历;coverage 返回 by_topic。

### T34 — verifier + enrichment
- **verifier**:answer 后(若 config.llm.verifier.enabled + question type 命中)调异家族 LLM 对照 citations 校验幻觉;answer 响应加 `verified`/`verification`。config 已有 verifier 段,接逻辑。
- **enrichment**:worker job_type=enrichment,异步跨 session 实体消歧增强(复用 B over C 灰区但跨 event 批)。config.llm 加 enrichment 段(默认关)。
- 验收:verifier 开 + 有 key → answer 带 verified 字段;enrichment job 入队可跑(mock 亦行)。

### T35 — LongMemEval-S 基准(P0,最后跑)
- 下载 LongMemEval-S(ICLR 2025,500 问 6 类)。
- runner:`scripts/benchmark/run_lme.py`——灌对话(每 session 一个 scope)→ 每 question 调 /answer → GPT-4o judge 对比 gold → 打分 + per-category。
- 先跑子集 50-100 问(省钱/时间),出基线;全量可选。
- 产出 `docs/verification/lme-baseline.md`(分数 + per-category + 与 93.8% 对比)。
- 验收:跑出数字,知道差几 pp。

## 回归门(每批后)

`scripts/run_regression.sh`(新建):stage0(37)+stage6(14)+stage7(31)+mcp(9)+mcp_http(7)+smoke = 全绿才进下一批。

## 工作量估计

| 批次 | 项 | 估计 |
|------|----|------|
| 0 | T27/T28/T29/T30/T34 | 中(5 项,多并行) |
| 1 | T31/T32 | 中-大(检索核心改动) |
| 2 | T33 | 中(新层+合成) |
| 3 | T35 | 中(接基准+judge) |

## 最终目标

补完 → 回归全绿 → 跑 LME-S → 得到"我们 X%,官方 93.8%,差 Ypp"的**事实**。
