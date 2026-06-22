# 知识入库最佳实践：怎么写数据，才能被正确连接、被准确召回

> **目标读者**：往 cortex 写数据的前置 agent / 数据清洗管线 / 任何要沉淀经验的自动化脚本。
> **本文档定位**：讲**「喂什么样的内容、怎么组织内容」**，才能让系统最大限度提取有效信息、在图谱里正确连接、在召回时被准确找到。
> **不是 API 手册**：HTTP 端点怎么调、字段全表，见 [`GUIDE-INGEST.md`](./GUIDE-INGEST.md)。本文只在必要处引用字段名。
> **领域**：精密制造设备（刻蚀 / CVD / PVD 等）的故障诊断经验库。文中示例用通用工业语境，不绑定具体机种。

---

## 0. 先建立心智模型：你喂的数据会被系统怎么处理

无论走哪条入库路径，系统对你的数据做四件事，每一件都有"喂得好 / 喂得差"的差别：

| 系统行为 | 它在干什么 | 你写得好→ | 你写得差→ |
|---|---|---|---|
| **① 实体身份判定** | 判断"这个名词是不是已经存在过的那个实体"。按 `entity_type` 分层用上下文(fab/tool/chamber/recipe)+标识符(P-02/1500W)区分。 | 同一物理对象稳定归一；不同对象正确分离 | 不同腔体的同名传感器被错误合并；或同一对象被拆成两份 |
| **② 断言语义判定** | 给每条 fact 打 `assertion_status`(observed/hypothesized/confirmed/ruled_out)+`polarity`。**只有 confirmed 的因果边和 observed 的结构边会进因果图**。 | 真正确认过的根因能进图、能被图遍历找到 | 把"怀疑""可能"写成了已确认，污染因果链；或确认过的事没进图 |
| **③ 谓词闭集校验** | predicate 必须命中预置本体(`ontology.py`)。未命中的谓词**整体隔离**，不建实体、不建 fact，只在 event 上记一条诊断。 | 用标准谓词，信息无损入库 | 自创谓词(`magically_caused_by`)被静默丢弃，你以为存了其实没有 |
| **④ 双时态归档** | `observed_at`(事情发生时间)→ fact 的 `valid_from`；`recorded_at`(入库时间)单独存。单值谓词(如状态)新值超替旧值并闭合区间。 | 历史判断和当前判断分开，能查"当时我们知道什么" | 把发生时间和入库时间搞混；或状态变更丢失历史 |

**再加第五件——召回时**：系统用 6 个通道找数据(向量/BM25/图/实体名/同义/时间衰减)。其中 **BM25 和实体名通道是「子串匹配」**(中文用 `simple` 分词 + `ILIKE`)。这决定了**关键实体的名字必须原样、稳定地出现在文本里**，不能用代词或同义词替代。

> 一句话总结本文档的全部宗旨：**写得像一份给后人看的排查档案——对象有编号、关系有证据、时间有锚点、结论有把握程度。**

---

## 1. 三条入库路径，选对路径

| 你手里的数据形态 | 用哪条路径 | 为什么 |
|---|---|---|
| 你已经推理出明确的因果关系/结构关系(机器可读) | **路径 A：triple 直写** `kind=triple` | 零损失、不经 LLM、`trusted=True`(确认型因果可直接进图) |
| 一段清洗过的故障叙述 / 排查经过(自然语言) | **路径 B：自然语言抽取** `kind=message/text` + `intent=diagnosis` | LLM 抽实体+关系，适合因果链完整的叙述 |
| 一篇有章节标题的结构文档(设备说明 / 工艺规范) | **路径 C：文档切块** `/v1/ingest/document` | 按 markdown 标题切，块间自动 `part_of` 连接 |
| 一张传感器/部件关系表(CSV/Excel) | **路径 A 批量** `/v1/import/jsonl` | 每行一条 triple，结构化灌入 |

> **路径选择的经验法则**：能结构化就结构化(路径 A)。triple 直写是"零损传递"——你写什么就存什么，且确认型因果可直接进图。自然语言路径(路径 B)有 LLM 抽取损耗(可能漏抽、可能改写名字)，适合你只有叙述、无法逐条结构化的场景。

---

