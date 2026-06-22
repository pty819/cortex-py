"""Pytest 配置 + 共享 fixture。

测试用独立 schema(cortex_test_XXX),不碰生产 cortex schema。
"""
import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("CORTEX_LLM_EXTRACTION_API_KEY", os.environ.get("CORTEX_LLM_EXTRACTION_API_KEY", ""))
os.environ.setdefault("CORTEX_LLM_ANSWER_API_KEY", os.environ.get("CORTEX_LLM_ANSWER_API_KEY", ""))
os.environ.setdefault("CORTEX_LLM_SYNTHESIS_API_KEY", os.environ.get("CORTEX_LLM_SYNTHESIS_API_KEY", ""))

_TEST_SCHEMA = f"cortex_test_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def test_scope():
    return f"test:{uuid.uuid4().hex[:8]}/line:A/user:test"


@pytest.fixture(autouse=True)
def reset_test_schema():
    """每个测试函数前在独立 test schema 里建表(不碰生产 cortex schema)。"""
    from cortex.db import get_engine
    from sqlalchemy import text as sa_text
    eng = get_engine()
    # 建独立 test schema
    with eng.begin() as c:
        c.execute(sa_text(f'DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE'))
        c.execute(sa_text(f'CREATE SCHEMA {_TEST_SCHEMA}'))
    # 读 schema.sql,替换 schema 名,执行
    schema_sql = Path(__file__).parent.parent.joinpath("src", "cortex", "schema.sql").read_text()
    schema_sql = schema_sql.replace("cortex.", f"{_TEST_SCHEMA}.")
    schema_sql = schema_sql.replace("CREATE SCHEMA IF NOT EXISTS cortex", f"CREATE SCHEMA IF NOT EXISTS {_TEST_SCHEMA}")
    schema_sql = schema_sql.replace("SET search_path = cortex", f"SET search_path = {_TEST_SCHEMA}")
    with eng.begin() as c:
        c.execute(sa_text(f"SET search_path = {_TEST_SCHEMA}, public"))
        c.execute(sa_text(schema_sql))
    # 让 session_scope 用 test schema
    os.environ["CORTEX_DB_SCHEMA_OVERRIDE"] = _TEST_SCHEMA
    yield
    with eng.begin() as c:
        c.execute(sa_text(f'DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE'))
    os.environ.pop("CORTEX_DB_SCHEMA_OVERRIDE", None)
