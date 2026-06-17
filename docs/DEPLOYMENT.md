# cortex-py 部署文档（面向 agent 自动执行）

> 本文档供部署 agent 逐步执行。每步含**执行指令** + **验证方法** + **失败排错**。
> 按顺序执行，每步验证通过再进下一步。遇到 ❌ 按对应「排错」处理。
>
> 目标：在一台机器上把 cortex-py 完整跑起来（后端 API + 异步 worker + 前端网页），可入库 / 看图谱 / 问答。

---

## 0. 前置条件（先核对，缺什么先装）

| 依赖 | 要求 | 检查命令 |
|---|---|---|
| 操作系统 | macOS / Linux（本文以 macOS 为主，Linux 见附录 A） | `uname -a` |
| Python | **3.12**（uv 会管理，但系统要有可用的 3.12） | `uv python list 2>/dev/null \| grep 3.12` |
| uv | ≥0.11 | `uv --version` |
| Node.js | ≥18（实测 v24） | `node --version` |
| npm | 随 Node | `npm --version` |
| PostgreSQL | 18.4 + 扩展（vector/ltree/pg_trgm/pgcrypto/unaccent/uuid-ossp） | 见步骤 2 |
| 网络 | 能访问 Postgres(5432)、embedding/rerank(8000)、LLM(api.minimaxi.com) | 见步骤 1 |

若 `uv` 没装：
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## 1. 拉取代码

```bash
git clone https://github.com/pty819/cortex-py.git
cd cortex-py
```

**验证**：`ls docs/specs/01-technical-decisions.md src/cortex/schema.sql frontend/package.json` 应都存在。

❌ 排错：clone 失败 → 检查网络 / GitHub 可达性。

---

## 2. 确认 PostgreSQL 与扩展

cortex 用一个 Postgres 实例 + `cortex` schema。本项目实测环境：

```
host: 192.168.1.21:5432
db:   postgres
user: postgres
pass: 0prV2JrQ1uJSBHZ2
```

**必装扩展**（在目标 Postgres 上，superuser 执行一次）：
```sql
CREATE EXTENSION IF NOT EXISTS vector;       -- pgvector(0.8.2+)
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
```

**验证扩展就绪**：
```bash
psql "postgresql://postgres:0prV2JrQ1uJSBHZ2@192.168.1.21:5432/postgres" -c "
  SELECT name FROM pg_available_extensions
  WHERE name IN ('vector','ltree','pg_trgm','pgcrypto','unaccent','uuid-ossp') AND installed_version IS NOT NULL;"
```
应返回 6 行。

❌ 排错：
- `vector` 不可用 → 装 pgvector：`apt install postgresql-<ver>-pgvector`（Debian）或从源码编译。
- 连不上 → 检查 Postgres `pg_hba.conf` 允许该 IP、防火墙放行 5432。

> **若你的 Postgres 在别的地址/密码**：改 `config/config.yaml` 的 `database.url`，并改 `scripts/db_proxy.py` 的 `TARGET`（见下一步）。

---

## 3. macOS 专用：启动 localhost DB 代理

> ⚠️ **macOS 必做**。uv 管理的独立 Python 3.12 构建因系统「本地网络」隐私授权，**无法直连 192.168.1.21**（报 `No route to host`），但能连 localhost 和公网。
> 解法：用**系统自带 `/usr/bin/python3`**（有 LAN 权限）跑一个透明 TCP 代理，3.12 连 localhost 即可。

```bash
# 用系统 python(不是 uv run!),后台跑
nohup python3 scripts/db_proxy.py > /tmp/cortex-db-proxy.log 2>&1 &
echo $! > /tmp/cortex-db-proxy.pid
```

`scripts/db_proxy.py` 默认：`127.0.0.1:5433 → 192.168.1.21:5432`。若你的 DB 在别处，改文件顶部 `TARGET`。

**验证**：
```bash
sleep 1
nc -z 127.0.0.1 5433 && echo "proxy UP ✓" || echo "proxy DOWN ❌"
cat /tmp/cortex-db-proxy.log    # 应有 "db-proxy listening 127.0.0.1:5433 -> ..."
```