## 2. 实体命名最佳实践(影响①身份判定 + ⑤召回)

实体命名是入库质量的第一关。名字决定了"能不能和已有实体合并"和"能不能被召回"。

### 2.1 五条命名铁律

1. **带标识符**：传感器 / 阀门 / 控制器 / 加热器的**编号必须写进名字**：`T-101`、`P-02`、`MFC-1`、`V-3`、`H-1`。
   - 系统会从名字里提取这些 token(`P-02`、`MFC-1`...)作为"关键身份标识"，用来阻止错误合并。写成 `温度传感器` 这种没编号的泛名，系统无法区分它和别的温度传感器。
2. **带部位**：故障 / 征兆带部位前缀：`腔体压力异常`(≠`管路压力异常`)、`MFC响应延迟`(≠`阀门响应延迟`)。
3. **数值参数=实体名带量纲**：`射频功率1500W`、`腔体压力3mTorr`、`气体流量A:50sccm`。系统会提取量纲 token，`射频功率1500W` 和 `射频功率1600W` 因此被判为不同参数(不会错误合并)。
4. **全文同一名字**：同一个对象在一条叙述里、在整批数据里，**始终用同一个名字**。简称/全称/昵称混用会让实体链接失败(系统不知道"张工"和"老张"是同一人，除非走灰区 LLM 判定，那有额外开销且可能判错)。
5. **给 `type` 和 `description`**：triple 的 `subject.type`/`object.type` 必须给对(LLM 路径由 prompt 抽)。`type` 直接决定身份分层(见 §3)。每个实体写一句 description(是什么、在哪、干什么用)。

### 2.2 实体类型速查(type 必须命中其一)

| 大类 | 类型 | 命名要点 |
|---|---|---|
| **物理/配置层** | `equipment` `subsystem` `module` `chamber` `component` `sensor` `controller` | 带编号、带层级 |
| | `process_param` `process_step` `recipe` | 参数带量纲、步骤用规范名 |
| | `material` `phenomenon` `chamber_state` `metrology_result` | 规范名/现象描述/偏差描述 |
| **故障层** | `fault` `symptom` `signal_pattern` | 部位+异常 / 可量化征兆 / 信号模式 |
| **诊断推理层** | `hypothesis` `evidence` `diagnostic_action` `correlation` `measure` | 排查产物，带推断语气 |
| | `person` `historical_ref` | 人 / 历史案例引用 |

> 数值本身不是实体——`80度` 是某条 fact 的 literal 值，不是实体。但 `基底温度80度` 作为 process_param 实体是合理的(它是"被配置成 80 度的那个参数位")。

### 2.3 命名正反例

| ✅ 好 | ❌ 坏 | 为什么坏 |
|---|---|---|
| `温度传感器T-101` | `温度传感器` | 无编号，无法和其他温度传感器区分；identifier token 提取不到 |
| `腔体压力异常` | `压力异常` | 无部位，和管路/气路压力异常混淆 |
| `射频功率1500W` | `射频功率`(单独) | 无量纲 token，无法和 `射频功率1600W` 区分 |
| 全文用 `密封圈老化` | 前文"密封圈老化"后文"O-ring劣化" | 同一对象两个名字，实体链接可能拆成两份 |
| `怀疑MFC校准漂移`(type=hypothesis) | `MFC校准漂移`(无 type 或 type=fault) | hypothesis 必须标 type，否则被当故障事实 |

---

## 3. context 字段最佳实践(影响①身份判定——**最关键**)

这是和普通知识图谱最大的区别：**同名实体会按物理上下文被分开**。喂传感器/部件/参数时，`context` 字段填什么，直接决定图谱连对了还是连糊了。

### 3.1 context 的 6 个字段

```json
"context": {
  "observed_at": "2026-06-01T10:00:00Z",   // 必填：事情发生时间(不是入库时间)
  "intent": "diagnosis",                    // 推荐：diagnosis/structure/incident_retrospective 触发因果 prompt
  "fab": "FAB1",                            // 厂
  "equipment": "PM1",                       // 机台(别名：tool)
  "module": "Etch",                         // 模块
  "chamber": "C1",                          // 腔体
  "recipe": "MainEtch",                     // 配方
  "recipe_revision": "R2"                   // 配方版本(别名：recipe_rev)
  // labels 可选：自由标签，用于检索过滤
}
```

