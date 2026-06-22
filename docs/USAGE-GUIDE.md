# cortex 使用指南（给 AI agent / 操作者）

> **你是谁、你要干什么**：你负责维护某型精密设备的知识图谱和记忆库。你手里有结构文档、历史故障报告、传感器信息等整理好的内容。你要把它们灌进 cortex，之后能精准召回。
>
> **cortex 是什么**：一个事实存储 + 检索引擎。它存你的知识、在被问到时返回相关事实。它不做诊断推理——那是你（下游 agent）的工作。

---

## 第一步：确定 scope（命名空间）

cortex 用 scope 路径隔离不同设备/产线的知识图谱。**同一 scope 里的实体才能互相连接**。

选一个 scope 命名规则，比如：

```
equip:XXX-v1                                    ← 整台设备的通用知识（结构、参数、传感器布局）
equip:XXX-v1/line:A                             ← 某条产线上的 XXX v1
equip:XXX-v1/line:A/user:diag                   ← 诊断 agent 的记忆（故障案例、排查记录）
```

**推荐**：所有知识放在 `equip:XXX-v1` 一个 scope 里（结构 + 案例 + 传感器全在一起，图谱最连通）。如果有多个机台，各用不同 scope。

下文以 `SCOPE=equip:XXX-v1` 为例。

---

## 第二步：确保服务在跑

```bash
# 1. DB 直连（macOS 授权过的，不需要代理）
# 如果用代理：python3 scripts/db_proxy.py

# 2. 后端 API
uv run uvicorn cortex.api.app:app --port 8002

# 3. Worker（异步抽取）
uv run python -m cortex.cli worker

# 4.（可选）MCP HTTP server（给 agent 注册用）
uv run python -m cortex.cli mcp-http --port 8001

# 5.（可选）前端
cd frontend && npm run dev    # → http://localhost:5173
```

验证服务正常：
```bash
curl http://localhost:8002/v1/health
# 应返回 {"status":"ok","ok":true,...}
```

---

## 第三步：预置因果谓词词表（建库时做一次）

这一步告诉 cortex 你的领域用哪些谓词、哪些是单值（新值替代旧值）、哪些是多值（允许多条共存）。

```bash
curl -X POST http://localhost:8002/v1/vocabularies \
  -H "Content-Type: application/json" \
  -H "X-Cortex-Actor: admin" \
  -d '{
    "scope": "equip:XXX-v1",
    "name": "predicate",
    "kind": "closed",
    "values": [
      {"canonical": "caused_by", "aliases": ["导致", "引起"]},
      {"canonical": "led_to", "aliases": ["引发"]},
      {"canonical": "has_symptom", "aliases": ["表现为", "症状"]},
      {"canonical": "symptom_of", "aliases": ["是...的症状"]},
      {"canonical": "monitored_by", "aliases": ["被...监测"]},
      {"canonical": "installed_on", "aliases": ["安装在"]},
      {"canonical": "controlled_by", "aliases": ["被...控制"]},
      {"canonical": "regulates", "aliases": ["调节"]},
      {"canonical": "part_of", "aliases": ["属于", "组成部分"]},
      {"canonical": "has_component", "aliases": ["包含", "由...组成"]},
      {"canonical": "repaired_by", "aliases": ["修复", "更换"]},
      {"canonical": "observed_by", "aliases": ["发现", "排查"]},
      {"canonical": "affects", "aliases": ["影响"]},
      {"canonical": "triggers", "aliases": ["触发"]},
      {"canonical": "correlates_with", "aliases": ["相关"]},
      {"canonical": "ruled_out", "aliases": ["排除"]},
      {"canonical": "confirmed_by", "aliases": ["确认"]},
      {"canonical": "references", "aliases": ["参考", "类似"]},
      {"canonical": "has_status", "aliases": ["状态"]},
      {"canonical": "cascades_to", "aliases": ["级联", "传播"]},
      {"canonical": "detected_by", "aliases": ["检测到"]},
      {"canonical": "investigates", "aliases": ["排查", "检查"]},
      {"canonical": "suggests", "aliases": ["暗示", "提示"]},
      {"canonical": "deviates_from", "aliases": ["偏离"]},
      {"canonical": "feedback_to", "aliases": ["反馈"]}
    ]
  }'
```

