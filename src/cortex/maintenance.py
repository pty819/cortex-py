"""Memory evolution:methylation(软剪枝长期不召回)+ consolidation(去重同实体 facts)。

methylation:access_count=0 且超阈值的 event → excluded_from_recall=true + methylated_at(可逆,不删 WAL)。
consolidation:同 (subject,predicate,object) 重复 live facts → 保留最新,其余 recorded_to 闭合(F1 软关语义)。
"""
from __future__ import annotations

from typing import Dict, Any

from sqlalchemy import text

from .db import session_scope

# ── 诊断场景因果 predicate 预置词表 ─────────────────────────────────────────
# (predicate, description, cardinality: 'single'=新值超替旧值, 'multi'=多值共存)
DIAGNOSIS_PREDICATES = [
    ("caused_by",      "故障由...引起",          "multi"),
    ("led_to",         "...导致",                "multi"),
    ("symptom_of",     "是...的症状",            "multi"),
    ("affects",        "...影响",                "multi"),
    ("part_of",        "...是...的组成部分",      "multi"),
    ("has_component",  "...包含",                "multi"),
    ("has_symptom",    "...表现为",              "multi"),
    ("repaired_by",    "...被...修复",            "multi"),
    ("observed_by",    "...被...发现",            "multi"),
    ("preceded_by",    "...发生在...之后(时序)",  "multi"),
    ("has_status",     "...状态为",              "single"),  # 单值:新状态超替旧状态
    ("deal_stage",     "交易阶段",               "single"),
]

def seed_diagnosis_vocab(scope: str) -> int:
    """预置诊断场景因果 predicate 闭合词表(幂等,含 cardinality)。返回新增值数。"""
    n = 0
    with session_scope() as conn:
        row = conn.execute(text("""
            INSERT INTO vocabularies (scope, name, kind, description, cardinality)
            VALUES (:s, 'predicate', 'closed', 'Diagnosis causal predicates', 'multi')
            ON CONFLICT (scope, name) DO UPDATE SET cardinality='multi'
            RETURNING vocab_id
        """), {"s": scope}).fetchone()
        if not row:
            row = conn.execute(text("SELECT vocab_id FROM vocabularies WHERE scope=:s AND name='predicate'"),
                               {"s": scope}).fetchone()
        if row:
            for pred, desc, card in DIAGNOSIS_PREDICATES:
                r = conn.execute(text("""
                    INSERT INTO vocabulary_values (vocab_id, canonical_value, aliases, cardinality)
                    VALUES (:v, :c, '{}', :card) ON CONFLICT (vocab_id, canonical_value) DO UPDATE SET cardinality=:card
                """), {"v": str(row.vocab_id), "c": pred, "card": card})
                n += r.rowcount or 0
    return n


def methylation_run(scope: str, older_than_days: int = 30) -> Dict[str, Any]:
    with session_scope() as conn:
        r = conn.execute(text("""
            UPDATE events SET excluded_from_recall=true, methylated_at=now()
            WHERE scope=:s AND excluded_from_recall=false AND access_count=0
              AND observed_at < now() - make_interval(secs => :secs)
        """), {"s": scope, "secs": float(older_than_days * 86400)})
        n = r.rowcount or 0
    return {"action": "methylation", "scope": scope, "methylated": n, "older_than_days": older_than_days}


def consolidation_run(scope: str, min_age_hours: int = 24) -> Dict[str, Any]:
    """同 (subject,predicate,object) 的重复 live facts:保留最新 valid_from,其余 recorded_to=now()。"""
    with session_scope() as conn:
        # 找重复组(>1 条 live fact 同三元组)
        dups = conn.execute(text("""
            SELECT subject_id::text, predicate, object_type,
                   coalesce(object_entity_id::text,'') AS oe, coalesce(object_value->>'value','') AS ov
            FROM facts
            WHERE scope=:s AND recorded_to IS NULL AND valid_to IS NULL
              AND extracted_at < now() - make_interval(secs => :secs)
            GROUP BY subject_id, predicate, object_type, oe, ov
            HAVING count(*) > 1
        """), {"s": scope, "secs": float(min_age_hours * 3600)}).fetchall()
        closed = 0
        for d in dups:
            # 该组按 valid_from 降序,保留首条,其余 recorded_to 闭合
            closed += conn.execute(text("""
                UPDATE facts SET recorded_to=now()
                WHERE fact_id IN (
                    SELECT fact_id FROM (
                        SELECT fact_id, row_number() OVER (ORDER BY valid_from DESC) AS rn FROM facts
                        WHERE scope=:s AND recorded_to IS NULL AND valid_to IS NULL
                          AND subject_id=CAST(:sub AS uuid) AND predicate=:p AND object_type=:ot
                          AND coalesce(object_entity_id::text,'')=:oe
                          AND coalesce(object_value->>'value','')=:ov
                    ) z WHERE rn > 1)
            """), {"s": scope, "sub": d[0], "p": d[1], "ot": d[2], "oe": d[3], "ov": d[4]}).rowcount or 0
    return {"action": "consolidation", "scope": scope, "facts_closed": closed, "groups": len(dups)}