> `tool` 是 `equipment` 的别名，`recipe_rev` 是 `recipe_revision` 的别名，填哪个都行。字段值会做大小写/全半角/空格归一(`FAB 1` = `fab1` = `ＦＡＢ１`)。

### 3.2 系统按 entity_type 决定用哪些 context 字段做身份区分

这是核心规则，**必须记住**：

| 实体 type | 参与身份区分的 context 字段 | 含义 |
|---|---|---|
| `equipment` / `tool` | `fab`, `equipment` | 同一机台号在不同厂是两个对象 |
| `module` `chamber` `component` `sensor` `subsystem` | `fab`, `equipment`, `module`, `chamber` | **PM1 的传感器 T-101 和 PM2 的 T-101 是两个对象** |
| `recipe` `process_step` `process_param` | 全部 6 个字段 | 不同配方/腔体下的"主工艺步骤"是不同对象 |
| `fault` `symptom` `material` `person` `hypothesis` 等其余 | **不区分**(跨上下文允许合并) | "腔体压力异常"是个通用概念，不属于某一台机台 |

### 3.3 context 正反例

**场景**：你有两台同型号线(PM1、PM2)，每台都有编号 `T-101` 的温度传感器。

✅ **好**(分对了——这是两个真实不同的传感器)：
```json
// 第一条
{"name": "T-101", "type": "sensor", ...}
"context": {"fab":"FAB1", "equipment":"PM1", "chamber":"C1", "observed_at":"..."}
// 第二条
{"name": "T-101", "type": "sensor", ...}
"context": {"fab":"FAB1", "equipment":"PM2", "chamber":"C1", "observed_at":"..."}
// → 系统判定为两个不同实体(因为 equipment 不同)。正确。
```

❌ **坏**(漏 context——两台机器的 T-101 被错误合并成一个)：
```json
// 两条都只写
{"name": "T-101", "type": "sensor"}
"context": {"observed_at":"..."}
// → context_key 都是 "{}"，向量相近 → 合并成一个实体。污染了所有涉及 T-101 的因果链。
```

❌ **坏**(过度带 context——把通用故障概念拆碎了)：
```json
{"name": "腔体压力异常", "type": "fault"}
"context": {"fab":"FAB1", "equipment":"PM1", "chamber":"C1", ...}
// → fault 类型不参与 context 区分，但如果你试图用 context 人为隔离开它，
//   后果是"腔体压力异常"这个通用故障模式无法在跨机台的相似案例间被关联召回。
//   fault/symptom 这类概念实体就该让它跨上下文共享。
```

> **判断准则**：这个对象是**物理上独立存在的一个实物/一个参数位**吗(sensor/component/chamber/param)？→ **必须带全 context**。它是一个**通用概念/现象/人**吗(fault/symptom/material/person/hypothesis)？→ **不要靠 context 去隔离它**，靠 scope 隔离更合适。

### 3.4 跨厂/跨机台同名的处理

如果你的数据天然是"多厂多机台"的，**优先用 scope 隔离**(见 §6)，而不是靠 context。scope 是物理隔离(完全不连通)，context 是逻辑归一(同对象合并、异对象分开)。两者搭配：

- 不同产线的图谱完全独立 → 用 scope(`mech:plant1/line:A` vs `mech:plant1/line:B`)
- 同一产线内、不同机台的同编号部件 → 用 context 区分

---

## 4. 谓词选择(影响③闭集校验 + 图遍历)

### 4.1 谓词是闭集——只能用预置的

系统预置了完整诊断本体(见 [`src/cortex/ontology.py`](../src/cortex/ontology.py))。**不在本体里的谓词会被整体隔离**：不建实体、不建 fact，只在 event 上记一条 `unknown_closed_predicate` 诊断。你以为存进去了，实际没有。

**本体里的谓词分类**：

