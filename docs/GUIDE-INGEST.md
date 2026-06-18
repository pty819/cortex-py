# Agent 喂入指南：怎么往 Cortex 记忆系统添加经验

> **目标读者**：你的前置 agent（负责从原始数据中清洗、提炼知识），以及任何要往 cortex 写入数据的自动化脚本。
> **核心问题**：我有多种类型的精炼数据（聊天记录、设计稿、传感器关系、零散知识），不是一个大案例而是分批分案地往里加，知识图谱能处理好吗？答案是**能**——本文档讲清楚怎么喂、喂什么格式、图谱怎么处理增量积累。

---

## 0. 先回答你的核心顾虑：分批喂入，图谱能不能接住？

**能，而且这正是这个系统的强项。** 原因：

| 机制 | 对"分批喂入"的作用 |
|---|---|
| **实体链接 B over C** | 第一批提了"张工"，第三批又提到"老张"，系统用向量召回 + LLM 判定归到同一个实体。你不需要在第一批就把所有人标全。 |
| **双时态** | 6 月 1 日的故障和 8 月 3 日的故障各是不同的 valid_from 时间窗口，不会互相覆盖。"历史上的判断"和"现在的理解"分开存。 |
| **scope 隔离** | `mech:plant1/line:A` 的故障图谱和 `mech:plant1/line:B` 的完全隔离。你还可以把某个重要案例单独开一个 scope。 |
| **超替（supersession）** | 第一批说"故障原因是润滑不足"，第三批新证据说"根本原因是密封失效导致润滑流失"——系统自动闭合旧判断、插入新判断，timeline 保留全部历史。 |
| **supports 链** | 每个结论（fact/belief）都回指产生它的原始 event_id。五批数据的结论都可以溯源到各自的原始叙述。 |

**简单说：系统不怕你分批喂，反而擅长——因为它不覆盖，只追加和连接。**

---

## 1. 你的数据类型 × 对应喂法速查

| 你要喂的数据 | 用哪条路径 | 格式 | 典型 agent 动作 |
|---|---|---|---|
| 精炼后的故障叙述（中文，一段文字） | **路径 B：自然语言叙述** | plain text | `POST /v1/experience` modality=document |
| 清洗后的结构化因果（JSON 三元组） | **路径 A：triple 直写** | JSON triple | `POST /v1/experience` kind=triple |
| 机械结构文档（有标题章节的 markdown） | **路径 C：切块入库** | markdown | `POST /v1/ingest/document` |
| 传感器/部件间关系（结构化表） | **路径 A：triple 批量** | JSONL triple | `POST /v1/import/jsonl` |
| 零散知识点（一句话、一个判断） | **路径 B：短叙述** | plain text | `POST /v1/experience` |
| 某个案例的完整历史（多轮对话清洗后） | **路径 B：长叙述** | plain text | `POST /v1/experience` 或批量 |

---

## 2. 路径 A：结构化三元组直写（零损失，最快）

**适用**：前置 agent 已经从原始数据里推理出了因果关系、部件关系、传感器关联——已经知道"轴承过热 **因为** 润滑不足"。

**格式**：

```json
{
  "scope": "mech:plant1/line:A/user:diag",
  "modality": "imported",
  "content": {
    "kind": "triple",
    "triple": {
      "subject": {"name": "轴承过热"},
      "predicate": "caused_by",
      "object": {"name": "润滑不足"}
    }
  },
  "context": {
    "observed_at": "2026-06-01T00:00:00Z",
    "intent": "diagnosis",
    "labels": ["bearing", "motor"]
  },
  "idempotency_key": "unique-per-case-triple-001"
}
```

**关键字段说明**：

| 字段 | 必填 | 说明 |
|---|---|---|
| `scope` | ✅ | 你组织知识的层级路径（见 §7） |
| `content.kind` | ✅ | 固定 `"triple"`，系统识别后跳过 LLM 抽取，直接建实体+fact |
| `triple.subject.name` | ✅ | 主体名（系统会自动做实体链接：和已有同名/同义实体合并） |
| `triple.predicate` | ✅ | **用因果词表里的词**（见 §6），系统会自动归一 |
| `triple.object.name` | ✅ | 客体名 |
| `context.observed_at` | ✅ | 事情发生的时间（不是入库时间） |
| `context.intent` | 推荐 | `"diagnosis"` 会触发因果词表归一；`"structure"` 表示机械结构；`"general"` 默认 |
| `idempotency_key` | ✅ | **每条唯一**，重复提交同 key 同 body 不会重复入库 |

