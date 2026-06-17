# 06 — 实施总 PRD(Stage 1→7 + 前端)

> **定位**:Stage 0 已绿(37 PASS)。本文是 Stage 1 起所有实现阶段的**执行 PRD + 验收标准**。
> 每个阶段的验收标准都是**可执行测试/可观测行为**——不是"代码写完",而是"跑通且证据可示"。
> 依据:`01` 选型 / `03` 数据模型 / `05` 缺口闭合(修订路线图 Stage 0→7)。
> 服务现状(2026-06-18 实测):Postgres 18.4 ✓、embedding(jina-v5 1024d)✓、rerank(prism)✓、LLM(Minimax)端点可达待 key。

---

## 全局技术约束

- **语言/框架**:Python ≥3.12、FastAPI、SQLModel(+ SQLAlchemy)、asyncpg/psycopg[binary]、openai SDK、pydantic v2、pytest。
- **单库**:一切在 Postgres `cortex` schema(stage0 用 `cortex_stage0`,**实现用 `cortex`**)。
- **配置**:`config/config.yaml`(YAML),三路 LLM + rerank + embedding 分段;启动**强制校验** `embedding.dimension == vector(1024)`。
- **写路径无 LLM**:experience 仅 append WAL + enqueue,抽取全异步。
- **scope 强制**:所有读写带 scope;SQL 层 `WHERE scope`。
- **无 key 兜底**:抽取/answer 在缺 LLM key 时走确定性 mock(保证管线可验证),key 入 config 即切真。

## 服务连接(实测)

| 服务 | 地址 | key | 状态 |
|------|------|-----|------|
| Postgres | `192.168.1.21:5432/postgres` | (conn str) | ✓ |
| Embedding/Rerank | `http://192.168.1.238:8000/v1` | `local` | ✓ |
| LLM(Minimax) | `https://api.minimaxi.com/v1` | `sk-cp-...`(待用户提供) | 端点可达,待 key |

---

## Stage 1 — 基座(config / db / models / blobs)

**交付**:`src/cortex/{config.py, db.py, models/, blobs/}`、`config/config.yaml`、`pyproject.toml` 完整依赖。

**验收(可执行)**:
1. `uv sync` 成功装齐依赖(fastapi/sqlmodel/openai/pydantic/pytest/httpx/psycopg...)。
2. `python -c "from cortex.config import load_config; load_config()"` 加载 YAML;维度不一致时(改 1024→999 测)**抛异常拒启动**。
3. `python -m cortex.db init` 建 `cortex` schema(复用 stage0 DDL,落 `cortex` 而非 `cortex_stage0`)。
4. blobs 读写单测:`put_blob(bytes)` → 同 bytes 二次 `put` 返回同 blob_id(refcount 不变)。

## Stage 2 — 写路径 + 队列 + SSE

**交付**:`src/cortex/{wal/, worker/, lifecycle/}`、`api/experience`、`api/lifecycle/stream`。

**验收**:
1. `POST /v1/experience`(scope+modality+content+idempotency_key)→ 202 + event_id + wal_offset;同 key 二次 200 幂等;同 key 异 body → 409。
2. 落库后自动 enqueue 一个 `extract` job(jobs 表 status=queued)。
3. worker 循环跑 `claim_next_job` → 抢到 job → 跑(初期跑一个 echo handler)→ completed。
4. visibility timeout:把 running job 的 locked_at 改 6min 前,reaper 重置为 queued。
5. `GET /v1/lifecycle/stream?event_id=` SSE 推 `captured`→`extracted` 帧。

## Stage 3 — 抽取 + 实体链接 + 派生层

**交付**:`src/cortex/extraction/`、`linking/`(B over C)、`vocab/`、`episodes/`、`beliefs/`。

**验收**:
1. **R1 探针**(`scripts/probe_llm_schema.py`):10 行脚本测 Minimax-M3 对 `response_format=json_schema` 的响应;有 key 时验证,无 key 时文档化 fallback 链。
2. 抽取 handler 喂一段 `"Priya works at Acme and owns the Q3 renewal"` → 产出 facts(subject/predicate/object)+ entities;mock 抽取器先跑通(确定性),真 LLM 待 key。
3. 实体链接:提到 "Bob" 且 aliases 表有 → A 层直接命中,不调 LLM(单测断言)。
4. vocabularies coerce:`vocab_coerce(scope,'deal_stage','签约')`→`signed`(单测)。
5. segmenter:连续 events 超 30min 间隔 → 封存 episode(sealed=true)。
6. beliefs 聚合:同实体多 fact → 产 belief(带 supports 链)。

