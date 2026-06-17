# cortex-py 设计文档索引

> 本目录是 cortex-py 项目的**权威设计文档**。所有实现以此为依据。
> 新 agent / 新 contributor **必须按顺序读完本目录全部文档**再开始工作。

## 阅读顺序

### 1. [`../HANDOFF.md`](../HANDOFF.md) — 交接总览(先读这个)
项目定位、锁定选型汇总、分阶段路线、关键风险、流程要求。

### 2. [`01-technical-decisions.md`](01-technical-decisions.md) — 技术选型与架构 ✅ 已锁定
15 个维度的选型表 + rationale + brainstorming 决策溯源(为什么不是别的)。
**不可推翻**,除非用户明确重开某项。

### 3. [`02-research-notes.md`](02-research-notes.md) — CortexDB 调研笔记(前因后果)
CortexDB 原版为什么这么设计,本项目继承什么、改什么。基于 `docs/reference/` 56 篇原文。
**理解"为什么"的参考**,不包含本项目自己的决策。

### 4. [`03-data-model.md`](03-data-model.md) — 数据模型设计 ⚠️ 待批准
8 张表的完整 schema + 决策 rationale + 图遍历 CTE 示例 + **第 11 节的 8 个待用户确认决策点**。
**阶段 0 DDL 的设计依据。批准前不写任何 SQL/代码。**

### 5. [`04-stage0-smoke-test.md`](04-stage0-smoke-test.md) — 阶段 0 冒烟测试计划
纯 SQL 验证 schema 的计划:假数据设计、6 个验证脚本、验收标准、失败处理决策树。
**方案 2(分层构建)的保险,不可跳过。**

## 参考材料

### [`../reference/`](../reference/README.md) — CortexDB 原版文档存档(56 篇全文)
按 concepts / research / api-reference / operations / features / sdks / enterprise / quickstart 分类。
重点:`research/res_arch.txt`(架构白皮书)、`operations/ops_recall-tuning.txt`(检索管线)。

## 当前状态(2026-06-18)

| 里程碑 | 状态 |
|--------|------|
| brainstorming 全流程 | ✅ 完成 |
| 技术选型锁定 | ✅ 完成 |
| 实际服务配置确认(embedding/rerank/vlm) | ✅ 完成(见 01 配置章节) |
| 调研笔记 | ✅ 完成 |
| 技术选型 spec | ✅ 完成 |
| 数据模型 spec | ✅ 完成(**7 决策点全部裁定**,见 03 第 11 节) |
| 运行时风险登记 | ✅ 完成(3 项,见 03 第 12 节) |
| 阶段 0 计划 | ✅ 完成 |
| 阶段 0 冒烟脚本 | ✅ 已就绪(`scripts/stage0/decision_probe.py`,待 Postgres 可达执行) |
| **用户 review 全部 specs** | ⬜ **下一步** |
| 阶段 0 执行(SQL 冒烟) | ⬜(待 Postgres `192.168.1.21` 可达) |
| writing-plans(阶段 1-5) | ⬜ |
| 阶段 1 实施 | ⬜ |

## 关键约束(给所有 agent / contributor)

1. **数据模型 spec(`03`)批准前,不写 schema DDL**
2. **阶段 0 冒烟通过前,不写 Python 业务代码**
3. **实体链接(B over C)是图谱质量命门,MVP 第一版就上,不用纯字符串归一**
4. **scope 过滤是 SQL 层强制,否则图谱糊一起**
5. **Redis 不用**(用户确认),队列走 Postgres `SKIP LOCKED`
6. **写路径无 LLM**,只 append WAL,抽取全异步

---

*本索引由 brainstorming 会话生成并维护。specs 变更时同步更新本索引。*
