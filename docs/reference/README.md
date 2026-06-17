# CortexDB 原版文档存档(参考)

本目录是 CortexDB(cortexdb.ai/docs)文档的**完整全文存档**,作为本项目复刻实现的一手参考。

## 抓取说明

- **抓取时间**:2026-06-17/18
- **抓取方式**:`curl` HTML + 正则剥 script/style,提取可见文本
- **覆盖范围**:docs 下**全部 56 篇**(已排除 connectors/integrations 的 35 篇连接器文档)
- **每篇格式**:纯文本,含页面 sidebar 导航(冗余,但正文完整)
- **每篇正文长度**:2.6K – 22K 字符

## 目录索引

### Concepts(9 篇)—— 核心设计理念,**必读**
- [`concepts/c_layers.txt`](concepts/c_layers.txt) — 五层记忆模型(Events/Episodes/Facts/Beliefs/Understanding)
- [`concepts/c_event-sourcing.txt`](concepts/c_event-sourcing.txt) — 事件溯源,写路径无 LLM
- [`concepts/c_bi-temporal.txt`](concepts/c_bi-temporal.txt) — 双时态模型,4 时间字段
- [`concepts/c_scopes.txt`](concepts/c_scopes.txt) — 层级 scope 命名空间
- [`concepts/c_experience-envelope.txt`](concepts/c_experience-envelope.txt) — 统一写入载荷(判别联合体)
- [`concepts/c_lifecycle.txt`](concepts/c_lifecycle.txt) — 六阶段异步生命周期
- [`concepts/c_authorization.txt`](concepts/c_authorization.txt) — 4 层能力栈 + PASETO(**本项目简化,仅参考**)
- [`concepts/c_knowledge-graph.txt`](concepts/c_knowledge-graph.txt) — 知识图谱(Facts+Beliefs 涌现)
- [`concepts/c_hybrid-retrieval.txt`](concepts/c_hybrid-retrieval.txt) — 4 通道混合检索 + RRF

### Research(2 篇)—— 架构总纲,**最优先必读**
- [`research/res_arch.txt`](research/res_arch.txt) — ⭐ **v1 架构白皮书**(13 节,完整设计)
- [`research/res_benchmark.txt`](research/res_benchmark.txt) — Benchmark 论文(含消融实验数据)

### API Reference(21 篇)—— 端点详设
核心 5 端点:
- [`api-reference/api_experience.txt`](api-reference/api_experience.txt) — `POST /v1/experience`(唯一写入)
- [`api-reference/api_recall.txt`](api-reference/api_recall.txt) — `POST /v1/recall`(StratifiedPack 检索)
- [`api-reference/api_answer.txt`](api-reference/api_answer.txt) — `POST /v1/answer`(recall ⊕ LLM)
- [`api-reference/api_forget.txt`](api-reference/api_forget.txt) — `POST /v1/forget`(选择性遗忘)
- [`api-reference/api_erasures.txt`](api-reference/api_erasures.txt) — Erasures(GDPR 真删,**本项目简化**)

层直读:
- [`api-reference/api_events.txt`](api-reference/api_events.txt) · [`api_episodes.txt`](api-reference/api_episodes.txt) · [`api_facts.txt`](api-reference/api_facts.txt) · [`api_beliefs.txt`](api-reference/api_beliefs.txt) · [`api_understanding.txt`](api-reference/api_understanding.txt)

图谱相关(**本项目重点参考**):
- [`api-reference/api_facts.txt`](api-reference/api_facts.txt) — 含 `GET /facts/timeline`(超替链)
- [`api-reference/api_beliefs.txt`](api-reference/api_beliefs.txt) — 含 `GET /beliefs/why`(证据图遍历)

其余:scopes/lifecycle/audit/import/export/blobs/vocabularies/temporal-phrases/auth/policy/admin

### Operations(9 篇)—— 部署/调优,**实现细节宝库**
- [`operations/ops_recall-tuning.txt`](operations/ops_recall-tuning.txt) — ⭐ **检索管线完整披露**(6 通道 + 编译常量 + 延迟预算)
- [`operations/ops_profiles.txt`](operations/ops_profiles.txt) — ⭐ **7 套可复制配置**(Benchmark/Voice/Batch/...)
- [`operations/ops_storage-cluster.txt`](operations/ops_storage-cluster.txt) — 存储分层 + HNSW 参数 + 集群
- [`operations/ops_configuration.txt`](operations/ops_configuration.txt) — 配置解析(文件/env/CLI 优先级)
- [`operations/ops_embeddings.txt`](operations/ops_embeddings.txt) — 嵌入模型选型 + 维度陷阱
- [`operations/ops_llm-answer.txt`](operations/ops_llm-answer.txt) — 4 处 LLM 调用点 + fallback 链
- [`operations/ops_security-compliance.txt`](operations/ops_security-compliance.txt) — 安全/合规(**本项目大部分跳过**)
- [`operations/ops_benchmarking.txt`](operations/ops_benchmarking.txt) — Benchmark 复现步骤
- [`operations/ops_overview.txt`](operations/ops_overview.txt) — Operations 总览

### Features(5 篇)
- graph-memory / temporal-queries / entity-extraction(**链接相关**) / memory-evolution / export-import

### SDKs(5 篇)
- Python / TypeScript / CLI / REST API / MCP Server

### Enterprise(2 篇)—— **本项目跳过**
### Quickstart(3 篇)

## 本项目与原版的偏差

本项目是**简化复刻**,与原版的关键偏差(详见 `docs/specs/01-technical-decisions.md`):

| 维度 | 原版 | 本项目 |
|------|------|--------|
| 图遍历 | 原生 BFS(自建) | 递归 CTE(Postgres) |
| 任务队列 | 自建 lifecycle stream | Postgres-as-queue(`SKIP LOCKED`) |
| 实体链接 | 未明示 | **B over C**(pgvector 召回 + LLM 灰区判定) |
| 授权 | PASETO + 4 层能力栈 | 静态 API key + scope 路径 |
| 集群 | gossip + 一致性哈希 | 单机 |
| 企业安全 | 加密/TLS/RBAC/SIEM/DSAR | 无 |
| Understanding 层 | LLM 概念合成 | MVP 最简版/跳过 |

## 重新抓取

如需查阅最新版或本存档未覆盖的页面(connectors/integrations),访问:
- 文档站:https://cortexdb.ai/docs/
- sitemap:https://cortexdb.ai/sitemap.xml

抓取脚本模式:
```bash
curl -sL "https://cortexdb.ai/docs/<path>" | \
  python3 -c "import sys,re,html as h; \
    t=re.sub(r'<script[^>]*>.*?</script>','',sys.stdin.read(),flags=re.S); \
    t=re.sub(r'<style[^>]*>.*?</style>','',t,flags=re.S); \
    t=re.sub(r'<[^>]+>','\n',t); t=h.unescape(t); \
    print(re.sub(r'\n\s*\n','\n',t))"
```
