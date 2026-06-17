"""数据库:engine + session + schema 初始化。

schema 以 src/cortex/schema.sql 为单一真相源(避免 ORM 与 DDL 漂移)。
查询走 SQLAlchemy text()(递归 CTE / pgvector <=> / tsvector 都是已验证 SQL)。
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .config import load_config

_engine: Engine | None = None

SCHEMA_SQL_PATH = Path(__file__).parent / "schema.sql"


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        cfg = load_config()
        _engine = create_engine(cfg.database.url, pool_pre_ping=True, future=True)
    return _engine


@contextmanager
def session_scope():
    """事务 session 上下文(成功 commit / 异常 rollback)。"""
    eng = get_engine()
    conn = eng.connect()
    tx = conn.begin()
    try:
        # schema 限定:所有裸名解析到 cortex
        conn.execute(text("SET search_path = cortex, public"))
        yield conn
        tx.commit()
    except Exception:
        tx.rollback()
        raise
    finally:
        conn.close()


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