> 这做一次就行。之后抽取时 LLM 产出的谓词会自动归一到这些标准值。
> `has_status` 是单值（新状态替代旧状态）；其余默认多值（允许多条共存，如一个部件可以有多个 `caused_by`）。

---

## 第四步：灌入知识（3 种内容 × 3 种方式）

### 方式 A：机械结构文档（有标题分层的 markdown）

**适合**：设备结构说明（系统→子系统→部件→传感器→故障特征）

```bash
curl -X POST http://localhost:8002/v1/ingest/document \
  -H "Content-Type: application/json" \
  -H "X-Cortex-Actor: admin" \
  -d '{
    "scope": "equip:XXX-v1",
    "text": "# XXX v1 真空系统\n\n真空系统由干泵、分子泵和截止阀V-3组成。\n\n## 干泵\n干泵型号DP-100，配压力传感器P-01（量程0-1000Pa）。正常工作时P-01读数<10Pa。\n干泵常见故障：泵油乳化（P-01缓慢上升）、轴承磨损（振动增大）。\n\n## 分子泵\n分子泵配转速传感器S-01和温度传感器T-03。正常转速27000rpm，温度<60℃。\n分子泵常见故障：轴承故障（S-01转速波动）、冷却不足（T-03超温）。\n\n## 截止阀V-3\nV-3控制真空管路通断，由PLC控制。密封圈材料为FKM。\nV-3常见故障：密封老化（保压测试压降>0.1mTorr/min）。",
    "intent": "structure"
  }'
```

cortex 会：
1. 按标题切块（`真空系统` / `干泵` / `分子泵` / `截止阀V-3`）
2. 每块用 LLM 抽取实体和关系
3. 自动建立：`干泵 --part_of--> 真空系统`、`P-01 --installed_on--> 干泵`、`泵油乳化 --affects--> 干泵` 等

> **关键**：结构文档先灌，这样后续故障报告里提到的实体（如 V-3、P-01）能链接到已有实体，不会重复建。

### 方式 B：故障排查报告（自然语言叙述）

**适合**：清洗过的故障排查时间轴（含因果推理、传感器数据、人员）

```bash
curl -X POST http://localhost:8002/v1/experience \
  -H "Content-Type: application/json" \
  -H "X-Cortex-Actor: admin" \
  -d '{
    "scope": "equip:XXX-v1",
    "modality": "document",
    "content": {
      "kind": "text",
      "text": "[2026-06-15 02:14] P-01压力传感器读数从5Pa缓慢上升到45Pa，触发真空异常告警。观察者：夜班操作员王某。\n\n[02:30] 工程师李某和张某讨论：影响范围是主工艺步骤的真空度。假设1：怀疑干泵故障。假设2：怀疑管路泄漏。假设3：怀疑V-3阀门密封失效。\n\n[03:00] 排查干泵：检查泵油外观，发现泵油乳化严重。检查P-01趋势，72h内从5Pa升到45Pa。干泵轴承振动正常。排除假设1（泵油乳化是现象不是根因）。\n\n[03:30] 排查管路：对V-3下游做保压测试，发现V-3处压降0.3mTorr/min。V-3密封圈上次更换是12个月前（推荐周期6个月）。\n\n[04:00] 相关性分析：V-3保压测试压降与P-01读数上升趋势相关系数0.92。管路其他段保压正常。排除假设2（管路本体无泄漏）。\n\n[05:00] 确认根因：V-3密封圈老化导致微漏。参考2025-11案例-007（相同故障模式）。根因链：真空度下降 caused_by V-3阀门微漏 caused_by V-3密封圈老化。\n\n[06:00] 修复：更换V-3密封圈。修复后P-01恢复到5Pa。保压测试通过。\n\n[08:00] 回归测试：跑3批次验证，真空度稳定。预防措施：V-3密封圈更换周期从12个月缩短到6个月。"
    },
    "context": {
      "observed_at": "2026-06-15T02:14:00Z",
      "intent": "incident_retrospective",
      "labels": ["vacuum", "V-3", "seal"]
    },
    "idempotency_key": "case-20260615-vacuum-leak"
  }'
```

