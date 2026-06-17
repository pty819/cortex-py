"""Stage 6:导出某 scope 的 events + facts + beliefs 为 JSONL(可回灌)。

events 以 envelope 形式导出 → /import/jsonl 可直接回灌(重新抽取);
facts 导出为 zep 兼容三元组 → /import/zep 可直写;
beliefs 导出为只读快照( informational)。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

from sqlalchemy import text

from .db import session_scope


def export_scope(scope: str) -> Dict[str, Any]:
    """导出 scope 下 events + facts + beliefs → JSONL 文本。返回 {export_id, format, bytes, data}。"""
    lines: List[str] = []
    with session_scope() as conn:
        evs = conn.execute(text("""
            SELECT event_id::text, modality, content, context, observed_actor, subject,
                   observed_at::text, recorded_at::text, idempotency_key
            FROM events WHERE scope=:s AND excluded_from_recall=false ORDER BY observed_at
        """), {"s": scope}).fetchall()
        for e in evs:
            lines.append(json.dumps({
                "type": "event",
                "scope": scope,
                "modality": e.modality,
                "content": e.content,
                "context": e.context,
                "observed_actor": e.observed_actor,
                "subject": e.subject,
                "observed_at": e.observed_at,
                "idempotency_key": e.idempotency_key,
            }, ensure_ascii=False))

        facts = conn.execute(text("""
            SELECT f.predicate, f.object_type, f.object_value, f.valid_from::text, f.valid_to::text,
                   f.confidence, s.canonical_name AS sname, o.canonical_name AS oname
            FROM facts f JOIN entities s ON s.entity_id=f.subject_id
            LEFT JOIN entities o ON o.entity_id=f.object_entity_id
            WHERE f.scope=:s AND f.recorded_to IS NULL ORDER BY f.valid_from
        """), {"s": scope}).fetchall()
        for f in facts:
            lines.append(json.dumps({
                "type": "fact",
                "scope": scope,
                "subject": f.sname,
                "predicate": f.predicate,
                "object": f.oname or (f.object_value or {}).get("value"),
                "object_type": f.object_type,
                "valid_from": f.valid_from,
                "valid_to": f.valid_to,
                "confidence": f.confidence,
            }, ensure_ascii=False))

        bels = conn.execute(text("""
            SELECT b.stance, b.claim, b.confidence, e.canonical_name, b.valid_from::text
            FROM beliefs b JOIN entities e ON e.entity_id=b.about_entity_id
            WHERE b.scope=:s AND b.valid_to IS NULL AND b.recorded_to IS NULL
        """), {"s": scope}).fetchall()
        for b in bels:
            lines.append(json.dumps({
                "type": "belief", "scope": scope, "about": b.canonical_name,
                "stance": b.stance, "claim": b.claim, "confidence": b.confidence,
                "valid_from": b.valid_from,
            }, ensure_ascii=False))

    data = "\n".join(lines) + ("\n" if lines else "")
    return {"export_id": "export_" + uuid.uuid4().hex[:24], "format": "jsonl",
            "bytes": len(data.encode("utf-8")), "data": data}