**批量喂入**（一次 1000 条以内）：

```json
POST /v1/import/jsonl
Content-Type: application/json

{
  "scope": "mech:plant1/line:A/user:diag",
  "scope_template": "{device}",   // 可选：从每行 record 的字段动态取 scope
  "lines": "{\"type\":\"triple\",\"subject\":{\"name\":\"主轴\"},\"predicate\":\"part_of\",\"object\":{\"name\":\"电机系统\"},\"observed_at\":\"2026-01-01T00:00:00Z\"}\n{\"type\":\"triple\",...}"
}
```

> **scope_template**：如果三元组里有设备/产线字段，可用模板自动分 scope。例如 `scope_template="mech:{device}/line:{line}"`，每行 record 里带 `"device":"plant1","line":"A"` 自动填。

**你的 agent 应该把因果关系拆成多条 triple**：

```
轴承过热 --caused_by--> 润滑不足        ← 第一条
润滑不足 --caused_by--> 密封失效        ← 第二条(根因继续往下追)
轴承过热 --has_symptom--> 异常振动      ← 症状
轴承过热 --repaired_by--> 更换密封件    ← 措施
故障 --observed_by--> 张工              ← 相关人
```

这些在图谱里会形成**因果链**，从"轴承过热"BFS 沿 `caused_by` 走就能到根因。

---

## 3. 路径 B：自然语言叙述（最自然，agent 直接写文字）

**适用**：你已经有一段清洗过的故障叙述（中文或英文，几千字以内），系统用 LLM 自动抽取实体和因果三元组。

**格式**：

```json
{
  "scope": "mech:plant1/line:A/user:diag",
  "modality": "document",
  "content": {
    "kind": "message",
    "role": "user",
    "text": "2026年6月1日，产线A电机出现异常振动。张工排查发现轴承温度达95度。经检查是润滑脂耗尽导致轴承过热。更换润滑脂后温度降至45度，振动恢复正常。此次故障根本原因是润滑系统维护缺失。"
  },
  "context": {
    "observed_at": "2026-06-01T10:00:00Z",
    "intent": "incident_retrospective",
    "labels": ["motor", "bearing", "lubrication"]
  },
  "idempotency_key": "case-001-incident-01"
}
```

**关键点**：

1. **`modality`**：用 `"conversation"` 或 `"document"` 都行，系统识别后走抽取。用 `"document"` 更准确（表示这不是对话，是一段知识）。
2. **`context.intent`**：设 `"incident_retrospective"` 或 `"diagnosis"` 会触发**因果强化抽取 prompt**（LLM 会被引导抽取 `caused_by/led_to/symptom_of` 等因果谓词，而不是只抽 `owns/uses`）。
3. **不需要切块**：几千字的叙述直接一次喂进去。原因：①切块会切断因果链（"异响→排查→发现→原因→修复"是一个完整的推理链）；②现代 LLM 上下文 128K+，几千字完全装得下；③系统在入库前不处理（写路径零 LLM），抽取是异步的，不阻塞。
4. **`content.kind="text"`**（纯文本）也可以：`{"kind":"text","text":"一段故障叙述..."}`
5. **labels**：自由标签，方便检索时过滤（非强制，但推荐标上设备/部件名）。

**你的 agent 每次可以只喂一小段**（比如一条消息的摘要），也可以喂一个完整案例。系统不会因为分段喂而丢信息——每个段都是独立的 event，抽取后通过实体链接（同一个"轴承"、"张工"）自然连进图谱。

**观察到抽取结果是异步的**（写入后几秒，worker 才抽取入图）。你可以：
- 不等（最自然，写完就去做别的）
- 用 `?wait=indexed` 阻塞到抽取完成（适合需要立即查的场景）

```json
POST /v1/experience?wait=indexed
{ ... }
```

---

## 4. 路径 C：结构文档切块入库

**适用**：机械结构文档（有 `# 主轴` / `## 冷却系统` 标题的 markdown），系统按章节切块，每块独立抽取，块间用 `part_of` 自动连接。

```json
POST /v1/ingest/document
{
  "scope": "mech:plant1/line:A/user:diag",
  "text": "# 电机系统\n电机系统包含主轴、冷却系统和润滑系统。\n\n## 主轴\n主轴使用6208型号轴承...\n\n## 冷却系统\n风扇直径200mm...",
  "intent": "structure",
  "min_chars": 200,
  "max_chars": 2000
}
```