cortex 会用 LLM 从这段叙述中抽取：
- **实体**：P-01、干泵、V-3、密封圈老化、李某、张某 等
- **因果链**：真空度下降 --caused_by--> V-3阀门微漏 --caused_by--> V-3密封圈老化
- **传感器关系**：P-01读数上升 --detected_by--> P-01
- **诊断推理**：怀疑干泵故障 --ruled_out--> 干泵；V-3保压测试 --correlates_with--> P-01读数(r=0.92)
- **修复**：V-3阀门微漏 --repaired_by--> 更换V-3密封圈
- **历史参照**：本次诊断 --references--> 案例-007

> **关键**：`intent: "incident_retrospective"` 触发诊断因果抽取 prompt（包含传感器↔故障↔根因的连接规则）。
>
> **写报告时注意**：排查过程中的排除项（"排除了假设1"）、相关性分析（"相关系数0.92"）都要写进去——cortex 会提取它们，下次遇到类似问题时，agent 能召回"上次排除了什么、什么相关"，避免重复排查。

### 方式 C：直接三元组（你已确定的因果，零损失）

**适合**：你 100% 确定的结构关系或因果，不想依赖 LLM 抽取

```bash
# 单条
curl -X POST http://localhost:8002/v1/experience \
  -H "Content-Type: application/json" \
  -H "X-Cortex-Actor: admin" \
  -d '{
    "scope": "equip:XXX-v1",
    "modality": "imported",
    "content": {
      "kind": "triple",
      "triple": {
        "subject": {"name": "分子泵"},
        "predicate": "part_of",
        "object": {"name": "真空系统"}
      }
    },
    "context": {"observed_at": "2026-01-01T00:00:00Z", "intent": "structure"},
    "idempotency_key": "triple-molecular-pump-partof-vacuum"
  }'

# 批量（用 JSONL）
curl -X POST http://localhost:8002/v1/import/jsonl \
  -H "Content-Type: application/json" \
  -H "X-Cortex-Actor: admin" \
  -d '{
    "scope": "equip:XXX-v1",
    "lines": "{\"modality\":\"imported\",\"content\":{\"kind\":\"triple\",\"triple\":{\"subject\":{\"name\":\"V-3\"},\"predicate\":\"part_of\",\"object\":{\"name\":\"真空系统\"}}},\"context\":{\"observed_at\":\"2026-01-01T00:00:00Z\"},\"idempotency_key\":\"triple-v3-partof-vacuum\"}\n{\"modality\":\"imported\",\"content\":{\"kind\":\"triple\",\"triple\":{\"subject\":{\"name\":\"P-01\"},\"predicate\":\"installed_on\",\"object\":{\"name\":\"干泵\"}}},\"context\":{\"observed_at\":\"2026-01-01T00:00:00Z\"},\"idempotency_key\":\"triple-p01-on-dryPump\"}"
  }'
```

> triple 直写不经 LLM，直接建实体和关系。适合：你从设备手册里手动整理的确定关系、从前置 agent 推理好的因果链。

### 灌入顺序建议

```
1. 先灌结构文档（方式 A）→ 建立部件/传感器/控制器的基础实体和层级关系
2. 再灌故障报告（方式 B）→ LLM 抽取的实体会链接到已有实体（如 V-3、P-01）
3. 最后补充确定的三元组（方式 C）→ 修补 LLM 可能遗漏的关系
```

---

## 第五步：验证图谱质量

### 看有多少实体和关系

```bash
# 实体列表
curl "http://localhost:8002/v1/entities?scope=equip:XXX-v1" -H "X-Cortex-Actor: admin"

# facts 列表
curl "http://localhost:8002/v1/facts?scope=equip:XXX-v1" -H "X-Cortex-Actor: admin"

# 只看因果
curl "http://localhost:8002/v1/facts?scope=equip:XXX-v1&predicate=caused_by" -H "X-Cortex-Actor: admin"
```

