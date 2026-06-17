# 10 — 机械故障诊断知识库:入库方案

> **场景**:用 cortex 做复杂机械故障诊断知识库。原始数据(半年聊天记录+图)经**前置 agent 预清洗**后,产出两类内容入库:
> 1. **事件回溯**(自然语言叙述,含推理思路/因果链/相关人员,几千字)
> 2. **机械结构总结**(有标题章节的长文档,系统→子系统→部件层级)
>
> 清洗产物还含**结构化三元组**(前置 agent 已推理好的因果)。
> **核心要求**:因果链在图谱里用 predicate 表达(可图遍历),证据可溯源到原始 event,长文档不丢信息。

---

## 1. 三条入库路径(按内容类型分流)

| 内容类型 | 入库路径 | 走抽取? | 为什么 |
|---|---|---|---|
| **结构化三元组**(前置 agent 产出的 `{subject,predicate,object,cause}`) | `content.kind="triple"` 直写 | 否(直写) | 前置已推理好,二次抽取有损。直写零损失,最快最稳。 |
| **事件回溯叙述**(几千字自然语言) | `POST /experience`(modality=document) | 是(因果 prompt) | 叙述里因果隐含在文本,需 LLM 抽成因果三元组。一次全文抽(不切块,保因果链完整)。 |
| **机械结构长文档**(有标题章节) | 分层切块 → 每块 `/experience` | 是(每块抽 + part_of 连) | 按章节切(非傻切),每块是一个部件描述,块间用 part_of 连,图谱自然成层级。 |

---

## 2. 因果 predicate 词表(图谱表达)

机械故障诊断的因果链用这套 predicate(可在 vocabularies 注册成 closed 词表,强制归一):

| predicate | 含义 | 方向 | 例 |
|---|---|---|---|
| `caused_by` | A 的故障由 B 引起 | 故障→原因 | `bearing_failure caused_by lubrication_loss` |
| `led_to` | A 导致 B | 原因→结果 | `overload led_to motor_burn` |
| `symptom_of` | A 是 B 的症状 | 症状→故障 | `vibration symptom_of bearing_wear` |
| `affects` | A 影响 B | 部件→部件 | `seal_leak affects pump_pressure` |
| `part_of` | A 是 B 的组成部分 | 部件→系统 | `bearing part_of motor` |
| `has_component` | A 包含 B | 系统→部件 | `pump has_component impeller` |
| `has_symptom` | A 表现为 B | 故障→症状 | `bearing_wear has_symptom noise` |
| `repaired_by` | A 被 B 修复 | 故障→措施 | `leak repaired_by seal_replace` |
| `observed_by` | A 被 B(人/手段)发现 | 故障→人 | `fault observed_by technician:zhang` |
| `preceded_by` | A 发生在 B 之后(时序) | 事件→事件 | `restart preceded_by shutdown` |

**图遍历价值**:从 `bearing_failure` 出发 BFS,沿 `caused_by` 找根因,沿 `symptom_of` 反向找所有症状,沿 `part_of` 找所属系统——这正是故障诊断的推理路径。

---

## 3. 路径 A:结构化三元组直写

前置 agent 产出 JSON 三元组,直接以 envelope 入库,绕过抽取:

```json
POST /v1/experience
{
  "scope": "mech:plant1/line:A/user:diag",
  "modality": "imported",
  "content": {
    "kind": "triple",
    "triple": {
      "subject": {"kind":"entity","name":"轴承过热"},
      "predicate": "caused_by",
      "object": {"kind":"entity","name":"润滑不足"}
    }
  },
  "context": {"observed_at":"2026-06-01T00:00:00Z","intent":"diagnosis","labels":["bearing"]},
  "idempotency_key": "triple-bearing-heat-cause-1"
}
```

**批量**:多条三元组用 `/v1/import/jsonl`(每行一个 triple envelope),或 `/v1/experience/bulk`。

**当前实现状态**:schema 支持 `content.kind="triple"`(03 设计),但**抽取管线对 triple 类型当前走"跳过"分支**(non-text)。**需补**:triple 直写 handler——解析 envelope 的 triple 字段,直接建 entity + fact(带 valid_from),不经 LLM。这是要实现的部分。

---

## 3. 路径 B:事件回溯叙述(全文抽取,不切块)

几千字清洗后叙述,一次喂抽取 LLM,用**因果强化 prompt**:

```
POST /v1/experience
{
  "scope": "mech:plant1/line:A/user:diag",
  "modality": "document",
  "content": {"kind":"text","text":"<几千字事件回溯:6月1日电机异响,张工排查发现轴承过热,因润滑不足导致,更换润滑后恢复正常...>"},
  "context": {"observed_at":"2026-06-01T00:00:00Z","intent":"incident_retrospective","labels":["motor","bearing"]},
  "idempotency_key": "incident-2026-0601-motor"
}
```

**为什么不切块**:清洗后的叙述是"一个完整事件因果链",切块会切断"异响→排查→发现过热→因润滑→修复"的链。一次全文抽,LLM 能看到完整因果。Minimax-M3 / 现代模型 128K 上下文,几千字无压力。

**抽取 prompt 改造**(当前是通用 `owns/uses`,改为因果):
```
从机械故障诊断文本抽取因果三元组。predicate 必须用因果词表:
caused_by/led_to/symptom_of/affects/has_symptom/repaired_by/observed_by/preceded_by/part_of。
subject/object 是实体(故障/部件/人/症状/措施)。每条 fact 带 confidence。
输出 {entities:[{name,type,description}], facts:[{subject,predicate,object,confidence}]}。
```