❌ 排错：
- `python3` 找不到 → 用 `/usr/bin/python3` 全路径。
- 代理起来但转发出错 → 系统 python 也要有 LAN 权限；若也被挡，在「系统设置 → 隐私与安全性 → 本地网络」给终端放行。
- **Linux 无此问题**，可跳过本步，直接把 `config/config.yaml` 的 `database.url` 指向真实 Postgres 地址（如 `192.168.1.21:5432`）。

---

## 4. 安装后端依赖

```bash
uv sync
```

**验证**：
```bash
uv run python -c "import fastapi, sqlalchemy, pgvector, openai, mcp; print('deps OK')"
```
应输出 `deps OK`。

❌ 排错：
- 装包失败 → 网络/proxy 问题，试 `UV_INDEX_URL` 镜像。
- `requires-python >=3.12` 不满足 → `uv python install 3.12` 后 `uv venv --python 3.12 && uv sync`。

---

## 5. 初始化数据库 schema

```bash
uv run python -m cortex.cli db init
```

**验证**：
```bash
uv run python -c "
from cortex.db import session_scope
from sqlalchemy import text
with session_scope() as c:
    n = c.execute(text(\"SELECT count(*) FROM pg_tables WHERE schemaname='cortex'\")).scalar()
    d = c.execute(text(\"SELECT format_type(a.atttypid,a.atttypmod) FROM pg_attribute a JOIN pg_class cl ON cl.oid=a.attrelid JOIN pg_namespace n ON n.oid=cl.relnamespace WHERE n.nspname='cortex' AND cl.relname='entities' AND a.attname='embedding'\")).scalar()
    print(f'tables={n} embedding={d}')
"
```
应输出 `tables=18 embedding=vector(1024)`（18 含 concepts/synonyms/temporal_phrases 等扩展表）。

❌ 排错：
- 连不上 DB → 代理没起（步骤 3）或 `config/config.yaml` 的 url 不对。
- `vector(1024)` 与 config `embedding.dimension` 不符 → 启动校验会拒；确保 config 是 1024、schema 是 vector(1024)。
- 权限不足建 schema → 用 superuser 连。

---

## 6. 配置 LLM key（可选但推荐）

不配则抽取/回答走确定性 mock（管线仍能端到端跑，但图谱质量是规则版）。配真实 key 后切 Minimax-M3：

```bash
export CORTEX_LLM_EXTRACTION_API_KEY="sk-cp-..."   # Minimax key
export CORTEX_LLM_ANSWER_API_KEY="$CORTEX_LLM_EXTRACTION_API_KEY"
export CORTEX_LLM_SYNTHESIS_API_KEY="$CORTEX_LLM_EXTRACTION_API_KEY"
```

> Minimax-M3 是推理模型，响应带 `<think>` 标签。`src/cortex/services.py` 的 `strip_think()` + `parse_llm_json()` 已处理，无需额外配置。
> 也可直接改 `config/config.yaml` 的 `llm.*.api_key`（但别提交真实 key）。

**验证**（配了 key 才跑）：
```bash
uv run python -c "from cortex.config import llm_configured; print('llm configured:', llm_configured('extraction'))"
```
配了 key 应输出 `True`。

---

## 7. 启动后端 API

```bash
nohup uv run uvicorn cortex.api.app:app --port 8002 --log-level warning > /tmp/cortex-api.log 2>&1 &
echo $! > /tmp/cortex-api.pid
```

> 端口用 **8002**（8000 可能被占，8001 留给 MCP HTTP）。

**验证**：
```bash
for i in $(seq 1 20); do curl -sf http://127.0.0.1:8002/v1/health >/dev/null 2>&1 && break; sleep 0.5; done
curl -s http://127.0.0.1:8002/v1/health
```
应输出 `{"status":"ok","ok":true,"detail":"postgres reachable"}`。

❌ 排错：
- 起不来 → `cat /tmp/cortex-api.log` 看报错（多为代理没起 / schema 没建 / 端口占用）。
- 端口占用 → 换 `--port 8003`，并同步改步骤 8 的 vite proxy。

---

## 8. 启动异步 worker