| 类别 | 谓词(选一) | 用途 |
|---|---|---|
| **结构/配置** | `part_of` `has_component` `installed_on` `located_in` `monitored_by` `controlled_by` `regulates` `configured_as` `depends_on` | 静态拓扑，进结构图 |
| **因果/级联** | `caused_by` `led_to` `cascades_to` `affects` `triggers` `contributes_to` `correlates_with` `suggests` `symptom_of` `has_symptom` | 故障传播，**只有 confirmed 进因果图** |
| **诊断推理** | `detected_by` `investigates` `investigated_by` `checked` `found` `normal` `ruled_out` `no_correlation` `supports` `contradicts` `refines_to` `alternative_to` `confirmed_by` `repaired_by` `observed_by` `references` `preceded_by` `drifts_from` `measured_as` `deviates_from` `feedback_to` | 排查过程与证据 |
| **状态** | `has_status` `deal_stage` | **单值超替** |

### 4.2 谓词选择正反例

| ✅ 好 | ❌ 坏 | 改法 |
|---|---|---|
| `T-101 installed_on 腔体壁` | `T-101 装在 腔体壁` | `装在` 不在闭集 → 隔离；用 `installed_on` |
| `轴承过热 caused_by 润滑不足` | `轴承过热 因为 润滑不足` | `因为` 不在闭集；用 `caused_by` |
| `MFC-1 流量偏差 correlates_with 刻蚀速率漂移` | `MFC-1 关联 刻蚀速率漂移` | 用 `correlates_with` |
| `密封失效 repaired_by 更换密封圈` | `密封失效 修复方式 更换密封圈` | 用 `repaired_by` |

> **triple 直写也一样受闭集约束**：`_direct_write_triple` 会先 `coerce` 谓词，未知谓词整条隔离。不要以为直写就能自创谓词。

### 4.3 因果谓词的 cardinality

绝大多数谓词是**多值**(`caused_by` 可以有多个根因、`has_symptom` 可以有多个征兆)，允许多条共存。只有 `has_status`、`deal_stage` 这类**单值**谓词是"新值超替旧值"。所以放心地给一个故障标多条 `caused_by`——它们不会被当冲突。

---

## 5. 断言与证据(影响②断言语义——决定因果能不能进图)

这是最容易踩坑、也是诊断价值最高的一环。系统对每条 fact 自动判 `assertion_status`，规则是：

### 5.1 断言判定规则(系统自动执行)

对**因果类谓词**(`caused_by`/`led_to`/`cascades_to`/`affects`/`triggers`/`contributes_to`/`correlates_with`/`suggests`/`symptom_of`/`has_symptom`)：

| 你写的内容 | 系统判定 | 进因果图? |
|---|---|---|
| 纯叙述因果，没标 status，没给证据 | **hypothesized**(假设) | ❌ 不进 |
| 标了 `assertion_status=confirmed` + 有 `evidence_span` + 该 evidence 原样出现在原文里 | **confirmed** | ✅ 进 |
| 标了 confirmed 但没有 evidence，或 evidence 不在原文里(LLM 路径) | 降级为 **hypothesized** | ❌ 不进 |
| triple 直写标了 confirmed(路径 A，`trusted=True`) | **confirmed**(不需要原文证据，因为你已结构化) | ✅ 进 |
| 标了 `negation=true` | **ruled_out**(否定) | ❌ 不进 |

对**排除/对立谓词**(`ruled_out`/`no_correlation`/`contradicts`)：永不进正向图(它们是"排除项"，不是"存在的关系")。

对**结构/状态谓词**(`part_of`/`installed_on`/`has_status` 等)：默认 **observed**，进结构图。

### 5.2 这意味着什么——怎么写才能让根因进图

**场景**：你排查后确认"腔体压力异常的根因是密封圈老化"，希望这条因果能被下游 agent 沿图遍历找到。

✅ **路径 A(triple 直写)——最直接**：
```json
{"subject":{"name":"腔体压力异常","type":"fault"},
 "predicate":"caused_by",
 "object":{"name":"密封圈老化","type":"material"},
 "assertion_status":"confirmed",
 "evidence_span":"更换密封圈后压力恢复，复装旧件后故障再次出现"}
```
→ trusted=True，confirmed 直接进图。

