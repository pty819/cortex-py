# 07 — Stage 7 PRD + 测试规范(剩余全部功能)

> **定位**:实现 PRD `06` 中推迟的 C 档 + erasures 完整 + episodes segmenter + vocab CRUD。
> 每项含**可执行验收**(测试脚本断言),实现后跑回归(stage0 + stage6 + smoke + 本档)。
> **不修改** `01` 选型 / `03 §11` 决策 / 已验证的 Stages 1-6 行为(回归保证)。

---

## 1. Erasures(GDPR 引用计数真删,4 阶段)

### PRD
- `POST /v1/erasures/preview` {scope, selector} → enumerate 命中 events,算 refcount(被 facts/beliefs.supports 引用数) → 产 manifest(逐行 delete vs redact)+ estimated_affected + refcount_breakdown。落 erasure_jobs(phase=enumerate, preview_id, manifest)。24h 过期。
- `GET /v1/erasures/preview/{preview_id}/manifest` → 返回 manifest。
- `POST /v1/erasures` {scope, selector, from_preview_id?} → execute:逐 event,refcount>0→redact(清 content+excluded_from_recall),=0→delete;`array_remove` 清 supports;blob refcount=0→删。phase completed。emit erasure_progress/complete。
- `GET /v1/erasures/{id}` → status(phase/progress)。
- `POST /v1/erasures/{id}/cancel` → phase=cancelled(只在阶段边界生效)。
- MVP **单 scope**,跳 cross_workspace / legal_hold(`05 §2.4 E1`)。

### 验收(erasures)
1. 灌数据:1 个被 fact 引用的 event EVT_A + 1 个无引用的孤立 event EVT_B。
2. `POST /preview` selector 命中两者 → manifest 标 EVT_A=redact、EVT_B=delete;refcount_breakdown 正确。
3. `GET /manifest/{id}` 返回 manifest;过期 manifest(改 created_at)→ execute 返回 409。
4. `POST /erasures` execute 后:EVT_A 行在但 content={} excluded_from_recall=true;EVT_B 物理删;supports 数组已 array_remove;`/facts` 不再返回引用 EVT_A 的 fact(或 fact.supports 已清)。
5. `GET /erasures/{id}` phase=completed,progress.deleted/redacted 计数对。

---

## 2. Episodes segmenter(有界事件序列)

### PRD
- `segment_scope(scope, since?)`:扫 events(order observed_at),按**时间窗**(相邻间隔 > 30min 封存)分组;每组建 episode(event_ids、actors=observed_actor 去重、causal_chain 从 context.preceded_by 推 [{from,to,relation:'precedes'}]、started_at/ended_at、sealed=true)。
- worker job_type=`segment`(ingest 后低优先级 enqueue;也可 `POST /episodes/build` 手动触发)。
- `GET /v1/episodes?scope=` → 列表;`POST /v1/episodes/build {scope}` → 触发 → {built, items[]}。

### 验收(episodes)
1. 灌 3 events:ev1@10:00、ev2@10:10、ev3@12:00(超 30min 间隔);ev2.context.preceded_by=[ev1]。
2. `POST /episodes/build` → 产 2 个 episode([ev1,ev2] / [ev3]),sealed=true。
3. episode1.actors 含 observed_actor;episode1.causal_chain 含 {from:ev1,to:ev2}。
4. `GET /episodes?scope=` 返回 2 条。

---

## 3. Vocabularies CRUD

### PRD
- `POST /v1/vocabularies` {scope, name, kind, values:[{canonical, aliases[]}]} → 建(+ values)。
- `GET /v1/vocabularies?scope=` → 列表(含 values);`GET /v1/vocabularies/{name}?scope=` → 单个。
- `PUT /v1/vocabularies/{name}?scope=` {kind?, values[]} → 替换 values(删旧建新)。
- `DELETE /v1/vocabularies/{name}?scope=` → 204(facts 保留已抽取值,`05 §2.2 V3`)。
- coerce(已实现)在新抽取时生效。

