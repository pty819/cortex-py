# 04 — 阶段 0 冒烟测试计划

> **状态**:待用户批准 `03-data-model.md` 后执行。
> **目标**:在写任何 Python 业务代码前,用纯 SQL + 假数据验证 schema 能否撑住图遍历、实体链接、双时态超替。
> **交付物**:`scripts/stage0/` 下的 SQL 脚本 + 假数据 + 验证查询 + 性能报告。

---

## 1. 阶段 0 的存在理由

用户选了"方案 2:分层构建"(先打数据模型地基)。方案 2 的风险是:schema 在没看到真实召回数据前难一次做对,Facts/实体链接的 schema 和图遍历的 CTE 性能是最难一次设计对的点。

**阶段 0 是保险**:用假数据 + 纯 SQL 提前暴露问题。schema 不对改的是 SQL 脚本,不是代码库。

参考:[`01-technical-decisions.md`](01-technical-decisions.md) 第 7 节(阶段 0 的风险与缓解)。

---

## 2. 交付物清单

```
scripts/stage0/
├── 00_extensions.sql       -- CREATE EXTENSION vector; 等
├── 01_schema.sql           -- 完整 DDL(从 03-data-model.md 翻译)
├── 02_seed_data.sql        -- 假数据:100 events + 30 entities + 200 facts + 50 beliefs
├── 03_temporal_tests.sql   -- 双时态超替 + timeline 查询验证
├── 04_graph_traversal.sql  -- 递归 CTE 图遍历 + 性能计时
├── 05_entity_resolution.sql-- pgvector 召回 + 阈值逻辑验证
├── 06_scope_isolation.sql  -- scope 过滤 + holistic/descend 遍历验证
├── 07_queue_demo.sql       -- SELECT FOR UPDATE SKIP LOCKED 抢任务演示
└── run_all.sh              -- 一键跑全,输出报告
```

---

## 3. 假数据设计

### 规模
- **100 events**(2 个 scope,每个 50 条)
- **30 entities**(人/组织/服务/项目混合)
- **200 facts**(含 20 条超替历史,模拟 deal_stage 从 poc → close → signed)
- **50 beliefs**
- **故意埋的测试用例**:
  - 同名不同人:scope A 的 "Acme"(客户)vs scope B 的 "Acme"(内部服务)——验证 scope 隔离
  - 别名:`Bob` / `Robert Smith` / `[email protected]` 归一到同一 entity——验证 B over C
  - 超替链:`Acme deal_stage` 三次变更——验证 timeline
  - 图遍历:`Priya works_at Acme, owns Q3-Renewal, Q3-Renewal has_status negotiating`——验证 2-3 跳 BFS

### scope 设计
- `org:acme/dept:sales/user:alice`(销售场景,Acme 是客户)
- `org:acme/dept:eng/user:bob`(工程场景,Acme 是内部服务代号)

---

## 4. 验证目标与验收标准

### 4.1 双时态超替(脚本 03)

**验证**:
1. 插入 fact: `Acme deal_stage = 'poc'`,valid_from=2026-01-01
2. 插入 fact: `Acme deal_stage = 'close'`,valid_from=2026-04-10 → 旧 fact 的 valid_to 闭合为 2026-04-10
3. 插入 fact: `Acme deal_stage = 'signed'`,valid_from=2026-05-13 → 中间 fact 闭合
4. timeline 查询返回 3 条,valid_from 升序,只有最后一条 valid_to IS NULL

**验收**:
- ✅ timeline 查询返回正确的 3 个版本
- ✅ `as_of='2026-03-01'` 查询返回 'poc'
- ✅ `as_of='2026-05-01'` 查询返回 'close'
- ✅ 当前查询(`valid_to IS NULL`)返回 'signed'

### 4.2 图遍历性能(脚本 04)

**验证**:
- seed 实体 `ent_acme_corp`,max_hops=2,沿所有 predicate
- 预期命中:`Priya`(works_at)、`Q3-Renewal`(owns)、negotiating(has_status)

**性能验收**(200 条 facts 规模):
- ✅ 2 跳 BFS < 50ms
- ✅ 3 跳 BFS < 200ms

**压力测试**(扩展到 1 万条 facts,脚本自动生成):
- 用 `generate_series` + 随机 entity_id/predicate 组合批量 INSERT,从 200 条扩到 1 万条
- ✅ 2 跳 BFS < 500ms
- ✅ 3 跳 BFS < 2s

如果 1 万条时 3 跳 > 2s,说明索引设计有问题,需优化(考虑物化 graph_edges 视图或 ltree)。