## Stage 4 — 检索 + StratifiedPack

**交付**:`src/cortex/retrieval/`(tsvector/pgvector/graph/RRF/rerank/pack)。

**验收(用真实 embedding+rerank)**:
1. 灌入 10 条各异 events(经抽取落 facts/embedding)。
2. `recall(scope, query="who owns the Q3 renewal")` → top 结果含 Q3 相关 fact。
3. 四通道各自返回候选;RRF 融合后顺序合理;rerank 后相关项上浮(rerank 分数 >0.3)。
4. StratifiedPack 含 `layers.{facts,beliefs,events}` + `provenance.trail`(各步 kept 数)+ `context_block`(synthesis LLM 或 mock 文本)。
5. 计时:单 scope recall p50 < 1s(种子规模)。

## Stage 5 — API 端点 + answer

**交付**:`src/cortex/api/` 全端点。

**验收**:
1. 静态 key 中间件:无/错 key → 401;对 key → 注入 scope 强制。
2. `POST /v1/recall` 返回 StratifiedPack;`view=structured` 只返 facts+beliefs。
3. `POST /v1/answer` = recall + answer LLM(有 key 真,无 key mock)→ 带引用回答 + `model_used`。
4. `POST /v1/forget` derived_only:命中 fact 的 recorded_to 闭合,timeline 仍可查。
5. 层直读:`GET /v1/facts/timeline?subject=&predicate=` 返回超替链;`GET /v1/beliefs/why` 返回支持图。
6. pytest:≥20 个端点契约测试(httpx+TestClient)。

## Stage 6 — 批量 / 导入 / 导出

**交付**:`/experience/bulk`、importers(jsonl/mem0/zep/letta/openai)、`/export`。

**验收**:bulk 灌 50 条 → import_jobs.accepted=50,逐条落 events+enqueue;jsonl importer 用 scope_template 填 scope;export 某 scope → JSONL 可回灌。

## Stage 7 — C 档(按需,登记)

memory evolution(methylation/consolidation)、temporal-phrases NL、admin/metrics、HyDE/multihop/salience、question-type LLM 路由。**MVP 不阻塞端到端**,时间允许则做 methylation 调度 + /healthz。

---

## 前端 — Vue3 + Vite 知识工作台

**技术**:Vue 3(`<script setup>` + Composition API)、Vite、Pinia、Vue Router、`@vue-flow/core`(图谱可视化)或 vis-network、原生 EventSource(SSE)。UI:Element Plus 或 Naive UI(自选)。后端 FastAPI 同源代理(Vite dev proxy)。

**页面/功能**:
1. **入库页**:表单(scope 下拉、modality、content textarea、可选 blob 上传)→ `POST /experience`;右侧 SSE 实时流(`captured/extracted/indexed`)。
2. **图谱页**:查 entities/facts → 节点(实体)+ 边(predicate,带 valid 窗口);支持 scope 过滤、点击节点看 timeline。
3. **问答页**:query 输入 → `POST /answer` → 展示回答 + 折叠的 StratifiedPack(layers/provenance/citations)。
4. **浏览页**:层直读(events/facts/beliefs 列表 + 分页)。

**验收**:浏览器入库 `"Priya owns Q3 renewal at Acme"` → 看抽取的三元组出现在图谱 → 问 "who owns Q3 renewal" → 看带引用回答。

---

## subagent 分发边界(并行安全)

| Agent | 模块 | 依赖 |
|-------|------|------|
| 基座(自写) | config/db/models/blobs | — |
| A:检索 | `retrieval/` | models(只读 schema) |
| B:前端 | `frontend/`(独立目录) | 后端 OpenAPI(契约) |
| 抽取/API/worker | `extraction/linking/api/worker` | 基座 |

基座先行;检索与前端可并行(前端先按契约 mock,后联调)。

## 验收循环

每阶段:实现 → 跑验收测试 → FAIL 修 → 复跑(≤3 轮)→ PASS 进下一阶段。关键证据存 `docs/verification/stageN.md`。

## 已知风险(继承 05 §9)

R1(LLM json_schema)→ Stage 3 探针验;R2(rerank 分数)→ Stage 4 实测已明朗(relevance 0-1,threshold 0.1 合理);R3(别名召回质量)→ Stage 3 真抽取验;R4(blobs 大文件)→ inline 先行;R5/R6 → 对应阶段。
