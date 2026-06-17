# cortex MCP server

cortex 把全部能力暴露为一个 **MCP (Model Context Protocol) server**,供 Claude Code / Cursor / Windsurf / VS Code Copilot 等 MCP 兼容 agent 注册使用。

## 架构

进程内实现:`mcp_server.py` 直接调用 `cortex.*` 函数(不走 HTTP),`memory_store` **同步抽取**(agent 存完立即可搜)。

**两种传输**:
- **stdio**(`cortex mcp`)— 本地单 agent 注册(每 agent 一个子进程)。
- **streamable-http**(`cortex mcp-http`)— **多人共享**:一个中心服务,多 agent 网络连。多用户按 `X-Cortex-Scope` 请求头隔离(每个 agent 配自己的 scope);`config.api.key` 非空时需 `Authorization: Bearer <key>`。

```
单机:    agent ─stdio JSON-RPC→ cortex.mcp_server ─DB(via 代理)→ Postgres
多人:    agentA ─┐
         agentB ─┼─HTTP /mcp(带各自 X-Cortex-Scope 头)→ cortex mcp-http ─DB→ Postgres
         agentC ─┘   (scope 隔离:看不到别人的记忆)
```

## 前置(重要)

1. **DB 代理必须先跑**(macOS 本地网络授权挡住 3.12 直连 .21):
   ```bash
   python3 scripts/db_proxy.py        # 系统 python(有 LAN 权限),监听 127.0.0.1:5433 → 192.168.1.21:5432
   ```
2. (可选,真实抽取/回答用)`mcp_server` 会按 config 调 LLM;配 key 走真实 Minimax,否则 mock:
   ```bash
   export CORTEX_LLM_EXTRACTION_API_KEY=sk-cp-...      # + ANSWER / SYNTHESIS
   ```
3. 建 schema(一次):`uv run python -m cortex.cli db init`

## 注册

### Claude Code(推荐)
项目根已有 `.mcp.json`,Claude Code 打开本项目会自动发现。或手动:
```bash
claude mcp add cortex -e CORTEX_SCOPE=org:acme/dept:sales/user:alice \
    -- uv run --directory /path/to/cortex-py python -m cortex.cli mcp
```

### Cursor / Windsurf / VS Code Copilot
编辑各自的 mcp 配置,server 块同 `.mcp.json`:
```json
{ "mcpServers": { "cortex": {
    "command": "uv",
    "args": ["run", "--directory", "/path/to/cortex-py", "python", "-m", "cortex.cli", "mcp"],
    "env": { "CORTEX_SCOPE": "org:acme/dept:sales/user:alice" }
}}}
```

### 多人共享(streamable-http,推荐给团队)
服务端起一个中心 MCP HTTP 服务:
```bash
python3 scripts/db_proxy.py &                 # 1. DB 代理
uv run python -m cortex.cli mcp-http --port 8001   # 2. MCP HTTP server(可配 config.api.key 鉴权)
# → http://<host>:8001/mcp
```
每个用户的 agent 用 **URL + 自己的 `X-Cortex-Scope` 头** 连接(scope 隔离,互不可见):
```json
{ "mcpServers": { "cortex": {
    "url": "http://cortex-host:8001/mcp",
    "headers": {
      "X-Cortex-Scope": "org:acme/dept:sales/user:bob"
    }
}}}
```
若服务端配了 `api.key`,再加 `"Authorization": "Bearer <key>"`。
> Claude Code 支持 `url` 型 server(`claude mcp add --transport http cortex http://host:8001/mcp -H 'X-Cortex-Scope: org:acme/user:bob'`)。

### 环境变量
| 变量 | 默认 | 作用 |
|------|------|------|
| `CORTEX_SCOPE` | `org:local/user:default` | stdio 模式工具的默认 scope(HTTP 模式优先用 `X-Cortex-Scope` 头) |

## 暴露的 23 个工具

**记忆核心**
- `health_check()` — DB 可达性 + 行数。
- `memory_store(text, scope?, modality?)` — 存记忆 + **同步抽取**(存完立即可搜)。
- `memory_search(query, scope?, view?, top_k?)` — 混合检索(向量+全文+图+RRF+rerank)→ facts/beliefs/context_block。
- `answer(query, scope?)` — 检索 + LLM 带 [n] 引用回答。
- `get_context(scope?, query?)` — holistic 视图("回应前该知道什么")。

**列表 / 详情**
- `memory_list(scope?, limit?)` / `memory_get(event_id)` — WAL 原始事件。
- `entity_list(scope?, q?)` — 图节点。
- `entity_edges(entity_id, scope?)` — 某实体的全部出边(按谓词)。
- `facts_timeline(subject, predicate, scope?)` — 双时态超替链(值演变史)。
- `list_beliefs(scope?, about?)` — 概率断言 + 证据链。

**批量 / 遗忘 / 擦除**
- `bulk_ingest(texts[], scope?, modality?)` — 批量存(异步抽取)。
- `memory_forget(predicate?, about_entity?, scope?)` — 软忘(闭合 recorded_to,保 WAL)。
- `erasure_preview(...)` / `erasure_execute(...)` — GDPR 真删(引用计数:redact vs delete)。

**结构 / 演化 / 时间**
- `episodes_build(scope?)` / `episodes_list(scope?)` — 事件分段(30min 窗 + 因果链)。
- `vocab_list(scope?)` / `vocab_create(name, kind, values)` — 受控词表(归一抽取值)。
- `temporal_list()` / `temporal_register(name, expression)` — NL 时间短语(如 `last week`=`-P7D..P0D`)。
- `admin_metrics(scope?)` — 行数 + 队列状态。
- `export_scope(scope)` — 导出 JSONL。

## 验收

```bash
python3 scripts/db_proxy.py &                       # 1. 代理
uv run python scripts/verify_mcp.py                 # 2. stdio(stdio JSON-RPC 驱动,9 项)
uv run python scripts/verify_mcp_http.py            # 3. streamable-http + 多 scope 隔离(7 项)
```
stdio 覆盖:initialize/tools-list/health/store-sync-extract/search/answer/entity_list。
HTTP 覆盖:server 起、tools/list(23)、scopeA 存+搜命中、**scopeB 隔离(0)**、显式 scope arg 覆盖头。

## agent 典型用法

```
agent → memory_store("Priya owns Q3 Renewal, signed the deal last week")
       → {event_id, facts_extracted:3}     # 立即入图
agent → memory_search("who owns Q3 Renewal")
       → {facts:[Priya owns Q3 Renewal,...], context_block:"...[1]..."}
agent → answer("is the Q3 deal signed?")
       → "Yes — Priya signed Q3 Renewal [1]."
```