**切块逻辑**（系统内部自动做）：
- 有 `#` 标题 → 按标题层级切，每块带路径（如 `电机系统/主轴`）
- 无标题 → 按段落（双换行）切
- 每块太短（<200字）→ 合并到上一块（标题块保持独立，不合并）
- 每块太长（>2000字）→ 按句号细分

**返回**：`{"chunks": 4, "import_id": "...", "accepted": 4}`，系统异步抽取每块。

**图谱效果**：系统在抽取每块时会自动产出 `主轴 part_of 电机系统`、`冷却系统 part_of 电机系统` 等关系，形成机械结构的层级图。

---

## 5. 传感器 / 部件间关系（结构化表 → 三元组批量灌）

**适用**：你有一张 CSV/Excel 表，记录"温度传感器-T1 装在轴承座上"、"振动传感器-V2 监测主轴"这种结构化关系。

你的 agent 先把每行转成 triple，然后批量灌入：

```
# 伪代码：agent 把表格行变成三元组
for row in sensor_table:
    triple = {
      "subject": {"name": row["sensor_name"]},
      "predicate": "installed_on",   # 或你定义的任何谓词
      "object": {"name": row["component"]}
    }
    # 批量 POST /v1/import/jsonl
```

**注意**：谓词用什么都可以，但如果用 §6 里的标准因果词表（`caused_by` 等），图遍历时能直接走因果推理。非因果谓词（如 `installed_on`、`monitored_by`、`produced_by`）完全合法，只是不参与因果推理——它们形成的是"结构关系图"，不是"因果链图"。两种图都很有用。

---

## 6. 因果谓词词表（建议，非强制）

系统预置了一套诊断场景的因果谓词闭合词表。用这些词，抽取和图遍历效果最好：

| 谓词 | 含义 | 方向 | 例子 |
|---|---|---|---|
| `caused_by` | 故障由…引起 | 故障→原因 | `轴承过热 caused_by 润滑不足` |
| `led_to` | …导致了 | 原因→结果 | `过载 led_to 电机烧毁` |
| `symptom_of` | …是…的症状 | 症状→故障 | `异响 symptom_of 轴承磨损` |
| `has_symptom` | …表现为 | 故障→症状 | `过热 has_symptom 振动加大` |
| `affects` | …影响了 | 部件→部件 | `密封失效 affects 润滑效果` |
| `part_of` | …是…的一部分 | 部件→系统 | `轴承 part_of 主轴` |
| `has_component` | …包含 | 系统→部件 | `电机系统 has_component 冷却系统` |
| `repaired_by` | …被…修复 | 故障→措施 | `轴承磨损 repaired_by 更换轴承` |
| `observed_by` | …被…发现 | 故障→人 | `异常振动 observed_by 张工` |
| `preceded_by` | …发生在…之后 | 事件→事件 | `停机 preceded_by 异响` |

**不在这个列表里也可以**——系统支持任意谓词，只是抽取时 LLM 可能不选它。如果你的前置 agent 已经结构化了（路径 A），可以用任何你定义的谓词（如 `monitored_by`、`calibrated_on`、`produced_by`）。

---

## 7. Scope 设计：你的知识怎么组织

Scope 是你组织记忆的层级路径，直接影响图谱隔离（不同 scope 的"Acme"不会糊在一起）。

**推荐结构**（机械诊断场景）：

```
mech:plant1                        ← 全厂级知识（设备目录、通用维护规范）
mech:plant1/line:A                 ← 产线 A 的通用知识（部件关系、传感器布局）
mech:plant1/line:A/user:diag       ← 诊断 agent 的记忆（故障案例、推理过程）
mech:plant1/line:A/user:maint      ← 维护工程师的记忆（维修日志、备件）
mech:plant1/line:B                 ← 产线 B（隔离的独立图谱）
```

**三个读模式**：
- `view: local`（默认）：只看当前 scope
- `view: holistic`：当前 scope + 所有祖先（诊断 agent 看到产线 A + 全厂知识）
- `view: descend`：当前 scope + 所有后代（管理层看整个 plant1 下所有产线）

**实践建议**：
- **案例级 scope**（如果你希望每个故障案例完全隔离）：`mech:plant1/line:A/case:001`
- **通用级 scope**（推荐）：`mech:plant1/line:A/user:diag`，所有案例共用一个图谱，靠实体链接和双时态区分不同案例
- **混合**：通用知识在 `mech:plant1/line:A`，个别敏感案例在 `case:xxx`