✅ **路径 B(自然语言)——必须在叙述里原样写出证据句**：
```
腔体压力异常，排查后确认根因为密封圈老化：更换密封圈后压力恢复，
复装旧件后故障再次出现。   ← 这句原样出现在 text 里
```
配合 fact 上 `assertion_status=confirmed` + `evidence_span="更换密封圈后压力恢复，复装旧件后故障再次出现"`。系统校验 evidence_span 原文命中后才认 confirmed。

❌ **坏**(自然语言路径，标 confirmed 但没证据)：
```
腔体压力异常，应该是密封圈老化吧。
```
+ `assertion_status=confirmed` 但没 evidence_span → 系统降级为 hypothesized，**不进图**。下游 agent 沿因果链找不到这条。

### 5.3 把"怀疑"和"确认"分开存

诊断是迭代的：先有一堆假设，逐步排除，最后确认一两个。**把这些都记下来**，不要只记最终结论——被排除的假设同样有价值(下次遇到相似征兆，能查到"上次已经排除过射频系统")。

```
# 假设(进诊断推理图，不进因果图)
怀疑射频系统故障 --ruled_out--> 射频系统
检查射频系统 --normal--> 射频系统
怀疑MFC校准漂移 --refines_to--> 怀疑MFC-1响应延迟

# 最终确认(进因果图)
腔体压力异常 --confirmed_by--> 更换密封圈复现验证
```

---

## 6. Scope 设计(物理隔离)

Scope 是最强隔离——不同 scope 的图谱完全不连通。设计原则：

```
org:fab1                          ← 全厂通用知识(设备目录、通用规范、通用故障模式)
org:fab1/etch:PM1                 ← PM1 机台专属(该机台的部件结构、传感器布局、配置)
org:fab1/etch:PM1/user:diag       ← PM1 诊断 agent 的记忆(案例、推理过程)
org:fab1/etch:PM2                 ← PM2(独立图谱，和 PM1 不连通)
```

- **通用知识**(故障模式、材料特性、通用工艺原理)→ 放高层级 scope，让所有机台用 `view=holistic` 都能召回。
- **机台专属**(具体某台的部件、传感器、配置、案例)→ 放该机台 scope。
- **不同机台的同编号部件**(PM1 的 T-101 vs PM2 的 T-101)→ 优先靠 scope 隔离(各自 scope)，其次靠 context(§3)。
- 读取时：`view=local`(只当前 scope)、`view=holistic`(当前+祖先，诊断最常用)、`view=descend`(当前+后代，管理视角)。

---

## 7. 双时态最佳实践(影响④归档)

### 7.1 两个时间别搞混

| 字段 | 含义 | 取自 |
|---|---|---|
| `observed_at` | **事情发生的时间**(故障发生、排查进行、参数生效) | `context.observed_at` |
| `recorded_at` | **入库时间**(你把这条数据喂给系统的时间) | 系统自动填 |

**永远把 `observed_at` 填成事情真正发生的时间**，哪怕是三年前的旧案例。这样系统能支持"2024年6月当时我们知道什么"这类时间旅行查询(`as_known`)。如果你不填 observed_at 或填成 now，历史案例的时间语义就丢了。

### 7.2 fact 的 valid_from / valid_to

- `valid_from`：这条 fact 何时开始为真(默认 = observed_at)。
- `valid_to`：何时停止为真(可选)。比如"PM1 状态=down"从 6/1 10:00 到 6/1 14:00 修复。
- **单值状态谓词**(`has_status`)：写新状态时系统自动闭合旧状态的 valid_to。即使新数据比旧数据**晚到**(迟到事件)，系统也会正确回填前驱区间，不会破坏当前 belief。
- **多值谓词**(`caused_by` 等)：不超替，允许多条共存。

### 7.3 时间正反例

✅ **好**：录入一个 2024 年的旧案例：
```json
"context": {"observed_at": "2024-08-15T09:30:00Z", ...}
```
今天(2026)录入，recorded_at=2026，但 valid_from=2024-08-15。能被"2024 年发生了什么"的查询召回。

❌ **坏**：旧案例却填 `observed_at = now`，历史案例和新案例的时间混在一起，时间衰减通道会把旧案例当新的优先召回。

---

## 8. 召回视角的写作(影响⑤能否被找到)

写数据时换一个视角想：**未来 agent 会用什么关键词来搜这条数据**？系统靠这些通道找数据：

