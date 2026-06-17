"""配置加载:YAML + 环境变量覆盖 + 维度强校验。

依据:01-technical-decisions.md §配置。三路 LLM + rerank + embedding 分段。
启动校验:embedding.dimension 必须 == schema 里 entities.embedding 的 vector(N)。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

_DEFAULT_CONFIG_PATH = Path("config/config.yaml")
# schema 里 entities.embedding 的 vector 维度(写死,见 schema.sql)
SCHEMA_VECTOR_DIM = 1024


class DatabaseCfg(BaseModel):
    url: str
    schema_: str = Field(default="cortex", alias="schema")


class EmbeddingCfg(BaseModel):
    provider: str
    api_key: str
    api_base: str
    model: str
    dimension: int
    max_concurrent: int = 10
    timeout: int = 60


class RerankCfg(BaseModel):
    provider: str = "openai-compatible"
    api_key: str
    api_base: str
    model: str
    threshold: float = 0.1
    top_n: int = 25
    timeout: int = 60


class LLMTierCfg(BaseModel):
    provider: str = "openai-compatible"
    model: str
    api_key: str
    api_base: str
    temperature: float = 0.0
    timeout: int = 600
    max_retries: int = 2
    max_concurrent: int = 10
    structured_output_mode: Literal["json_schema", "json_object", "prompt"] = "json_schema"


class VerifierCfg(BaseModel):
    """可选 verifier(默认关);字段都有默认值,disabled 时不强制要求 key。"""
    enabled: bool = False
    provider: str = "openai-compatible"
    model: str = "Minimax-M3"
    api_key: str = ""
    api_base: str = "https://api.minimaxi.com/v1"
    temperature: float = 0.0
    timeout: int = 600
    max_retries: int = 2


class LLMCfg(BaseModel):
    extraction: LLMTierCfg
    answer: LLMTierCfg
    synthesis: LLMTierCfg
    verifier: VerifierCfg = Field(default_factory=VerifierCfg)


class ApiCfg(BaseModel):
    key: str = ""
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])


class WorkerCfg(BaseModel):
    poll_interval_secs: float = 1.0
    visibility_timeout_secs: int = 300
    reaper_interval_secs: int = 60
    max_attempts: int = 3
    backoff_base_secs: int = 4


class RetrievalCfg(BaseModel):
    top_k: int = 40
    rrf_k: float = 60.0
    graph_weight: float = 0.20
    graph_max_hops: int = 2


class LinkThresholds(BaseModel):
    merge: float = 0.85
    new: float = 0.30


class ExtractionCfg(BaseModel):
    embedding_text: str = "{name}. {description}"
    link_thresholds: LinkThresholds = Field(default_factory=LinkThresholds)


class AppConfig(BaseModel):
    database: DatabaseCfg
    embedding: EmbeddingCfg
    rerank: RerankCfg
    llm: LLMCfg
    api: ApiCfg = Field(default_factory=ApiCfg)
    worker: WorkerCfg = Field(default_factory=WorkerCfg)
    retrieval: RetrievalCfg = Field(default_factory=RetrievalCfg)
    extraction: ExtractionCfg = Field(default_factory=ExtractionCfg)

    @model_validator(mode="after")
    def _check_dim(self):
        if self.embedding.dimension != SCHEMA_VECTOR_DIM:
            raise ValueError(
                f"embedding.dimension={self.embedding.dimension} != schema vector({SCHEMA_VECTOR_DIM}). "
                "改 embedding 模型须同步改 schema.sql 的 vector(N);否则会静默召回失败(全 0 结果)。"
            )
        return self


def _env_overrides(d: dict) -> dict:
    """环境变量覆盖(优先级高于 YAML)。"""
    tiers = {"extraction": "extraction", "answer": "answer", "synthesis": "synthesis", "verifier": "verifier"}
    for tier, key in tiers.items():
        env = os.environ.get(f"CORTEX_LLM_{tier.upper()}_API_KEY")
        if env and "llm" in d and tier in d["llm"]:
            d["llm"][tier]["api_key"] = env
    if (db := os.environ.get("CORTEX_DATABASE_URL")):
        d.setdefault("database", {})["url"] = db
    if (k := os.environ.get("CORTEX_API_KEY")):
        d.setdefault("api", {})["key"] = k
    return d


_CACHE: AppConfig | None = None


def load_config(path: str | Path | None = None, *, reload: bool = False) -> AppConfig:
    """加载 YAML 配置。path 默认 config/config.yaml(可被 CORTEX_CONFIG 覆盖)。"""
    global _CACHE
    if _CACHE and not reload:
        return _CACHE
    p = Path(path or os.environ.get("CORTEX_CONFIG", _DEFAULT_CONFIG_PATH))
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    raw = _env_overrides(raw)
    _CACHE = AppConfig.model_validate(raw)
    return _CACHE


def llm_configured(tier: str = "extraction") -> bool:
    """某路 LLM 是否配了真实 key(非占位符)。"""
    cfg = load_config()
    key = getattr(cfg.llm, tier).api_key
    return bool(key) and not key.startswith("REPLACE_WITH")
