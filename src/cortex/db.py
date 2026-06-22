"""数据库:engine + session + schema 初始化。

schema 以 src/cortex/schema.sql 为单一真相源(避免 ORM 与 DDL 漂移)。
查询走 SQLAlchemy text()(递归 CTE / pgvector <=> / tsvector 都是已验证 SQL)。
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from .config import load_config

_engine = None

SCHEMA_SQL_PATH = Path(__file__).parent / "schema.sql"


def get_engine():
    global _engine
    if _engine is None:
        cfg = load_config()
        _engine = create_engine(
            cfg.database.url,
            poolclass=NullPool,     # 不池化:每次 session 新建连接(代理场景最稳,无中毒连接复用)
            future=True,
        )
    return _engine


@contextmanager
def session_scope():
    """事务 session 上下文(成功 commit / 异常 rollback)。
    OperationalError 时 invalidate 连接,强制池丢弃(防中毒连接复用)。
    测试时可通过 CORTEX_DB_SCHEMA_OVERRIDE 环境变量切换到独立 test schema。"""
    eng = get_engine()
    schema = os.environ.get("CORTEX_DB_SCHEMA_OVERRIDE", "cortex")
    conn = eng.connect()
    tx = conn.begin()
    try:
        conn.execute(text(f"SET search_path = {schema}, public"))
        yield conn
        tx.commit()
    except OperationalError:
        try:
            tx.rollback()
        except Exception:  # noqa: BLE001
            pass
        conn.invalidate()   # 强制池丢弃此连接,不再复用
        raise
    except Exception:
        tx.rollback()
        raise
    finally:
        conn.close()


def with_retry(fn, *args, retries=2, **kwargs):
    """对 DB 操作的 OperationalError 重试(代理 LAN 抖动兜底)。"""
    import time as _t
    for i in range(retries):
        try:
            return fn(*args, **kwargs)
        except OperationalError:
            if i < retries - 1:
                _t.sleep(0.5)
                continue
            raise


def get_db():
    """FastAPI 依赖:yield 一个 connection(请求内复用)。"""
    eng = get_engine()
    conn = eng.connect()
    try:
        conn.execute(text("SET search_path = cortex, public"))
        # FastAPI 走 autocommit-per-request;这里给一个显式事务
        tx = conn.begin()
        try:
            yield conn
            tx.commit()
        except Exception:
            tx.rollback()
            raise
    finally:
        conn.close()


def init_schema(drop: bool = False) -> None:
    """建 schema + 全表(幂等)。drop=True 先 DROP SCHEMA CASCADE(开发重置)。"""
    cfg = load_config()
    eng = get_engine()
    with eng.begin() as conn:
        if drop:
            conn.execute(text(f'DROP SCHEMA IF EXISTS {cfg.database.schema_} CASCADE'))
        sql = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        conn.execute(text(sql))
        # 运行时维度二次校验:查 entities.embedding 实际 vector 维度
        row = conn.execute(text(
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
            "JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='cortex' AND c.relname='entities' AND a.attname='embedding'"
        )).fetchone()
        actual = row[0] if row else ""
        if str(cfg.embedding.dimension) not in actual:
            raise RuntimeError(f"entities.embedding 类型={actual} 与 config embedding.dimension={cfg.embedding.dimension} 不符")


def assert_services_reachable() -> dict:
    """启动健康检查:Postgres 可连。返回 {ok, detail}。"""
    try:
        with session_scope() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "detail": "postgres reachable"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)}