### 验收(vocab)
1. `POST` 建 closed 词表 deal_stage values=[{signed,[won,签约]}]。
2. `GET /{name}` 返回 values。
3. `PUT` 替换 values=[{poc,[pitch]}];GET 确认。
4. coerce('签约') 在 PUT 后仍→signed?否(已删)→ coerce('pitch')→poc。
5. `DELETE` → 204;再 GET → 404。

---

## 4. Memory evolution(methylation + consolidation)

### PRD
- **methylation**:worker job_type=`methylation`。events.access_count=0 且 observed_at 早于阈值(默认 30 天)→ `methylated_at=now()` + `excluded_from_recall=true`(软剪枝,可逆:清 methylated_at 即恢复)。**不删 WAL**。
- **consolidation**:worker job_type=`consolidate`。同 (scope,subject,predicate,object) 的重复 facts → 保留最新(最大 valid_from),其余 recorded_to=now()(软关,`05 §4.2 F1` 同语义)。
- 触发:`POST /v1/admin/maintenance {action: methylation|consolidation, scope, older_than_days?}` 或 worker 周期 enqueue。
- 配置:worker.methylation_inactivity_days(默认 30)、consolidation_min_age_hours(默认 24)。

### 验收(evolution)
1. 灌一旧 event(observed_at=60天前, access_count=0)+ 一新 event。
2. `POST /admin/maintenance {action:methylation, scope, older_than_days:30}` → 旧 event methylated_at+excluded_from_recall=true;新 event 不动。
3. recall 不再返回旧 event 的 facts(被 excluded)。
4. consolidation:同 subject+predicate+object 两条 fact → run 后一条 recorded_to 闭合,recall 去重。

---

## 5. Temporal phrases(NL 时间解析)

### PRD
- 表 `temporal_phrases(name PK, anchor timestamptz, expression text, scope, created_at)`;expression = 两 ISO8601 duration 以 `..` 隔,相对 anchor(如 `last week` → `-P7D..P0D`)。
- `POST /v1/temporal/phrases` {name, anchor?, expression} → 注册(anchor 默认 now())。
- `GET /v1/temporal/phrases` → 列表。`DELETE /v1/temporal/phrases/{name}` → 204。
- 内置默认:`last week`(-P7D..P0D)、`this month`(当月)、`yesterday`。
- 解析器 `parse_temporal(natural, reference_date)`:词表命中 → (from,to);recall 的 `temporal.natural` → 转 as_of/valid_during 过滤。

### 验收(temporal)
1. `GET /temporal/phrases` 含内置 `last week`。
2. `POST` 注册 `last quarter`(expression=`-P3M..P0D`)。
3. `parse_temporal('last week', 2026-06-18)` → from≈2026-06-11。
4. recall 带 `temporal.natural='last week'`(reference_date=2026-06-18)→ 只返回该窗内 facts(用种子时间构造)。

---

## 6. Admin / metrics

### PRD
- `GET /v1/admin/metrics?scope=` → JSON {events, facts, beliefs, jobs_by_status, entities, episodes, blobs}。
- `GET /v1/admin/version` → {version, schema_tables}。
- `/v1/health` 已有。

### 验收(admin)
1. `GET /admin/metrics` 200,含 jobs_by_status dict。
2. `GET /admin/version` 200。

---

## 回归测试矩阵(实现后全跑)

| 套件 | 命令 | 预期 |
|------|------|------|
| Stage 0 SQL 冒烟 | `scripts/stage0/run_all.sh` | 37 PASS / 0 FAIL |
| Stage 6 verify | `uv run python scripts/verify_stage6.py` | 14 PASS |
| 端到端 smoke | `uv run python -m cortex.cli smoke` | pipeline 跑通 |
| **Stage 7 verify** | `uv run python scripts/verify_stage7.py` | 本档全部验收 PASS |

**回归不变量**:已验证的 1-6 行为不退步(experience 幂等、recall 4 通道、forget 双轨、bulk/import/export、R1 think 剥离)。