- **BM25 / 实体名通道**：子串匹配(`simple` 分词 + `ILIKE`)。关键词**原样**出现在 `predicate`/`object_value`/实体名里才能命中。
- **向量通道**：语义相似。
- **图通道**：沿 confirmed 因果边 / observed 结构边 BFS。
- **时间衰减**：按 observed_at 衰减。
- **同义**：预置同义词扩展(可配)。

### 8.1 写作铁律(为了被召回)

1. **关键实体名原样出现，不要用代词替代**。
   - ✅ "T-101 温度缓慢漂移 5 度。T-101 的历史曲线显示..."
   - ❌ "T-101 温度缓慢漂移 5 度。**它**的历史曲线显示..."(第二个 T-101 没了，BM25 命中权重下降)
2. **保留领域专有名词原形**，不要过度改写成口语。
   - ✅ "EPD 信号斜率偏离基准 15%"
   - ❌ "终点检测的那个信号有点不对"(EPD、斜率、15% 这些召回关键词全丢了)
3. **数值和量纲原样写**：`1500W`、`3mTorr`、`50sccm`。这些既是身份 token 也是召回关键词。
4. **给实体挂别名**：如果某个对象有多个叫法(全称/简称/俗称)，用 `/v1/entities/{id}/aliases` 或 alias 通道登记，召回时都能命中。
5. **结构性关系要显式写**：想让"从 T-101 出发找到腔体壁"成立，就得有 `T-101 installed_on 腔体壁` 这条 fact。图通道只能沿已存在的边走。

### 8.2 召回正反例

假设未来 agent 会查"T-101 为什么漂移"。

✅ **好叙述**(能被多通道召回 + 图遍历)：
```
T-101 温度传感器缓慢漂移(72h内5度)。T-101 installed_on 腔体壁。
检查温控系统发现 T-101 漂移由温度PID参数失调引起(已确认：调整PID增益后
T-101 恢复稳定，复现验证)。T-101漂移 caused_by 温度PID参数失调。
```
→ BM25 命中"T-101"；实体名命中；图通道能从 T-101 沿 caused_by 到 PID；confirmed 有证据进图。

❌ **坏叙述**(召不回 + 图断)：
```
有个温度传感器不太准，应该是控制那块的参数有问题吧。
```
→ "T-101" 没出现；"PID" 没出现；没结构边；没证据→不进图。这条经验等于没存。

---

## 9. 按数据类型的最佳样本

### 9.1 历史故障案例(最常见——叙述型)

**用路径 B**(`kind=message`, `intent=incident_retrospective` 或 `diagnosis`)。

**最佳样本结构**(一段叙述里包含这 6 个要素)：
1. **时间地点**：何时、哪台机台、哪个腔体(`observed_at` + `context`)。
2. **征兆**：可观测现象(具体数值)，挂 `symptom` 类型，`detected_by` 哪个传感器。
3. **排查链**：假设→检查→发现→排除/细化(`hypothesis`/`diagnostic_action`/`evidence`/`ruled_out`/`refines_to`)。
4. **根因**：最终确认的原因，**原样写出确认证据**(更换/复现/对比)，`caused_by` + `confirmed` + `evidence_span`。
5. **级联**：跨子系统传播链(`cascades_to`)。
6. **修复**：措施 + 验证(`repaired_by` + 验证结果)。

**样本**：
```
2026-06-15 10:00，PM1 腔体C1。P-02 压力传感器读数波动 ±0.5mTorr（detected_by P-02）。
怀疑真空系统泄漏（hypothesis）。检查真空系统密封性，发现密封圈表面有裂纹（found）。
同时排查射频系统，RF 反射功率正常，射频系统排除嫌疑（ruled_out 射频系统）。
确认根因：密封圈老化导致气体泄漏（caused_by 密封圈老化）。证据：更换密封圈后
压力恢复稳定，复装旧裂纹件后波动再次出现。
级联：气体泄漏 cascades_to 压力波动 cascades_to 等离子不稳定 cascades_to 均匀性偏差。
修复：更换密封O-ring（repaired_by）。验证：连续 3 批 wafer 均匀性回归规格内。
```
配合 `context={fab, equipment:PM1, chamber:C1, observed_at, intent:incident_retrospective}`。