### 4.3 实体链接 B over C(脚本 05)

**验证 C 层(向量召回)**:
- 用 `Robert Smith` 的 embedding 查 entities 表
- 预期:命中 `ent_robert_smith`(cosine 高),不命中 scope B 的无关实体

**验证阈值逻辑**:
- cosine > 0.85:直接合并(不调 LLM)
- cosine < 0.30:直接新建
- 0.30 ~ 0.85:灰区(脚本里 mock 成"调 LLM",返回预设判定)

**验收**:
- ✅ 向量召回 top-5 正确
- ✅ 阈值分支正确触发
- ✅ 合并后 `merged_into` 正确设置,旧实体查询被过滤

### 4.4 scope 隔离(脚本 06)

**验证**:
- scope A 的 `Acme`(客户)和 scope B 的 `Acme`(内部服务)是不同 entity_id
- scope A 的查询看不到 scope B 的 facts
- holistic 遍历:从 `org:acme/dept:sales/user:alice` 向上,能看到 `org:acme/dept:sales` 和 `org:acme` 的记忆

**验收**:
- ✅ 跨 scope 查询返回 0 条(scope 过滤生效)
- ✅ holistic 生成正确的前缀列表并查到祖先 scope 数据

### 4.5 scope 路径 LIKE vs ltree(脚本 06 附带)

**对比**:
- 用 TEXT + LIKE 做 holistic 遍历(生成前缀列表 + ANY)
- 如果装了 ltree 扩展,同样查询用 ltree 的 `@>`(祖先)操作符
- 对比性能

**验收**:记录两者性能,选优。MVP 倾向 LIKE(不引入扩展),除非 ltree 显著更优。

### 4.6 Postgres-as-queue(脚本 07)

**验证**:
- 插入 10 个 job(不同 priority)
- 用 `FOR UPDATE SKIP LOCKED` 模拟 2 个 worker 并发抢
- 验证:高 priority 先抢,两 worker 不抢同一个

**验收**:
- ✅ priority DESC 排序正确
- ✅ SKIP LOCKED 生效(无重复抢)
- ✅ visibility timeout 模拟(把 running 超时任务重置为 queued)

---

## 5. 执行流程

```bash
# 在用户提供 的 Postgres 上执行
export DATABASE_URL="postgresql://postgres:...@192.168.1.21:5432/postgres"

cd scripts/stage0

# 1. 建 schema
psql $DATABASE_URL -f 00_extensions.sql
psql $DATABASE_URL -f 01_schema.sql

# 2. 灌假数据
psql $DATABASE_URL -f 02_seed_data.sql

# 3. 跑验证
psql $DATABASE_URL -f 03_temporal_tests.sql
psql $DATABASE_URL -f 04_graph_traversal.sql
psql $DATABASE_URL -f 05_entity_resolution.sql
psql $DATABASE_URL -f 06_scope_isolation.sql
psql $DATABASE_URL -f 07_queue_demo.sql

# 或一键
./run_all.sh > stage0_report.txt 2>&1
```

---

## 6. 失败处理

阶段 0 的失败是**有价值的**——它暴露 schema 问题。失败时的决策树:

| 失败现象 | 可能原因 | 修正方向 |
|----------|----------|----------|
| 图遍历 1 万条时 > 2s | 索引未命中或 CTE 展开过深 | 加部分索引 / 物化 graph_edges / 降 max_hops |
| 向量召回漏召回 | embedding 质量或维度不对 | 检查 embedding 生成逻辑 / 调阈值 |
| scope LIKE 全表扫描 | 索引未覆盖 | scope 列加 btree,或换 ltree |
| 超替 timeline 查询慢 | 未用 (subject_id, predicate, valid_from) 索引 | 确认 idx_facts_subj_pred_valid 存在且被用 |
| queue 抢任务串行化 | SKIP LOCKED 未生效 | 确认 Postgres 版本 ≥ 9.5 |

修正的是 SQL 脚本 / 索引 / schema,不是 Python 代码。

---

## 7. 阶段 0 完成标准

全部以下满足,才进入阶段 1(写 Python 业务代码):

- ✅ 8 张表 DDL 执行无错
- ✅ 假数据灌入成功
- ✅ 6 个验证脚本全部通过验收标准
- ✅ 1 万条 facts 压力测试达性能验收
- ✅ LIKE vs ltree 决策已做(scope 路径方案定型)
- ✅ stage0_report.txt 产出,记录所有性能数字和决策

---

*阶段 0 是方案 2(分层构建)的保险。不要跳过它直接写 Python。*
