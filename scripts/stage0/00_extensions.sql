-- 00_extensions.sql — 阶段 0 扩展安装
-- 所有对象落在独立 schema `cortex_stage0`,保证幂等 + 与真实库隔离。
-- DDL 依据: docs/specs/03-data-model.md(8 张主表 + 2 张辅助)。

CREATE SCHEMA IF NOT EXISTS cortex_stage0;

-- pgvector (B over C 的 C 层向量召回,entities.embedding vector(1024))
CREATE EXTENSION IF NOT EXISTS vector;

-- ltree (spec 第 4.5 节:LIKE vs ltree 对比验证)
CREATE EXTENSION IF NOT EXISTS ltree;

-- pg_trgm / unaccent (BM25 通道的 tsvector 备选方案探索)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- pgcrypto (gen_random_uuid 在 PG13+ 已内建,保留作冗余保险)
CREATE EXTENSION IF NOT EXISTS pgcrypto;