worker 消费队列做抽取（入库后图谱是 worker 异步建的）。

```bash
nohup uv run python -m cortex.cli worker > /tmp/cortex-worker.log 2>&1 &
echo $! > /tmp/cortex-worker.pid
```

**验证**：
```bash
sleep 1
tail -2 /tmp/cortex-worker.log    # 应有 "worker ... started"
```

❌ 排错：worker 不抢 job → 确认 DB 可连（它和 API 共用一个 DB）。

---

## 9. 安装并启动前端

```bash
cd frontend
npm install
# 改 vite proxy 指向你的后端端口(默认 8002,若步骤7换了端口这里也改)
# vite.config.ts 里 server.proxy['/v1'].target = 'http://localhost:<后端端口>'
nohup npm run dev > /tmp/cortex-frontend.log 2>&1 &
echo $! > /tmp/cortex-frontend.pid
cd ..
```

**验证**：
```bash
for i in $(seq 1 20); do curl -sf http://127.0.0.1:5173 >/dev/null 2>&1 && break; sleep 0.5; done
curl -s http://127.0.0.1:5173/v1/health    # 经 vite 代理打后端
```
应输出后端 health JSON。

❌ 排错：
- `npm install` 慢/失败 → 用镜像 `npm config set registry https://registry.npmmirror.com`。
- 5173 起不来 → `cat /tmp/cortex-frontend.log`；端口占用换 `--port 5174`（`npm run dev -- --port 5174`）。
- 代理 404 → 确认 `vite.config.ts` 的 target 端口与步骤 7 一致。

---

## 10. 端到端验证

### 10.1 灌一条记忆
```bash
curl -s -X POST http://127.0.0.1:8002/v1/experience \
  -H "Content-Type: application/json" -H "X-Cortex-Actor: user:alice" \
  -d '{"scope":"org:acme/dept:sales/user:alice","modality":"conversation",
       "content":{"kind":"message","role":"user","text":"Priya Rao owns the Q3 Renewal project at Acme Corp."},
       "context":{"observed_at":"2026-06-18T10:00:00Z"},"idempotency_key":"deploy-test-1"}'
```
应返回 `{"event_id":"...","wal_offset":N,"status":"captured",...}`。

### 10.2 等 worker 抽取（2-3 秒）
```bash
sleep 3
curl -s "http://127.0.0.1:8002/v1/entities?scope=org:acme/dept:sales/user:alice" -H "X-Cortex-Actor: user:alice" \
  | python3 -m json.tool | head -20
```
应看到 Priya Rao / Acme Corp / Q3 Renewal 等实体。

### 10.3 问答
```bash
curl -s -X POST http://127.0.0.1:8002/v1/answer \
  -H "Content-Type: application/json" -H "X-Cortex-Actor: user:alice" \
  -d '{"scope":"org:acme/dept:sales/user:alice","query":"who owns the Q3 Renewal"}' \
  | python3 -m json.tool
```
应返回带 `answer` + `citations` 的响应。

### 10.4 打开网页
浏览器访问 **http://localhost:5173**：
- `/ingest` 入库 → 右侧 SSE 看 captured→extracted→indexed
- `/graph` 图谱 → 节点边可视化（节点名深色 + 白描边，浅底可见）
- `/qa` 问答 → 带引用回答
- `/browse` 浏览 → events/facts/beliefs 列表

全部正常 → 部署成功。🎉

---

## 11. （可选）启动 MCP server

给 agent 注册用。

**stdio**（本地单 agent）：
```bash
uv run python -m cortex.cli mcp
```

**streamable-http**（多人共享）：
```bash
nohup uv run python -m cortex.cli mcp-http --port 8001 > /tmp/cortex-mcp.log 2>&1 &
```
注册到 Claude Code：项目根 `.mcp.json` 已就绪（stdio），或 `claude mcp add --transport http cortex http://<host>:8001/mcp -H "X-Cortex-Scope: org:acme/user:bob"`。详见 `docs/mcp.md`。

---

## 进程管理速查