**需实现**:extraction/pipeline.py 的 `_llm_extract` 支持按 modality/intent 选 prompt(诊断类用因果 prompt)。

---

## 4. 路径 C:机械结构长文档(按层级切块)

有标题章节的结构文档,按章节切,每块一个 `/experience`,块间用 `part_of` 连:

```
原文档:
# 电机系统
## 主轴
轴承型号 6208,润滑脂...
## 冷却系统
风扇...
```

切块(按 `^#{1,6}\s` 标题):
- chunk1: "电机系统"(总览)
- chunk2: "主轴"(部件描述)
- chunk3: "冷却系统"(部件描述)

每块入库:
```json
POST /v1/experience
{"scope":"mech:plant1/line:A/user:diag","modality":"document",
 "content":{"kind":"text","text":"# 电机系统\n电机系统包含主轴和冷却系统..."},
 "context":{"intent":"structure","labels":["motor_system"],"preceded_by":[]},
 "idempotency_key":"struct-motor-system"}
```

抽取时 prompt 要求:每块抽出 `X part_of 电机系统` / `电机系统 has_component X`,块间自然连成层级图。

**切块模块**(需实现):`src/cortex/chunking.py`
- 输入:长 markdown/text + 最小块字数(默认 200)
- 策略:按 `^#{1,6}\s` 标题切;无标题则按段落(双换行)切;段落过长(>2000字)再按句号切。
- 输出:`[{text, heading, path(如 "电机系统/主轴"), depth}]`
- **不 overlap**(机械结构块边界清晰,overlap 反而混淆);**不傻切固定窗口**(会切断部件描述)。

---

## 5. 修掉硬截断(token 预算制)

当前 B 类截断(会丢专业信息)改为按 LLM 上下文预算动态填:

| 位置 | 现状 | 改为 |
|---|---|---|
| `retrieval/pipeline.py:215` HyDE | `[:500]` | 不截(假设段落本就短)或按 `min(len, 2000)` |
| `retrieval/pipeline.py:420` context_block | `facts[:8]` | 按 token 预算填(从 rerank 后候选按相关度填到预算 70%) |
| `api/app.py:219` why event summary | `[:120]` | 不截(存全文 event.text) |
| `mcp_server.py:117` get_context | `facts[:10]` | 按 token 预算 |
| answer 路径 `facts[:6]` | 硬 6 条 | 按 token 预算(留 30% 给答案) |

**实现**:加 `src/cortex/token_budget.py`——`fit_to_budget(items, max_tokens, estimator)` 按相关度顺序填,超预算停。recall 的 `budgets.max_tokens` 已设计,接通即可。

---

## 6. 完整入库工作流(你的场景)

```
原始(聊天记录+图)
    │
    ▼ 前置 agent 预清洗
    │
    ├─ 结构化三元组 ─────► /import/jsonl(triple)────► 直写 facts(零损失)
    │
    ├─ 事件回溯叙述 ─────► /experience(document)────► 因果抽取全文 ► facts(因果链)
    │
    └─ 机械结构长文档 ──► chunking.py 按章节切 ──► /experience×N ──► 抽取 ► facts(part_of 层级)
                                                    │
                                                    ▼
                                              知识图谱(facts 为边)
                                                    │
                              ┌─────────────────────┼─────────────────────┐
                              ▼                     ▼                     ▼
                         图遍历找根因            recall 拉证据         answer 带引用
                         (caused_by BFS)       (events 原文溯源)      (cite fact_id)
```

**溯源**:每个 fact 的 `supports` 指回原始 event_id → 诊断结论可追溯到"哪条清洗后叙述/哪个原始聊天片段"。

---

## 7. 执行计划(依赖顺序)

| 步 | 任务 | 依赖 | 验收 |
|----|------|------|------|
| S1 | `token_budget.py` + 改 5 处 B 类截断 | 无 | 大 pack 不超预算;why 存全文 |
| S2 | triple 直写 handler(extract_event 识别 `content.kind="triple"` 直建 fact) | 无 | triple 入库不经 LLM,图谱有边 |
| S3 | 因果抽取 prompt(按 intent=diagnosis 选因果词表 prompt) | 无 | 叙述抽出的 predicate 是 caused_by 等 |
| S4 | `chunking.py`(按标题/段落切)+ API `/v1/ingest/document`(切块后批量入库) | S3 | 长文档切成多块,每块入库,part_of 连接 |
| S5 | 因果 vocabularies 预置(注册 §2 词表为 closed)+ 抽取时 coerce | S3 | predicate 自动归一到词表 |
| S6 | 诊断 demo 脚本(一段故障叙述 + 一个结构文档 + 几条 triple → 入库 → 图遍历找根因 → answer) | S1-S5 | 端到端跑通,因果链可遍历 |

全部做完后,你的前置 agent 产物按 §6 三条路径入库即可。

---

## 8. 你不需要担心的(系统已具备)

- **双时态**:故障发生时间(valid_from)vs 入库时间(recorded_from)分开,能回答"6月1日当时我们认为原因是什么"。
- **scope 隔离**:不同产线/设备 `mech:plant1/line:A` vs `mech:plant1/line:B` 图谱隔离。
- **beliefs 证据链**:`/beliefs/why` 给出"为什么判断是这个故障"的支持图(已实现 T28)。
- **erasures**:误入库的诊断可 GDPR 级删除(已实现 T33)。
- **MCP**:诊断 agent 可注册 cortex MCP,直接 `memory_store` 病例 + `memory_search` 找相似故障(已实现)。