### 9.2 设备结构知识(文档型)

**用路径 C**(`/v1/ingest/document`, `intent=structure`)。markdown 按标题分层：

```markdown
# 刻蚀系统PM1
刻蚀系统包含气体输送系统、温控系统、射频系统、真空系统。

## 气体输送系统
气体输送系统包含 MFC-1、MFC-2、气体管路。
MFC-1 控制工艺气体A流量，量程 100sccm。

## 温控系统
温控系统包含加热器H-1、温度PID、温度传感器T-101。
T-101 安装在腔体壁，量程 0-300度。
```
→ 系统按标题切块，自动抽 `MFC-1 part_of 气体输送系统`、`T-101 installed_on 腔体壁` 等结构边。

**要点**：先灌结构知识(建立部件层级图)，再灌故障案例(抽取的实体会链接到已有部件)。

### 9.3 传感器组关系(表格→批量 triple)

**用路径 A 批量**(`/v1/import/jsonl`)。每行一条 triple envelope：

```jsonl
{"type":"triple","subject":{"name":"T-101","type":"sensor"},"predicate":"installed_on","object":{"name":"腔体壁","type":"component"},"observed_at":"2026-01-01T00:00:00Z"}
{"type":"triple","subject":{"name":"T-101","type":"sensor"},"predicate":"monitored_by","object":{"name":"腔体温度","type":"process_param"},"observed_at":"2026-01-01T00:00:00Z"}
{"type":"triple","subject":{"name":"P-02","type":"sensor"},"predicate":"installed_on","object":{"name":"腔体壁","type":"component"},"observed_at":"2026-01-01T00:00:00Z"}
```
**关键**：每行 envelope 的 context 或 record 里要带 `fab/equipment/chamber`，否则不同机台的同编号传感器被合并(§3)。用 `scope_template` 按 record 字段自动分 scope 更稳。

### 9.4 工艺配方 / 参数

**用路径 A**。参数实体名带量纲，步骤用规范名，`configured_as` 连接：

```json
{"subject":{"name":"主工艺步骤","type":"process_step"},
 "predicate":"configured_as",
 "object":{"name":"射频功率1500W","type":"process_param"}}
```
**关键**：`process_step`/`process_param` 参与全 context 区分——不同配方/腔体下的"主工艺步骤"是不同实体。配方上下文要在 context 里写全。`射频功率1500W` 和 `射频功率1600W` 因量纲 token 不同自动分离。

### 9.5 排查推理过程

**用路径 A 或 B**。把假设、证据、排除、细化都记下来(§5.3)：

```
怀疑气体输送系统 refines_to 怀疑MFC-1响应延迟      # 假设细化
对比MFC设定值 checked MFC-1实测流量                 # 排查动作
检查MFC响应 found MFC-1流量阶梯式偏差               # 发现
MFC-1校准合格 contradicts 怀疑MFC校准漂移           # 证据反驳假设
怀疑MFC-1响应延迟 confirmed_by MFC阶跃响应测试      # 确认
```
→ 形成完整诊断推理图。下次相似征兆能查到"上次查过 MFC、校准合格、最终确认是响应延迟"。

### 9.6 零散知识点(一句话判断)

**用路径 B 短叙述**：
```json
{"kind":"text","text":"腔体积碳(seasoning漂移)会导致刻蚀速率渐变，是渐变型故障的常见隐性根因。"}
```
+ `intent=diagnosis`。LLM 抽成 `腔体积碳 affects 刻蚀速率`。即使没确认证据(降为 hypothesized 不进因果图)，BM25/向量通道仍能召回——零散知识的价值主要在召回，不在图遍历。

---

## 10. 反模式清单(别这么干)

