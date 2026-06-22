"""Pytest 配置 + 共享 fixture。

每个测试用真实 Postgres(需 DB 代理/直连可达),不 mock。
测试间用独立 scope 隔离,不互相干扰。
"""
import os
import uuid

import pytest

# 确保 LLM key 在环境里(测试需要真实 LLM 做抽取验证)
os.environ.setdefault("CORTEX_LLM_EXTRACTION_API_KEY", os.environ.get("CORTEX_LLM_EXTRACTION_API_KEY", ""))
os.environ.setdefault("CORTEX_LLM_ANSWER_API_KEY", os.environ.get("CORTEX_LLM_ANSWER_API_KEY", ""))
os.environ.setdefault("CORTEX_LLM_SYNTHESIS_API_KEY", os.environ.get("CORTEX_LLM_SYNTHESIS_API_KEY", ""))


@pytest.fixture(scope="session")
def test_scope():
    """全局唯一测试 scope(避免与其他 scope 冲突)。"""
    return f"test:{uuid.uuid4().hex[:8]}/line:A/user:test"


@pytest.fixture(autouse=True)
def reset_schema():
    """每个测试函数前重置 schema(确保干净状态,避免 idempotency 冲突)。"""
    from cortex.db import init_schema
    init_schema(drop=True)
    yield