---

## 8. 完整 agent 调用模板

你的前置 agent 每次拿到一批清洗好的数据后，按以下模板调用：

### 8.1 喂入一段故障叙述

```bash
curl -X POST http://<cortex-host>:8002/v1/experience \
  -H "Content-Type: application/json" \
  -H "X-Cortex-Actor: diag-agent" \
  -d '{
    "scope": "mech:plant1/line:A/user:diag",
    "modality": "document",
    "content": {
      "kind": "text",
      "text": "<这里放你清洗好的叙述文本>"
    },
    "context": {
      "observed_at": "2026-06-01T10:00:00Z",
      "intent": "incident_retrospective",
      "labels": ["motor", "bearing"]
    },
    "idempotency_key": "unique-case-id-seq-001"
  }'
```

### 8.2 批量灌结构化三元组

```bash
# 方式一：JSONL 文件
cat triples.jsonl | curl -X POST http://<cortex-host>:8002/v1/import/jsonl \
  -H "Content-Type: application/json" \
  -H "X-Cortex-Actor: diag-agent" \
  -d '{
    "scope": "mech:plant1/line:A/user:diag",
    "lines": "<每行一个 triple envelope JSON>"
  }'

# 方式二：先准备好 JSONL 文件，用 Python 调
import httpx
lines = open("triples.jsonl").read()
r = httpx.post("http://<cortex-host>:8002/v1/import/jsonl",
    json={"scope": "mech:plant1/line:A/user:diag", "lines": lines})
print(r.json())  # {"import_id":"...","accepted":50,"failed":0}
```

### 8.3 喂入结构文档

```bash
curl -X POST http://<cortex-host>:8002/v1/ingest/document \
  -H "Content-Type: application/json" \
  -H "X-Cortex-Actor: diag-agent" \
  -d '{
    "scope": "mech:plant1/line:A/user:diag",
    "text": "<markdown 格式的结构文档>",
    "intent": "structure"
  }'
```

### 8.4 查图谱（确认入库效果）

```bash
# 查看这个 scope 下的实体
curl "http://<cortex-host>:8002/v1/entities?scope=mech:plant1/line:A/user:diag"

# 查看因果链（所有 caused_by 关系）
curl "http://<cortex-host>:8002/v1/facts?scope=mech:plant1/line:A/user:diag&predicate=caused_by"

# 从某个故障出发 BFS 找根因
curl -X POST http://<cortex-host>:8002/v1/recall \
  -H "Content-Type: application/json" \
  -d '{"scope":"mech:plant1/line:A/user:diag","query":"轴承过热的根本原因","view":"holistic"}'
```

---

## 9. 注意事项

1. **`idempotency_key` 必须唯一**：同 key 同 body = 幂等（不重复），同 key 异 body = 409。建议格式：`{case-id}-{type}-{seq}`，如 `case-001-incident-01`。

2. **`observed_at` 是事情发生的时间**，不是你入库的时间。系统另外记录入库时间（`recorded_at`）。这两个时间分开存，后续可以问"6月1日当时我们知道什么"（as_known 查询）。

3. **先喂结构/关系，再喂案例叙述**：先用路径 C 灌机械结构文档（建立部件层级图），再用路径 B 灌故障案例（抽取的实体会链接到已有的部件上）。不是强制顺序，但先灌结构能让后续故障案例的实体链接更准确（因为图谱里已有部件名）。

4. **抽取是异步的**：POST /experience 返回 202 后，抽取在后台进行（几秒到几十秒）。如果你的 agent 需要立即确认入库效果，加 `?wait=indexed` 参数阻塞到抽取完成。

5. **前缀词推荐**：`labels` 是自由标签，不会自动分词。建议统一格式，如 `["motor","bearing","line-A"]`，方便后续 recall 时过滤。

6. **图谱增量积累不需要你做任何"合并"操作**：新案例的新实体如果和已有实体名字相同或语义相同（如"张工"和"老张"），实体链接（B over C）自动归一。你只需要正常喂数据，连接自然发生。

7. **查看抽取质量**：`GET /v1/facts?scope=...&limit=20` 看最近抽出来的三元组。如果 predicate 不对（比如 LLM 抽出了"has"而不是"caused_by"），检查 `context.intent` 是否设了 `"diagnosis"` 或 `"incident_retrospective"`（触发因果 prompt）。