| 进程 | pid 文件 | 日志 | 停止 |
|---|---|---|---|
| DB 代理 | `/tmp/cortex-db-proxy.pid` | `/tmp/cortex-db-proxy.log` | `kill $(cat /tmp/cortex-db-proxy.pid)` |
| 后端 API | `/tmp/cortex-api.pid` | `/tmp/cortex-api.log` | `kill $(cat /tmp/cortex-api.pid)` |
| worker | `/tmp/cortex-worker.pid` | `/tmp/cortex-worker.log` | `kill $(cat /tmp/cortex-worker.pid)` |
| 前端 | `/tmp/cortex-frontend.pid` | `/tmp/cortex-frontend.log` | `kill $(cat /tmp/cortex-frontend.pid)` |
| MCP HTTP | `/tmp/cortex-mcp.pid` | `/tmp/cortex-mcp.log` | `kill $(cat /tmp/cortex-mcp.pid)` |

**一键停全部**：
```bash
for f in /tmp/cortex-*.pid; do [ -f "$f" ] && kill $(cat "$f") 2>/dev/null; done
```

**启动顺序**：DB 代理 → 后端 API → worker → 前端（→ MCP）。
**停止顺序**：反过来，或直接全停（无强依赖，DB 代理停了 API 会报连不上但不会崩）。

---

## 回归验收（确认系统健康）

```bash
bash scripts/run_regression.sh
```
预期：5 套件全 PASS（stage0 37 + stage6 14 + stage7 31 + mcp 9 + mcp-http 7）。

单项：
```bash
python3 scripts/stage0/run_all.py        # SQL 冒烟
uv run python scripts/verify_stage7.py   # 功能验收
uv run python -m cortex.cli smoke        # 端到端 demo
```

---

## 附录 A：Linux 部署差异

1. **无 DB 代理步骤**：Linux 无 macOS 本地网络授权问题，3.12 可直连 Postgres。跳过步骤 3，把 `config/config.yaml` 的 `database.url` 改成真实地址（如 `postgresql://postgres:...@192.168.1.21:5432/postgres`）。
2. **系统 python3 可能就是 3.12**：可直接 `python3 -m venv .venv && pip install -e .`，但推荐仍用 `uv sync`。
3. **服务化**（可选）：用 systemd 把 API/worker/代理各做成 unit，开机自启。示例 unit 见附录 B。

## 附录 B：systemd unit 示例（Linux 生产）

`/etc/systemd/system/cortex-api.service`：
```ini
[Unit]
Description=cortex FastAPI
After=network.target

[Service]
WorkingDirectory=/opt/cortex-py
Environment=CORTEX_LLM_EXTRACTION_API_KEY=sk-cp-...
Environment=CORTEX_LLM_ANSWER_API_KEY=sk-cp-...
Environment=CORTEX_LLM_SYNTHESIS_API_KEY=sk-cp-...
ExecStart=/opt/cortex-py/.venv/bin/uvicorn cortex.api.app:app --port 8002
Restart=on-failure
User=cortex

[Install]
WantedBy=multi-user.target
```
worker / db_proxy 同理（换 `ExecStart`）。`systemctl enable --now cortex-api cortex-worker`。

## 附录 C：常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| API 起不来，日志 `No route to host` | DB 代理没起 / DB 不可达 | 步骤 3 起代理；或 config url 指错 |
| 入库成功但图谱空 | worker 没起 | 步骤 8 起 worker；`tail /tmp/cortex-worker.log` |
| 抽取结果是 `mock-extractor` | 没配 LLM key | 步骤 6 配 key |
| answer 带 `<think>` 标签 | 未走 `strip_think`（旧版） | 确认 `services.py` 的 `strip_think` 在 answer 路径调用 |
| 前端 `/v1/*` 404 | vite proxy target 端口不对 | 步骤 9 改 `vite.config.ts` target |
| `vector(1024)` 维度不符 | 换了 embedding 模型但没改 schema | schema 与 config dimension 必须一致 |
| 图谱节点名看不见 | （已修）字体描边 | 确认 `GraphView.vue` font 有 `strokeWidth`+`strokeColor` |

---

*部署成功后，参考 `README.md` 了解各页面用法，`docs/mcp.md` 了解 agent 注册，`docs/specs/` 了解设计。*
