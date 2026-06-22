"""核心 DB 操作:WAL append(幂等)、Postgres-as-queue、lifecycle 事件。

schema 以 cortex.sql 为源;查询走 SQLAlchemy text()。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .db import session_scope


# ── WAL / Events append ─────────────────────────────────────────────────────
class IdempotencyConflict(Exception):
    """同 idempotency_key + 不同 body → 409"""


def _body_hash(modality: str, content: Dict, context: Dict) -> str:
    raw = json.dumps({"modality": modality, "content": content, "context": context}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def append_event(*, scope: str, modality: str, content: Dict[str, Any], context: Dict[str, Any],
                 caller: str, observed_actor: Optional[str] = None, subject: Optional[str] = None,
                 directives: Optional[Dict] = None, idempotency_key: str,
                 observed_at: Optional[str] = None) -> Tuple[str, int]:
    """append 一个 event。幂等:同 key+同 body → 返回既有;同 key+异 body → raise IdempotencyConflict。"""
    oactor = observed_actor or caller
    sub = subject or oactor
    obs_at = observed_at or context.get("observed_at")
    with session_scope() as c:
        # 先查幂等
        existing = c.execute(text(
            "SELECT event_id, wal_offset FROM events WHERE scope=:s AND idempotency_key=:k"
        ), {"s": scope, "k": idempotency_key}).fetchone()
        if existing:
            # 用 Python 端重算 hash 对比(避免 PG jsonb::text 与 Python json.dumps 排序差异)
            ex_content = c.execute(text("SELECT content FROM events WHERE event_id=:id"),
                                   {"id": existing.event_id}).scalar()
            ex_context = c.execute(text("SELECT context FROM events WHERE event_id=:id"),
                                   {"id": existing.event_id}).scalar()
            ex_modality = c.execute(text("SELECT modality FROM events WHERE event_id=:id"),
                                    {"id": existing.event_id}).scalar()
            ex_body = _body_hash(ex_modality, ex_content, ex_context)
            if ex_body == _body_hash(modality, content, context):
                return str(existing.event_id), existing.wal_offset
            raise IdempotencyConflict(f"idempotency_key={idempotency_key} 已存在且 body 不同")
        try:
            row = c.execute(text("""
                INSERT INTO events (scope, modality, content, context, caller, observed_actor, subject,
                                    observed_at, directives, idempotency_key)
                VALUES (:scope,:modality,CAST(:content AS jsonb),CAST(:context AS jsonb),:caller,:oa,:subj,
                        COALESCE(:observed_at, now()), CAST(:directives AS jsonb), :ik)
                RETURNING event_id, wal_offset
            """), {"scope": scope, "modality": modality, "content": json.dumps(content),
                   "context": json.dumps(context), "caller": caller, "oa": oactor, "subj": sub,
                   "observed_at": obs_at, "directives": json.dumps(directives) if directives else None,
                   "ik": idempotency_key}).fetchone()
            _auto_provision_scope(c, scope)
            emit_lifecycle(c, kind="captured", scope=scope, event_id=row.event_id)
            return str(row.event_id), row.wal_offset
        except IntegrityError as e:  # 理论上上面已查;并发兜底
            raise IdempotencyConflict(str(e.orig)) from e


def _auto_provision_scope(conn, scope: str) -> None:
    parts = scope.split("/")
    for i in range(1, len(parts) + 1):
        p = "/".join(parts[:i])
        parent = "/".join(parts[:i - 1]) if i > 1 else None
        conn.execute(text("""
            INSERT INTO scopes (scope_path, parent_path, auto_provisioned)
            VALUES (:p, :parent, true) ON CONFLICT (scope_path) DO NOTHING
        """), {"p": p, "parent": parent})


# ── queue ───────────────────────────────────────────────────────────────────
def enqueue_job(*, job_type: str, scope: str, event_id: Optional[str] = None,
                payload: Optional[Dict] = None, priority: int = 0) -> str:
    with session_scope() as c:
        row = c.execute(text("""
            INSERT INTO jobs (job_type, scope, event_id, payload, priority)
            VALUES (:t,:s,CAST(:e AS uuid),CAST(:p AS jsonb),:pr) RETURNING job_id
        """), {"t": job_type, "s": scope, "e": event_id, "p": json.dumps(payload) if payload else None,
               "pr": priority}).fetchone()
        return str(row.job_id)


def claim_next_job(conn, worker_id: str) -> Optional[Dict[str, Any]]:
    """原子抢一个到期 job(SKIP LOCKED)。调用方在已开的事务里。返回 row dict 或 None。"""
    row = conn.execute(text("""
        UPDATE jobs SET status='running', locked_by=:w, locked_at=now(), started_at=now(), attempts=attempts+1
        WHERE job_id = (SELECT job_id FROM jobs
                        WHERE status='queued' AND run_after <= now()
                        ORDER BY priority DESC, run_after, created_at
                        FOR UPDATE SKIP LOCKED LIMIT 1)
        RETURNING job_id, job_type, scope, event_id, attempts, max_attempts, payload
    """), {"w": worker_id}).fetchone()
    if not row:
        return None
    return {"job_id": str(row.job_id), "job_type": row.job_type, "scope": row.scope,
            "event_id": str(row.event_id) if row.event_id else None,
            "attempts": row.attempts, "max_attempts": row.max_attempts,
            "payload": row.payload or {}}


def complete_job(conn, job_id: str, result: Optional[Dict] = None) -> None:
    conn.execute(text("""
        UPDATE jobs SET status='completed', completed_at=now(), result=CAST(:r AS jsonb)
        WHERE job_id=CAST(:j AS uuid)
    """), {"r": json.dumps(result) if result else None, "j": job_id})


def fail_job(conn, job_id: str, error: str, *, backoff_base: int = 4) -> None:
    """失败:未超 max_attempts → queued+退避;超限 → failed(死信)。"""
    info = conn.execute(text("SELECT attempts, max_attempts FROM jobs WHERE job_id=CAST(:j AS uuid)"),
                        {"j": job_id}).fetchone()
    if info and info.attempts >= info.max_attempts:
        conn.execute(text("UPDATE jobs SET status='failed', error=:e WHERE job_id=CAST(:j AS uuid)"),
                     {"e": error[:500], "j": job_id})
    else:
        conn.execute(text("""
            UPDATE jobs SET status='queued', locked_by=NULL, locked_at=NULL,
                            run_after=now() + make_interval(secs => :backoff), error=:e
            WHERE job_id=CAST(:j AS uuid)
        """), {"backoff": float(backoff_base ** (info.attempts if info else 1)), "e": error[:500], "j": job_id})


def reap_zombies(conn, visibility_secs: int) -> int:
    """visibility timeout:running 且 locked_at 超时 → 重置 queued。返回重置数。"""
    r = conn.execute(text("""
        UPDATE jobs SET status='queued', locked_by=NULL, locked_at=NULL
        WHERE status='running' AND locked_at < now() - make_interval(secs => :v)
    """), {"v": float(visibility_secs)})
    return r.rowcount or 0


# ── lifecycle ───────────────────────────────────────────────────────────────
def emit_lifecycle(conn, *, kind: str, scope: str, event_id: Optional[str] = None,
                   job_id: Optional[str] = None, batch_id: Optional[str] = None,
                   payload: Optional[Dict] = None) -> str:
    row = conn.execute(text("""
        INSERT INTO lifecycle_events (kind, scope, event_id, job_id, batch_id, payload)
        VALUES (:k,:s,CAST(:e AS uuid),CAST(:j AS uuid),CAST(:b AS uuid),CAST(:p AS jsonb))
        RETURNING lifecycle_id, ts
    """), {"k": kind, "s": scope, "e": event_id, "j": job_id, "b": batch_id,
           "p": json.dumps(payload or {})}).fetchone()
    # NOTIFY:让 ?wait= 的 listener 能立刻收到(通道 cortex_lc,payload=kind|event_id)
    conn.execute(text("SELECT pg_notify('cortex_lc', :msg)"),
                 {"msg": f"{kind}|{event_id or ''}"})
    return str(row.lifecycle_id)


# ── ?wait= 同步阻塞 ─────────────────────────────────────────────────────────
import psycopg2
import threading

_STAGE_ORDER = {"captured": 0, "extracted": 1, "indexed": 2, "consolidated": 3}


def wait_for_stage(event_id: str, target_stage: str, timeout: float = 30.0) -> Dict[str, Any]:
    """阻塞直到该 event 出现目标 stage 的 lifecycle 事件,或超时。用独立连接 LISTEN/轮询。
    target_stage: captured|indexed|consolidated。indexed 对应 extracted/indexed;consolidated 对应 consolidated。
    返回 {reached, stages_completed, elapsed_ms}。超时 reached=False(降级 async)。"""
    import time as _t
    cfg = load_config()
    t0 = _t.time()
    target_kinds = {"captured": ["captured"], "indexed": ["extracted", "indexed"],
                    "consolidated": ["consolidated"]}.get(target_stage, [])
    # 先查已有(可能已处理完)
    with session_scope() as c:
        done = [r[0] for r in c.execute(text(
            "SELECT kind FROM lifecycle_events WHERE event_id=CAST(:e AS uuid) ORDER BY ts"),
            {"e": event_id}).fetchall()]
        if any(k in target_kinds for k in done):
            return {"reached": True, "stages_completed": done,
                    "elapsed_ms": int((_t.time() - t0) * 1000)}
    # LISTEN 独立连接(autocommit)
    conn = psycopg2.connect(cfg.database.url)
    conn.autocommit = True
    try:
        conn.execute("LISTEN cortex_lc")
        while _t.time() - t0 < timeout:
            # 优先查表(通知可能已积压)
            with session_scope() as c:
                done = [r[0] for r in c.execute(text(
                    "SELECT kind FROM lifecycle_events WHERE event_id=CAST(:e AS uuid) ORDER BY ts"),
                    {"e": event_id}).fetchall()]
                if any(k in target_kinds for k in done):
                    return {"reached": True, "stages_completed": done,
                            "elapsed_ms": int((_t.time() - t0) * 1000)}
            # 等 notify(最多 1s)
            import select as _sel
            _sel.select([conn], [], [], 1.0)
            conn.notices  # drain
            try:
                conn.poll()
                while conn.notifies:
                    n = conn.notifies.pop()
                    if "|" in n.payload:
                        kind, eid = n.payload.split("|", 1)
                        if eid == event_id and kind in target_kinds:
                            with session_scope() as c:
                                done = [r[0] for r in c.execute(text(
                                    "SELECT kind FROM lifecycle_events WHERE event_id=CAST(:e AS uuid) ORDER BY ts"),
                                    {"e": event_id}).fetchall()]
                            return {"reached": True, "stages_completed": done,
                                    "elapsed_ms": int((_t.time() - t0) * 1000)}
            except Exception:
                pass
        return {"reached": False, "stages_completed": [], "elapsed_ms": int((_t.time() - t0) * 1000),
                "note": "timeout, downgraded to async"}
    finally:
        conn.close()


def list_lifecycle_since(conn, *, scope: Optional[str] = None, event_id: Optional[str] = None,
                         since: Optional[str] = None, limit: int = 100):
    sql = "SELECT lifecycle_id::text, kind, ts::text, scope, event_id::text, payload FROM lifecycle_events WHERE 1=1"
    p: Dict[str, Any] = {"lim": limit}
    if scope:
        sql += " AND scope=:s"; p["s"] = scope
    if event_id:
        sql += " AND event_id=CAST(:e AS uuid)"; p["e"] = event_id
    if since:
        sql += " AND lifecycle_id > CAST(:since AS uuid)"; p["since"] = since
    sql += " ORDER BY ts ASC LIMIT :lim"
    rows = conn.execute(text(sql), p).fetchall()
    return [{"lifecycle_id": r[0], "kind": r[1], "ts": r[2], "scope": r[3],
             "event_id": r[4], "payload": r[5] or {}} for r in rows]