1. **不带 context 喂传感器/部件** → 不同机台同编号部件错误合并。✅ 修：sensor/component/chamber/param 必须带 fab/equipment/chamber/recipe。
2. **自创谓词**(`因为`/`装在`/`关联`/`修复方式`) → 整条隔离，静默丢失。✅ 修：只用本体里的英文谓词。
3. **把怀疑写成确认** → 假设污染因果图。✅ 修：怀疑用 `hypothesis` + 因果谓词不标 confirmed(自动 hypothesized 不进图)；确认才标 confirmed + 证据。
4. **确认因果不给证据**(LLM 路径) → 降级 hypothesized 不进图。✅ 修：原文里原样写出确认证据句，并填 `evidence_span`。
5. **用代词替代实体名** → BM25/实体名通道召不回。✅ 修：T-101、P-02 等关键名原样重复出现。
6. **同一对象多个名字** → 实体拆分，图谱断裂。✅ 修：全文统一命名；有多叫法就登记 alias。
7. **observed_at 填 now 录旧案例** → 时间语义错乱。✅ 修：填事情真实发生时间。
8. **fault/symptom 靠 context 人为隔离** → 通用故障模式无法跨案例关联。✅ 修：概念实体不带 chamber context，跨案例共享靠 scope。
9. **把数值当实体**(`80度` 当实体) → 命名混乱。✅ 修：数值是 literal；参数位才是实体(`基底温度` process_param，其 `configured_as` 值是 80度)。
10. **结构边不显式写** → 图遍历断链。✅ 修：`installed_on`/`part_of`/`monitored_by` 等显式建。

---

## 11. 喂入前自检清单

每批数据入库前，过一遍这个清单：

- [ ] **命名**：传感器/部件/控制器带编号？故障/征兆带部位？参数带量纲？全文同一对象用同一名字？
- [ ] **type**：每个实体标了正确的 type？(尤其 hypothesis 要标 hypothesis 别标 fault)
- [ ] **context**：sensor/component/chamber/process_param/process_step/recipe 带了 fab/equipment/chamber/recipe？fault/symptom/material/person 没被 context 误隔离？
- [ ] **谓词**：全部用本体里的英文谓词？没有自创？
- [ ] **断言**：确认的因果标了 confirmed + evidence_span(且证据原样在原文)？怀疑的没误标 confirmed？
- [ ] **时间**：observed_at 是事情发生时间(不是 now)？旧案例填了真实历史时间？
- [ ] **召回**：关键实体名在叙述里原样出现(没用代词替代)？结构性关系显式写了？
- [ ] **idempotency_key**：每条唯一？(同 key 同 body 幂等，同 key 异 body 会 409)
- [ ] **scope**：机台专属数据放机台 scope？通用知识放高层 scope？

---

## 12. 一页速查(打印贴墙)

```
命名：带编号(T-101) + 带部位(腔体压力异常) + 带量纲(1500W) + 全文统一
type：sensor/component/chamber/param → 必带 context(fab/equipment/chamber/recipe)
      fault/symptom/material/person → 不靠 context 隔离(靠 scope)
谓词：只用本体英文谓词(caused_by/installed_on/configured_as/...)，禁自创
断言：确认因果 = confirmed + evidence_span(原文原句)；怀疑 = 不标 confirmed(自动 hypothesized)
时间：observed_at = 事情发生时间(旧案例填真实历史时间)
召回：关键实体名原样重复出现，别用代词；结构关系显式建(installed_on/part_of)
路径：能结构化 → triple 直写(A)；只有叙述 → message+intent=diagnosis(B)；
      结构文档 → /ingest/document(C)；关系表 → /import/jsonl(A批量)
scope：机台专属→机台scope；通用知识→高层scope；跨机台隔离→不同scope
幂等：每条 idempotency_key 唯一
```

---

## 附：与操作手册的关系

| 你想做的事 | 看哪份文档 |
|---|---|
| 理解系统为什么这样要求(命名/context/断言背后的机制) | **本文档** |
| HTTP 端点怎么调、字段全表、curl 模板 | [`GUIDE-INGEST.md`](./GUIDE-INGEST.md) |
| 一个完整诊断事件样板(字段级模板) | [`TEMPLATE-DIAGNOSIS.md`](./TEMPLATE-DIAGNOSIS.md) |
| 系统整体能力、5 层架构、API 全貌 | [`USAGE-GUIDE.md`](./USAGE-GUIDE.md) |
| 谓词/实体类型完整定义(权威来源) | [`src/cortex/prompts.py`](../src/cortex/prompts.py) + [`src/cortex/ontology.py`](../src/cortex/ontology.py) |
