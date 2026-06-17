"""Stage 6:批量写 / 5 个导入器 / 导入状态。

bulk_ingest:experience/bulk 的核心,逐条 append_event + enqueue extract。
importer:jsonl/mem0/letta/openai 把源记录映射成 envelope 复用 bulk 路径;
         zep 特殊——双时态 facts 直写(跳过抽取,尊重源 valid_from/valid_to)。
scope_template:{field} 占位,从每条记录取值。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from .core import append_event, enqueue_job, emit_lifecycle
from .db import session_scope


# ── scope_template 解析 ─────────────────────────────────────────────────────
def resolve_scope(template: Optional[str], record: Dict[str, Any], default: str) -> str:
    """{field} 占位替换:record 有则填,无则保留字面量。无 template → default。"""
    if not template:
        return default
    return re.sub(r"\{(\w+)\}", lambda m: str(record.get(m.group(1), m.group(0))), template)


# ── 通用 bulk 路径 ──────────────────────────────────────────────────────────
def bulk_ingest(*, scope: str, items: List[Dict[str, Any]], source: str = "bulk",
                ordering: str = "strict_temporal", caller: str = "importer") -> Dict[str, Any]:
    """items:每项是 envelope dict(modality/content/context/idempotency_key + 可选 observed_actor/subject/directives)。
    逐条 append + enqueue。返回 {import_id, source, accepted, failed}。"""
    total = len(items)
    with session_scope() as conn:
        ijid = conn.execute(text("""
            INSERT INTO import_jobs (scope, source, status, total, ordering)
            VALUES (:s,:src,'running',:n,:o) RETURNING import_id
        """), {"s": scope, "src": source, "n": total, "o": ordering}).fetchone().import_id
        emit_lifecycle(conn, kind="import_progress", scope=scope, batch_id=str(ijid),
                       payload={"phase": "started", "total": total})

    accepted = failed = 0
    for item in items:
        try:
            eid, _ = append_event(
                scope=item["scope"], modality=item.get("modality", "conversation"),
                content=item["content"], context=item.get("context", {}),
                caller=caller, observed_actor=item.get("observed_actor"),
                subject=item.get("subject"), directives=item.get("directives"),
                idempotency_key=item["idempotency_key"], observed_at=item.get("observed_at"))
            enqueue_job(job_type="extract", scope=item["scope"], event_id=eid, priority=-1)
            accepted += 1
            if accepted % 25 == 0:
                with session_scope() as conn:
                    conn.execute(text("UPDATE import_jobs SET accepted=:a, failed=:f WHERE import_id=:i"),
                                 {"a": accepted, "f": failed, "i": str(ijid)})
                    emit_lifecycle(conn, kind="import_progress", scope=scope, batch_id=str(ijid),
                                   payload={"accepted": accepted, "failed": failed, "total": total})
        except Exception:  # noqa: BLE001 单条失败不致命
            failed += 1

    with session_scope() as conn:
        conn.execute(text("""
            UPDATE import_jobs SET status='completed', accepted=:a, failed=:f, completed_at=now()
            WHERE import_id=:i
        """), {"a": accepted, "f": failed, "i": str(ijid)})
        emit_lifecycle(conn, kind="import_complete", scope=scope, batch_id=str(ijid),
                       payload={"accepted": accepted, "failed": failed, "total": total})
    return {"import_id": str(ijid), "source": source, "accepted": accepted, "failed": failed}


def _run_envelope_import(*, records: List[Dict[str, Any]], scope: str, source: str,
                         mapper, scope_template: Optional[str], ordering: str = "strict_temporal") -> Dict[str, Any]:
    """通用:records → mapper(record,scope,template)→envelope dict → bulk_ingest。"""
    items = [mapper(r, scope, scope_template) for r in records]
    return bulk_ingest(scope=scope, items=items, source=source, ordering=ordering)


def get_import_status(import_id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as conn:
        row = conn.execute(text("""
            SELECT import_id::text, source, status, accepted, failed, total
            FROM import_jobs WHERE import_id=CAST(:i AS uuid)
        """), {"i": import_id}).fetchone()
    if not row:
        return None
    return {"import_id": row[0], "source": row[1], "status": row[2],
            "accepted": row[3], "failed": row[4], "total": row[5]}


# ── 5 个导入器映射 ──────────────────────────────────────────────────────────
def _key(rec: Dict, field: str, idx: int) -> str:
    return str(rec.get(field) or f"{field}-{idx}-{int(time.time()*1000)%100000}")


def jsonl_mapper(rec: Dict, scope: str, template: Optional[str]) -> Dict[str, Any]:
    """jsonl 行 = envelope(减 scope)。取 modality/content/context/idempotency_key。"""
    sc = resolve_scope(template, rec, scope)
    content = rec.get("content") or {"kind": "text", "text": rec.get("text", "")}
    return {"scope": sc, "modality": rec.get("modality", "conversation"),
            "content": content, "context": rec.get("context", {}),
            "observed_at": (rec.get("context") or {}).get("observed_at"),
            "idempotency_key": rec.get("idempotency_key") or _key(rec, "ik", hash(json.dumps(rec, sort_keys=True)) & 0xffff)}


def mem0_mapper(rec: Dict, scope: str, template: Optional[str]) -> Dict[str, Any]:
    sc = resolve_scope(template, rec, scope)
    meta = rec.get("metadata") or {}
    return {"scope": sc, "modality": "conversation",
            "content": {"kind": "message", "role": "user", "text": rec.get("memory", "")},
            "context": {"observed_at": rec.get("timestamp"),
                        "labels": meta.get("labels", []),
                        "intent": meta.get("intent")},
            "observed_at": rec.get("timestamp"),
            "idempotency_key": _key(rec, "memory", hash(str(rec.get("memory", ""))) & 0xffff)}


def letta_mapper(rec: Dict, scope: str, template: Optional[str]) -> Dict[str, Any]:
    sc = resolve_scope(template, rec, scope)
    return {"scope": sc, "modality": "document",
            "content": {"kind": "text", "text": rec.get("text", "")},
            "context": {"intent": rec.get("label")},
            "idempotency_key": _key(rec, "label", hash(str(rec.get("text", ""))) & 0xffff)}


def openai_mapper(rec: Dict, scope: str, template: Optional[str]) -> Dict[str, Any]:
    sc = resolve_scope(template, rec, scope)
    return {"scope": sc, "modality": "observation",
            "content": {"kind": "text", "text": rec.get("content", "")},
            "context": {},
            "idempotency_key": rec.get("id") or _key(rec, "oai", hash(str(rec.get("content", ""))) & 0xffff)}


# ── zep:双时态 facts 直写(跳过抽取)─────────────────────────────────────────
def import_zep_direct(*, scope: str, facts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Zep facts 直接落 entities + facts,保留源 valid_from/valid_to,不抽不走 LLM。
    每条 fact 独立事务(一条失败不污染其余),捕获首错误用于诊断。"""
    from .extraction.pipeline import _resolve_or_create, _insert_fact
    from .config import load_config
    cfg = load_config()
    thr = (cfg.extraction.link_thresholds.merge, cfg.extraction.link_thresholds.new)
    total = len(facts)
    with session_scope() as conn:
        ijid = conn.execute(text("""
            INSERT INTO import_jobs (scope, source, status, total, ordering)
            VALUES (:s,'zep','running',:n,'strict_temporal') RETURNING import_id
        """), {"s": scope, "n": total}).fetchone().import_id

    accepted = failed = 0
    first_err: Optional[str] = None
    for f in facts:
        try:
            with session_scope() as conn:
                subj, obj = f["subject"], f["object"]
                sid = _resolve_or_create(conn, scope, subj, None, subj, thr, "zep-import")
                oid = _resolve_or_create(conn, scope, obj, None, obj, thr, "zep-import")
                vf = f.get("valid_from") or "2026-01-01T00:00:00Z"
                fid = _insert_fact(conn, scope=scope, subject_id=sid, predicate=f["predicate"],
                                   object_type="entity", object_entity_id=oid, object_value=None,
                                   valid_from=vf, confidence=f.get("confidence", 0.8),
                                   supports=[], model="zep-import")
                if f.get("valid_to"):
                    conn.execute(text("UPDATE facts SET valid_to=CAST(:vt AS timestamptz) WHERE fact_id=CAST(:fid AS uuid)"),
                                 {"vt": f["valid_to"], "fid": fid})
            accepted += 1
        except Exception as e:  # noqa: BLE001 单条失败不致命
            failed += 1
            if first_err is None:
                first_err = f"{type(e).__name__}: {str(e)[:200]}"

    with session_scope() as conn:
        conn.execute(text("""
            UPDATE import_jobs SET status='completed', accepted=:a, failed=:f, completed_at=now()
            WHERE import_id=:i
        """), {"a": accepted, "f": failed, "i": str(ijid)})
        emit_lifecycle(conn, kind="import_complete", scope=scope, batch_id=str(ijid),
                       payload={"source": "zep", "accepted": accepted, "failed": failed,
                                "total": total, "first_error": first_err})
    return {"import_id": str(ijid), "source": "zep", "accepted": accepted,
            "failed": failed, "first_error": first_err}


# ── 对外入口 ────────────────────────────────────────────────────────────────
def import_jsonl(scope: str, lines: str, scope_template: Optional[str] = None) -> Dict[str, Any]:
    records = [json.loads(ln) for ln in lines.splitlines() if ln.strip()]
    return _run_envelope_import(records=records, scope=scope, source="jsonl",
                                mapper=jsonl_mapper, scope_template=scope_template)


def import_mem0(scope: str, memories: List[Dict], scope_template: Optional[str] = None) -> Dict[str, Any]:
    return _run_envelope_import(records=memories, scope=scope, source="mem0",
                                mapper=mem0_mapper, scope_template=scope_template)


def import_letta(scope: str, blocks: List[Dict], scope_template: Optional[str] = None) -> Dict[str, Any]:
    return _run_envelope_import(records=blocks, scope=scope, source="letta",
                                mapper=letta_mapper, scope_template=scope_template)


def import_openai_mem(scope: str, memories: List[Dict], scope_template: Optional[str] = None) -> Dict[str, Any]:
    return _run_envelope_import(records=memories, scope=scope, source="openai",
                                mapper=openai_mapper, scope_template=scope_template)