### 用 Python 脚本看图谱全貌

```python
import httpx
SCOPE = "equip:XXX-v1"
HEAD = {"X-Cortex-Actor": "admin"}

ents = httpx.get(f"http://localhost:8002/v1/entities", params={"scope": SCOPE}, headers=HEAD).json()
facts = httpx.get(f"http://localhost:8002/v1/facts", params={"scope": SCOPE}, headers=HEAD).json()

print(f"实体({len(ents['items'])}):")
for e in ents["items"]:
    print(f"  [{e.get('entity_type','?')}] {e['canonical_name']}")

print(f"\nfacts({len(facts['items'])}):")
for f in facts["items"]:
    obj = f["object"].get("value", "")
    print(f"  {f['subject']['name']} --{f['predicate']}--> {obj}")
```

或打开前端 `http://localhost:5173/graph`，scope 选 `equip:XXX-v1`，看可视化图谱。

---

## 第六步：召回 / 问答

### 检索相关事实（recall）

```bash
curl -X POST http://localhost:8002/v1/recall \
  -H "Content-Type: application/json" \
  -H "X-Cortex-Actor: admin" \
  -d '{
    "scope": "equip:XXX-v1",
    "query": "真空度下降的原因是什么",
    "view": "local"
  }'
```

返回 StratifiedPack：命中的 facts + beliefs + 综述文本。

### 问答（answer）

```bash
curl -X POST http://localhost:8002/v1/answer \
  -H "Content-Type: application/json" \
  -H "X-Cortex-Actor: admin" \
  -d '{
    "scope": "equip:XXX-v1",
    "query": "V-3阀门泄漏怎么排查？上次类似故障怎么处理的？"
  }'
```

cortex 会召回相关 facts（包括历史案例、排除项、传感器数据），整理后呈现。**cortex 不做诊断推理**——它忠实呈现库里有什么，推理由你（下游 agent）做。

### view 参数

| view | 含义 | 什么时候用 |
|---|---|---|
| `local`（默认） | 只查当前 scope | 大多数情况 |
| `holistic` | 当前 scope + 所有祖先 scope | 如 `equip:XXX-v1/line:A/user:diag` 查时能看到 `equip:XXX-v1` 的通用结构知识 |
| `descend` | 当前 scope + 所有后代 | 管理层看所有产线 |
| `structured` | 只返 facts + beliefs（跳 events，轻量快） | agent 只需要结构化数据时 |

---

## 第七步：诊断 Case 管理

### 创建一个新 Case（遇到新故障时）

```bash
curl -X POST http://localhost:8002/v1/cases \
  -H "Content-Type: application/json" \
  -H "X-Cortex-Actor: diag-agent" \
  -d '{
    "scope": "equip:XXX-v1",
    "title": "2026-06-20 刻蚀均匀性偏差",
    "equipment": "XXX-v1",
    "lot": "LOT-20260620-003"
  }'
```

返回 `episode_id`，后续操作用它。

### 更新 Case 状态

```bash
# 排查中
curl -X PATCH http://localhost:8002/v1/cases/{episode_id} \
  -H "Content-Type: application/json" \
  -H "X-Cortex-Actor: diag-agent" \
  -d '{"phase": "investigation", "status": "investigating"}'

# 找到根因
curl -X PATCH http://localhost:8002/v1/cases/{episode_id} \
  -H "Content-Type: application/json" \
  -d '{"phase": "root_cause", "root_cause": "腔体积碳导致等离子模式漂移"}'

# 修复完成
curl -X PATCH http://localhost:8002/v1/cases/{episode_id} \
  -H "Content-Type: application/json" \
  -d '{"phase": "regression", "status": "resolved", "resolution": "执行腔体clean, 更换密封圈"}'
```

### 把排查记录关联到 Case

每灌入一条排查 event 后，关联到 case：

```bash
curl -X POST http://localhost:8002/v1/cases/{episode_id}/events \
  -H "Content-Type: application/json" \
  -d '{"event_id": "上面experience返回的event_id"}'
```

