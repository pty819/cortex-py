# Scope 策略最佳实践

> scope 是 cortex 的**命名空间**。同一 scope 里的实体才能互相连接、合并、一起被召回。
> 选错 scope 的后果不是报错,而是**静默的数据隔离**:结构知识和故障诊断各建各的图谱,跨文档实体无法链接,召回时看不到对方。

本文是给操作者 / AI agent 的决策指南,讲清楚什么时候该合并、什么时候该分。

---

## 一、scope 的核心规则

1. **scope 是字符串路径**,如 `org:fab-a/etch:E301`。cortex 把它当作命名空间,所有实体/fact/belief 都挂在某个 scope 下。
2. **跨 scope 默认不可见**:`recall(view=local)` 只查当前 scope。结构文档在 scope A、故障报告在 scope B,默认互相看不见。
3. **实体合并(B over C)只在同 scope 内生效**:跨文档同名实体(如 MFC-101)要自动合并,它们必须在同一个 scope。
4. **scope 支持路径层级**,可用 `view=holistic` 向祖先查、`view=descend` 向后代查——但**抽取/入图时不跨 scope 合并**。

> 简言之:scope = 「这条知识属于哪个设备的哪个层级的世界」。放错了世界,就成了一座孤岛。

---

## 二、决策:什么时候该用同一个 scope

### ✅ 应该合并(同一个 scope)的场景

**同一台物理设备 / 同一个系统的结构知识 + 运行事件 + 故障案例**,必须放一个 scope。

为什么:故障报告里提到的部件(MFC-101、OES-301、腔体C3)需要链接到结构文档里已建好的实体,才能形成「故障实体 → 结构上下文」的连通图谱。分开 scope,这些同名实体会被各自独立创建,图谱断裂。

具体例子(本仓库 demo 数据):
```
结构文档 structure-etch-E301.json:
  scope = org:fab-a/etch:E301          ← E-301 的通用结构知识

故障报告 incident-etch-E301-20260620.json:
  scope = org:fab-a/etch:E301/chamber:C3/user:diag   ← ❌ 不同 scope
```

**问题**:两个 scope 不同。结构文档建的 `MFC-101`(part_of 气体输送系统)和故障报告里排查的 `MFC-101`(CF4 流量偏差检查)在默认 `local` 召回下互相不可见,B-over-C 实体合并也不会把它们合到一起。故障诊断图谱失去了结构上下文。

**正确做法**:把结构知识也放在(或同时镜像到)诊断 scope 能覆盖到的层级。

### 推荐的 scope 布局(单机台)

```
equip:XXX-v1                          ← 整台设备的全部知识(结构 + 案例 + 传感器)
```

所有知识放一个 scope,图谱最连通,召回最简单(都用 `local`)。这是大多数单机台场景的最佳选择。

### 多机台共享通用知识的布局

如果有多台同型设备,且「通用结构/参数知识」想被所有机台共享:

```
equip:XXX-series          ← 通用知识(机型手册、传感器布局,所有机台共用)
  └─ equip:XXX-v1         ← 1 号机的特定案例
  └─ equip:XXX-v2         ← 2 号机的特定案例
```

机台特定 case 放各自 scope;通用结构放祖先 `equip:XXX-series`。召回机台 scope 时用 `view=holistic` 向上把通用知识带进来。

> ⚠️ 注意:这种方式下,**抽取时实体合并仍不跨 scope**。机台 case 里提到的 MFC-101 不会自动并入祖先 scope 的 MFC-101,只是召回时能被一起检索到。要真正合并,得放同一 scope。

---

## 三、决策:什么时候该分 scope

### ✅ 应该分(不同 scope)的场景

1. **不同租户 / 不同组织**的数据隔离:`org:acme/...` vs `org:globex/...`。
2. **完全独立的设备**,知识不该串台:E-301 的知识 vs P-201 的知识。
3. **不同项目 / 产线**需要隔离的运行记忆(且无共享结构)。

判断标准:**这两组知识是否需要「同名实体合并成同一个节点」?需要 → 合 scope;不需要 → 分 scope。**

---

## 四、操作清单

### 入图前先定 scope

灌任何知识前,先回答:**这条知识属于哪台设备的哪个世界?** 然后把所有相关文档(结构 + 案例)统一灌进那个 scope。

```
# 正确:结构 + 诊断都进 equip:E301-v1
POST /v1/ingest/document  scope=equip:E301-v1  intent=structure
POST /v1/experience       scope=equip:E301-v1  intent=incident_retrospective

# 错误:结构进一个 scope,诊断进另一个 scope(图谱断裂)
POST /v1/ingest/document  scope=equip:E301-v1          ...  intent=structure
POST /v1/experience       scope=diag:E301-cases        ...  intent=incident_retrospective
```

### 召回时确认 view

| 你的场景 | 推荐 view |
|---|---|
| 单 scope 布局(推荐) | `local`(默认) |
| 通用知识在祖先 scope,查机台时带上 | `holistic` |
| 管理层要看所有后代机台 | `descend` |

### 已经灌错 scope 了怎么办

实体一旦在 scope A 建好,**没有跨 scope 移动实体的内置 API**。补救方法:
1. 把文档用正确 scope **重新灌一遍**(带新的 `idempotency_key`),cortex 会在新 scope 重建。
2. 旧 scope 的数据用 `POST /v1/forget`(软忘)或 `POST /v1/erasures`(硬删)清理。
3. 预防胜于治疗:入图前先确定 scope。

---

## 五、demo 数据的 scope 说明

仓库自带两份 demo 数据,**刻意用了不同的 scope** 作为反面教材和测试用途:

| 文件 | scope | intent |
|---|---|---|
| `data/structure-etch-E301.json` | `org:fab-a/etch:E301` | structure |
| `data/incident-etch-E301-20260620.json` | `org:fab-a/etch:E301/chamber:C3/user:diag` | incident_retrospective |

**实际使用时**:若想让故障诊断能链接到结构知识(看到 MFC-101 的 part_of 关系),把两者统一到同一个 scope,或对诊断 scope 用 `view=holistic` 召回(可向上看到结构 scope 的知识,但实体不合并)。

统一 scope 后,跨文档同名实体(如 MFC-101、OES-301、腔体C3)会通过 B-over-C 自动合并,图谱自动连通。

---

## 六、一句话总结

> **一个设备一个 scope,结构 + 案例全在一起。** 除非真的需要租户/设备隔离,否则别分 scope——分了就是孤岛。
