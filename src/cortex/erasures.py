"""Erasures:GDPR 引用计数真删,4 阶段(enumerate→refcount→delete→cleanup)。

preview 产 manifest(逐 event delete vs redact);execute 按 manifest 执行:
  refcount>0 → redact(清 content + excluded_from_recall,保 id+wal_offset)
  refcount=0 → 物理删
  array_remove 清 facts/beliefs.supports;blob refcount=0 → 删。
MVP 单 scope,跳 cross_workspace / legal_hold(05 §2.4 E1)。
每 event 独立事务,避免 PG tx 中毒。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from .core import emit_lifecycle
from .db import session_scope

MANIFEST_TTL_HOURS = 24


def _event_refcount(conn, scope: str, event_id: str) -> int:
    return conn.execute(text("""
        SELECT (SELECT count(*) FROM facts   WHERE scope=:s AND CAST(:e AS uuid) = ANY(supports))
             + (SELECT count(*) FROM beliefs WHERE scope=:s AND CAST(:e AS uuid) = ANY(supports))
    """), {"s": scope, "e": event_id}).scalar() or 0


def _select_event_ids(conn, scope: str, selector: Dict[str, Any]) -> List[str]:
    """根据 selector 收集命中的 event_id。MVP:memory_ids 直接;about_entity/predicate 走 facts.supports 反查。"""
    ids: List[str] = []
    if selector.get("memory_ids"):
        ids.extend(selector["memory_ids"])
    cond = ""
    params: Dict[str, Any] = {"s": scope}
    if selector.get("about_entity"):
        cond += " AND (f.subject_id=CAST(:a AS uuid) OR f.object_entity_id=CAST(:a AS uuid))"
        params["a"] = selector["about_entity"]
    if selector.get("predicate"):
        cond += " AND f.predicate=:p"; params["p"] = selector["predicate"]
    if cond:
        rows = conn.execute(text(f"""
            SELECT DISTINCT unnest(f.supports)::text FROM facts f
            WHERE f.scope=:s{cond}
        """), params).fetchall()
        ids.extend(r[0] for r in rows)
    # 去重 + 只保留真实存在的
    if not ids:
        return []
    rows = conn.execute(text("SELECT event_id::text FROM events WHERE scope=:s AND event_id = ANY(CAST(:ids AS uuid[]))"),
                        {"s": scope, "ids": "{" + ",".join(ids) + "}"}).fetchall()
    return list({r[0] for r in rows})


def preview_erasure(*, scope: str, selector: Dict[str, Any]) -> Dict[str, Any]:
    """enumerate + refcount → manifest。落 erasure_jobs(phase=enumerate)。"""
    with session_scope() as conn:
        eids = _select_event_ids(conn, scope, selector)
        manifest_entries = []
        n_del = n_red = 0
        for eid in eids:
            rc = _event_refcount(conn, scope, eid)
            action = "redact" if rc > 0 else "delete"
            if action == "redact":
                n_red += 1
            else:
                n_del += 1
            manifest_entries.append({"event_id": eid, "action": action, "refcount": rc})
        preview_id = uuid.uuid4()
        manifest = {"events": manifest_entries,
                    "expires_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        row = conn.execute(text("""
            INSERT INTO erasure_jobs (scope, selector, phase, preview_id, manifest, refcount_breakdown)
            VALUES (:s, CAST(:sel AS jsonb), 'enumerate', :pid, CAST(:m AS jsonb), CAST(:rb AS jsonb))
            RETURNING erasure_id
        """), {"s": scope, "sel": json.dumps(selector), "pid": str(preview_id),
               "m": json.dumps(manifest), "rb": json.dumps({"events_to_delete": n_del, "events_to_redact": n_red})}).fetchone()
        eid = str(row.erasure_id)
    return {"erasure_id": eid, "preview_id": str(preview_id), "estimated_affected": {"events": len(eids)},
            "refcount_breakdown": {"events_to_delete": n_del, "events_to_redact": n_red},
            "manifest": manifest}


def get_manifest(preview_id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as conn:
        row = conn.execute(text("""
            SELECT manifest, created_at FROM erasure_jobs WHERE preview_id=CAST(:p AS uuid)
            ORDER BY created_at DESC LIMIT 1
        """), {"p": preview_id}).fetchone()
    if not row:
        return None
    age_h = (datetime.now(timezone.utc) - row.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
    if age_h > MANIFEST_TTL_HOURS:
        return {"expired": True}
    return {"manifest": row.manifest, "expired": False}


def execute_erasure(*, scope: str, selector: Optional[Dict[str, Any]] = None,
                    from_preview_id: Optional[str] = None) -> Dict[str, Any]:
    """执行 delete 阶段。from_preview_id 时校验 manifest 未 stale(否则 409)。返回 progress。"""
    if from_preview_id:
        mf = get_manifest(from_preview_id)
        if not mf or mf.get("expired"):
            raise ValueError("manifest expired or not found (re-run preview)")
        entries = mf["manifest"]["events"]
        with session_scope() as conn:
            erasure_id = conn.execute(text("""
                INSERT INTO erasure_jobs (scope, selector, phase, preview_id, manifest)
                VALUES (:s, CAST(:sel AS jsonb), 'delete', CAST(:p AS uuid), CAST(:m AS jsonb))
                RETURNING erasure_id
            """), {"s": scope, "sel": json.dumps(selector or {}), "p": from_preview_id,
                   "m": json.dumps(mf["manifest"])}).fetchone().erasure_id
    else:
        pv = preview_erasure(scope=scope, selector=selector or {})
        entries = pv["manifest"]["events"]
        erasure_id = uuid.UUID(pv["erasure_id"])

    progress = {"deleted": 0, "redacted": 0, "demoted": 0}
    for ent in entries:
        eid = ent["event_id"]
        try:
            with session_scope() as conn:
                rc = _event_refcount(conn, scope, eid)
                if rc > 0 or ent["action"] == "redact":
                    conn.execute(text("""UPDATE events SET content='{}'::jsonb, excluded_from_recall=true
                        WHERE event_id=CAST(:e AS uuid) AND scope=:s"""), {"e": eid, "s": scope})
                    progress["redacted"] += 1
                else:
                    # 物理删前先置空指向该 event 的 FK(jobs/lifecycle),否则 FK 约束挡 DELETE
                    conn.execute(text("UPDATE jobs SET event_id=NULL WHERE event_id=CAST(:e AS uuid)"), {"e": eid})
                    conn.execute(text("UPDATE lifecycle_events SET event_id=NULL WHERE event_id=CAST(:e AS uuid)"), {"e": eid})
                    conn.execute(text("DELETE FROM events WHERE event_id=CAST(:e AS uuid) AND scope=:s"),
                                 {"e": eid, "s": scope})
                    progress["deleted"] += 1
                # 清 supports 数组
                conn.execute(text("UPDATE facts   SET supports = array_remove(supports, CAST(:e AS uuid)) WHERE scope=:s"),
                             {"e": eid, "s": scope})
                conn.execute(text("UPDATE beliefs SET supports = array_remove(supports, CAST(:e AS uuid)) WHERE scope=:s"),
                             {"e": eid, "s": scope})
        except Exception:  # noqa: BLE001
            pass

    with session_scope() as conn:
        conn.execute(text("""
            UPDATE erasure_jobs SET phase='completed', progress=CAST(:p AS jsonb), completed_at=now()
            WHERE erasure_id=:i
        """), {"p": json.dumps(progress), "i": str(erasure_id)})
        emit_lifecycle(conn, kind="erasure_complete", scope=scope,
                       payload={"erasure_id": str(erasure_id), "progress": progress})
    return {"erasure_id": str(erasure_id), "phase": "completed", "progress": progress}


def get_erasure_status(erasure_id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as conn:
        row = conn.execute(text("""
            SELECT erasure_id::text, phase, progress, refcount_breakdown, created_at::text, completed_at::text
            FROM erasure_jobs WHERE erasure_id=CAST(:i AS uuid)
        """), {"i": erasure_id}).fetchone()
    if not row:
        return None
    return {"erasure_id": row[0], "phase": row[1], "progress": row[2] or {}, "refcount_breakdown": row[3],
            "created_at": row[4], "completed_at": row[5]}


def cancel_erasure(erasure_id: str) -> bool:
    with session_scope() as conn:
        r = conn.execute(text("""
            UPDATE erasure_jobs SET phase='cancelled'
            WHERE erasure_id=CAST(:i AS uuid) AND phase IN ('enumerate','delete')
        """), {"i": erasure_id})
        return (r.rowcount or 0) > 0
