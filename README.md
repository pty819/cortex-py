# cortex-py

> CortexDB 记忆系统的 Python 复刻实现 —— 面向个人 / 小团队的 agent 长期记忆层，核心是**知识图谱**（Facts + Beliefs + 图遍历）。
>
> 五层记忆模型（Events / Episodes / Facts / Beliefs / Understanding）、双时态三元组、层级 scope 隔离、6 通道混合检索、MCP 双传输（stdio + 多人 HTTP）。

---

## 这是什么

复刻 [CortexDB](https://cortexdb.ai/docs/) 的核心记忆架构，定位为个人 / 小团队可用，**重点投入知识图谱质量**。不是生产级、不做集群 / 企业安全 / benchmark 复现。

**核心能力**：
- **五层记忆**：Events（WAL）→ Episodes → Facts（双时态三元组）→ Beliefs（概率断言）→ Understanding（概念合成）
- **双时态**：每条派生记录 4 个时间字段，能同时回答"现在什么是真的"和"当时我们怎么以为的"
- **知识图谱**：Facts 当图边，递归 CTE 做 2-3 跳 BFS；Beliefs 带 supports 证据链
- **6 通道混合检索**：向量（pgvector）+ BM25（tsvector）+ 图遍历 + entity-name + synonym + temporal-decay，RRF 融合 + rerank
- **实体链接 B over C**：向量召回 + 阈值 + LLM 灰区判定（图谱质量命门）
- **MCP**：23 个工具，stdio（本地）+ streamable-http（多人共享，按 scope 隔离）
- **完整 API**：experience / recall / answer / forget / erasures / bulk / 5 导入器 / export / 层直读 / lifecycle SSE

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.12 |
| Web | FastAPI + uvicorn |
| DB | PostgreSQL 18.4（pgvector / ltree / pg_trgm / pgcrypto） |
| ORM | SQLAlchemy（原生 `text()` 查询，DDL 在 `src/cortex/schema.sql`） |
| 向量 | pgvector HNSW |
| 队列 | Postgres-as-queue（`SKIP LOCKED`，无 Redis） |
| LLM | OpenAI 兼容接口（默认 Minimax-M3；抽取 / 回答 / 合成 / 校验分路配） |
| Embedding | jina-embeddings-v5（1024 维） |
| Rerank | prism rerank |
| 前端 | Vue 3 + Vite + Pinia + vis-network |
| MCP | FastMCP（stdio + streamable-http） |

## 项目结构

```
cortex-py/
├── src/cortex/
│   ├── config.py          # YAML 配置 + 维度强校验
│   ├── db.py              # engine / session / schema 初始化
│   ├── schema.sql         # 全表 DDL（单一真相源）
│   ├── core.py            # WAL append(幂等) + 队列 + lifecycle + ?wait=
│   ├── services.py        # embedding / rerank / LLM 客户端 + think 剥离
│   ├── extraction/        # 抽取管线 + 实体链接 B over C
│   ├── retrieval/         # 6 通道 + RRF + rerank + StratifiedPack
│   ├── worker/            # Postgres-as-queue worker 循环
│   ├── api/               # FastAPI 全端点
│   ├── ingest.py          # 批量 + 5 导入器
│   ├── export_data.py     # 导出 JSONL
│   ├── erasures.py        # GDPR 引用计数真删（4 阶段）
│   ├── episodes.py        # 事件分段器
│   ├── understanding.py   # 概念合成层
│   ├── maintenance.py     # methylation / consolidation
│   ├── temporal.py        # NL 时间短语解析
│   └── mcp_server.py      # MCP server（23 工具，双传输）
├── frontend/              # Vue3 + Vite 前端
├── scripts/
│   ├── stage0/            # SQL 冒烟套件（37 项验收）
│   ├── verify_stage6.py   # 批量/导入/导出验收（14 项）
│   ├── verify_stage7.py   # erasures/episodes/vocab/演化/时间/admin（31 项）
│   ├── verify_mcp.py      # MCP stdio 验收（9 项）
│   ├── verify_mcp_http.py # MCP HTTP 多 scope 隔离验收（7 项）
│   ├── db_proxy.py        # localhost DB 代理（解 macOS LAN 授权）
│   └── run_regression.sh  # 全量回归
├── docs/specs/            # 设计文档 01-09 + HANDOFF
└── config/config.yaml     # 配置
```

## 快速开始

### 1. 环境准备

```bash
# 安装依赖（uv 管理，Python 3.12）
uv sync
```

### 2. macOS 用户：启动 DB 代理

> macOS 本地网络授权会挡住 uv 管理的 3.12 python 直连局域网 Postgres。
> 用系统 python（有 LAN 权限）跑一个 localhost 透明代理，3.12 连 localhost 即可。

```bash
python3 scripts/db_proxy.py        # 后台跑，监听 127.0.0.1:5433 → <DB>:5432
```

（`config/config.yaml` 的 `database.url` 已指向 `127.0.0.1:5433`。若你的 Postgres 在别处，改 `db_proxy.py` 的 `TARGET` 和 config。）

### 3. 初始化数据库 schema

```bash
uv run python -m cortex.cli db init
```

### 4. 配置 LLM（可选，但推荐）

默认抽取 / 回答走确定性 mock（无 key 也能跑通整条管线）。配真实 key 后切 Minimax：

```bash
export CORTEX_LLM_EXTRACTION_API_KEY=sk-cp-...
export CORTEX_LLM_ANSWER_API_KEY=sk-cp-...
export CORTEX_LLM_SYNTHESIS_API_KEY=sk-cp-...
```

（Minimax-M3 是推理模型，响应带 `<think>` 标签，`services.py` 的 `strip_think()` + `parse_llm_json()` 已处理。）

### 5. 启动后端 + worker

```bash
# 终端 1：FastAPI（用 8002 避开占用）
uv run uvicorn cortex.api.app:app --port 8002

# 终端 2：异步抽取 worker（消费队列）
uv run python -m cortex.cli worker
```

### 6. 启动前端

```bash
cd frontend
npm install        # 首次
npm run dev        # → http://localhost:5173
```

> 前端 Vite 代理默认把 `/v1` 转发到 `http://localhost:8000`。后端用别的端口时，改 `frontend/vite.config.ts` 的 `target`。

打开 http://localhost:5173 即可使用。

## 四个页面

| 页面 | 功能 |
|---|---|
| **/ingest** 入库 | 填 scope / modality / 文本 → `POST /experience` → 右侧 SSE 实时显示 captured→extracted→indexed |
| **/graph** 图谱 | 查 entities + facts → vis-network 渲染节点边；点节点看 timeline 超替链 |
| **/qa** 问答 | 提问 → `POST /answer` → 带引用回答 + 可折叠 StratifiedPack |
| **/browse** 浏览 | Events / Facts / Beliefs 分页列表 |

## MCP（给 agent 注册用）

**stdio**（本地单 agent）：
```bash
uv run python -m cortex.cli mcp
```

**streamable-http**（多人共享）：
```bash
uv run python -m cortex.cli mcp-http --port 8001
# → http://host:8001/mcp，每个 agent 带各自的 X-Cortex-Scope 头（scope 隔离）
```

注册到 Claude Code：项目根 `.mcp.json` 已就绪（自动发现）。详见 `docs/mcp.md`。

## 验收 / 回归

```bash
bash scripts/run_regression.sh     # 全量：stage0(37) + stage6(14) + stage7(31) + mcp(9) + mcp-http(7)
uv run python -m cortex.cli smoke  # 端到端：入库→抽取→检索→回答
```

## 设计文档

| 文档 | 内容 |
|---|---|
| `docs/HANDOFF.md` | 项目交接总览 |
| `docs/specs/01-09` | 技术选型 / 数据模型 / 缺口闭合 / 实施PRD / Stage7 PRD / 与官方差距 / 补全计划 |
| `docs/mcp.md` | MCP 完整指南 |
| `docs/INGEST-BEST-PRACTICES.md` | 知识入库最佳实践——怎么写数据才能被正确连接和召回（给前置 agent） |
| `docs/GUIDE-INGEST.md` | 喂入操作手册——HTTP 端点、字段、curl 模板 |
| `docs/USAGE-GUIDE.md` | 系统使用说明——5 层架构、API 全貌 |
| `docs/TEMPLATE-DIAGNOSIS.md` | 诊断事件字段级样板 |

## 与 CortexDB 官方的差距

详见 `docs/specs/08-gap-vs-official.md`。一句话：**架构地基命中官方消融表全部高价值项**（Facts -22.4pp / Bi-temporal -12.8pp / Graph -6.4pp / HNSW -7.7pp / BM25 -5.6pp —— 全有），缺的是把 ~80% 推到 93.8% 的打磨层（HyDE / multihop / salience 已实现默认关）+ 零基准测量。MCP 我们超过官方（23 工具 + 多人 HTTP vs 16 stdio）。

## 许可

个人项目。