### 查历史 Case

```bash
# 列出所有 resolved 的 case
curl "http://localhost:8002/v1/cases?scope=equip:XXX-v1&status=resolved" -H "X-Cortex-Actor: admin"

# 搜索包含"密封"的 case
curl -X POST http://localhost:8002/v1/cases/search \
  -H "Content-Type: application/json" \
  -d '{"scope": "equip:XXX-v1", "query": "密封"}'

# 查完整 case（含 events + facts + beliefs）
curl http://localhost:8002/v1/cases/{episode_id} -H "X-Cortex-Actor: admin"
```

---

## 第八步：给 AI agent 注册 MCP

如果下游 agent 要直接调 cortex（不通过 HTTP），注册 MCP：

### stdio（本地单 agent）

Claude Code 项目根 `.mcp.json`：
```json
{
  "mcpServers": {
    "cortex": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/cortex-py", "python", "-m", "cortex.cli", "mcp"],
      "env": {"CORTEX_SCOPE": "equip:XXX-v1"}
    }
  }
}
```

### HTTP（多人共享）

```bash
uv run python -m cortex.cli mcp-http --port 8001
```

agent 连 `http://host:8001/mcp`，带 header `X-Cortex-Scope: equip:XXX-v1`。

### agent 可用的 28 个工具

agent 注册后可以调用：
- `memory_store(text)` — 存一条记忆（自动抽取入图谱）
- `memory_search(query)` — 检索相关事实
- `answer(query)` — 问答
- `case_create / case_update / case_get / case_list / case_search` — Case 管理
- `entity_list / entity_edges / facts_timeline` — 图谱浏览
- `bulk_ingest(texts)` — 批量灌入
- `health_check()` — 检查服务状态
- ...（完整列表见 `docs/mcp.md`）

---

## 完整工作流示例

```
第一天：建库
  1. 预置词表 → POST /vocabularies
  2. 灌结构文档 → POST /ingest/document（你的设备手册 markdown）
  3. 等几分钟（worker 异步抽取）
  4. 验证图谱 → GET /entities + GET /facts

第二天~第N天：灌经验
  5. 每次故障排查完 → 把清洗后的报告 → POST /experience
  6. 如果有确定的三元组 → POST /experience (triple)
  7. 遇到新故障 → POST /cases 建一个 case
  8. 排查过程中 → recall 查"上次类似故障怎么处理的"
  9. 排查完 → PATCH /cases 更新根因和修复措施

日常使用
  10. agent 通过 MCP 或 API 随时 recall
  11. 浏览器 http://localhost:5173 看图谱和问答
```

---

## 常见问题

**Q: 灌进去后多久能搜到？**
A: worker 异步抽取，通常 10-60 秒（取决于文本长度和 LLM 速度）。如果要立即搜到，加 `?wait=indexed` 参数阻塞到抽取完成。

**Q: LLM 抽取质量不理想怎么办？**
A: ①确保 `intent` 设对（`structure` vs `incident_retrospective`）；②报告写得详细（包含排除项、相关性分析、传感器数据）；③确定的因果关系用方式 C（triple 直写）补。

**Q: 同一个设备被建成两个实体了怎么办？**
A: 这是实体链接问题。cortex 会用别名精确匹配 + 向量召回 + LLM 灰区判定。如果还是分裂了，可以通过灌入别名 triple 来桥接（`{"subject":{"name":"V-3"},"predicate":"has_status","object":{"name":"截止阀V-3"}}`），或者后续手动合并。

**Q: scope 怎么选？**
A: 一个设备一个 scope（`equip:XXX-v1`）。如果有多台同型设备且经验需要共享，用祖先 scope（`equip:XXX-series`）灌通用知识，各自 scope 灌机台特定案例，recall 时用 `view=holistic` 向上查。

**Q: 怎么删掉错误的知识？**
A: `POST /v1/forget`（软忘，闭合 recorded_to）或 `POST /v1/erasures`（硬删）。详见 API 文档。
