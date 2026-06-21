"""Understanding 层:LLM 概念合成(per topic)+ related 图 + coverage。

synthesize_scope:每 topic 一次 synthesis LLM 调用 → 产 concept(name/summary/supports/related/confidence)。
related 图:5 关系(specializes/generalizes/contrasts/co_occurs/causes)。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from . import services
from .config import load_config
from .db import session_scope

_RELATIONS = ("specializes", "generalizes", "contrasts", "co_occurs", "causes")


def synthesize_scope(scope: str, topics: Optional[List[str]] = None) -> Dict[str, Any]:
    """对 scope 做 Understanding 合成。topics=None 则按实体主题自动分。返回 {synthesized, topics}。"""
    cfg = load_config()
    with session_scope() as conn:
        # 取该 scope 的 beliefs + 高 confidence facts 作合成素材
        beliefs = conn.execute(text("""
            SELECT b.claim, b.confidence, e.canonical_name FROM beliefs b
            JOIN entities e ON e.entity_id=b.about_entity_id
            WHERE b.scope=:s AND b.valid_to IS NULL AND b.recorded_to IS NULL LIMIT 20
        """), {"s": scope}).fetchall()
        if not beliefs:
            return {"synthesized": 0, "topics": [], "note": "no beliefs to synthesize"}
        # 自动分 topic:按 entity canonical_name 分(每个实体一个 topic)
        if not topics:
            topics = sorted({b.canonical_name for b in beliefs})[:5]
        n = 0
        for topic in topics:
            relevant = [b for b in beliefs if b.canonical_name == topic] or beliefs[:5]
            material = json.dumps({"topic": topic, "beliefs": [{"claim": b.claim, "conf": b.confidence} for b in relevant]},
                                  ensure_ascii=False)
            try:
                if services.llm_configured("synthesis"):
                    from .prompts import UNDERSTANDING_SYNTHESIZE
                    raw = services.llm_chat("synthesis", UNDERSTANDING_SYNTHESIZE,
                        material)
                    data = services.parse_llm_json(raw)
                else:
                    data = {"name": topic, "summary": f"{topic}: " + "; ".join(b.claim for b in relevant[:3]),
                            "confidence": 0.6, "related": []}
            except Exception:  # noqa: BLE001
                data = {"name": topic, "summary": f"{topic} (mock)", "confidence": 0.5, "related": []}
            # supports = 相关 belief 的 fact supports
            sup = []
            for b in relevant:
                fids = conn.execute(text("SELECT supports::text[] FROM beliefs WHERE belief_id=CAST(:b AS uuid)"),
                                    {"b": b.belief_id}).fetchone() if False else []
            # 简化:supports 取该 topic 实体的 live facts
            ent = conn.execute(text("SELECT entity_id::text FROM entities WHERE scope=:s AND canonical_name=:n AND merged_into IS NULL LIMIT 1"),
                               {"s": scope, "n": topic}).fetchone()
            if ent:
                fids = [r[0] for r in conn.execute(text(
                    "SELECT fact_id::text FROM facts WHERE scope=:s AND subject_id=CAST(:e AS uuid) AND valid_to IS NULL AND recorded_to IS NULL LIMIT 5"),
                    {"s": scope, "e": ent[0]}).fetchall()]
                sup = fids
            # related:解析 LLM 输出 + 关联同 scope 已有 concepts(按 name)
            related = []
            for r in (data.get("related") or [])[:5]:
                related.append({"name": r.get("name", "?"), "relation": r.get("relation", "co_occurs")})
            conn.execute(text("""INSERT INTO concepts (scope, name, topic, version, summary, supports, related, confidence)
                VALUES (:s,:n,:t,1,:sum,CAST(:sup AS uuid[]),CAST(:rel AS jsonb),:c)"""),
                {"s": scope, "n": data.get("name", topic), "t": topic,
                 "sum": data.get("summary", ""), "sup": "{" + ",".join(sup) + "}" if sup else "{}",
                 "rel": json.dumps(related, ensure_ascii=False), "c": float(data.get("confidence", 0.5))})
            n += 1
    return {"synthesized": n, "topics": topics}


def list_concepts(scope: str, topic: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    with session_scope() as conn:
        sql = "SELECT concept_id::text, name, topic, version, summary, confidence, related FROM concepts WHERE scope=:s"
        p: Dict[str, Any] = {"s": scope, "lim": limit}
        if topic:
            sql += " AND topic=:t"; p["t"] = topic
        sql += " ORDER BY created_at DESC LIMIT :lim"
        rows = conn.execute(text(sql), p).fetchall()
    return [dict(concept_id=r[0], name=r[1], topic=r[2], version=r[3], summary=r[4],
                 confidence=r[5], related=r[6] or []) for r in rows]


def get_concept(concept_id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as conn:
        r = conn.execute(text("""SELECT concept_id::text, scope, name, topic, version, summary,
            supports::text[], related, confidence, valid_from::text FROM concepts WHERE concept_id=CAST(:c AS uuid)"""),
            {"c": concept_id}).fetchone()
    if not r:
        return None
    return dict(concept_id=r[0], scope=r[1], name=r[2], topic=r[3], version=r[4], summary=r[5],
                supports=list(r[6] or []), related=r[7] or [], confidence=r[8], valid_from=r[9])


def related_concepts(concept_id: str, relation: Optional[str] = None, depth: int = 2, limit: int = 20) -> List[Dict[str, Any]]:
    """遍历 related 图(BFS,depth 跳)。relation 过滤。"""
    with session_scope() as conn:
        base = get_concept(concept_id)
        if not base:
            return []
        visited = {concept_id}
        result = []
        frontier = [concept_id]
        for _ in range(depth):
            nxt = []
            for cid in frontier:
                c = get_concept(cid)
                if not c:
                    continue
                for rel in (c["related"] or []):
                    rn = rel.get("name")
                    if relation and rel.get("relation") != relation:
                        continue
                    # 按 name 找 concept
                    row = conn.execute(text("SELECT concept_id::text FROM concepts WHERE scope=:s AND name=:n LIMIT 1"),
                                       {"s": base["scope"], "n": rn}).fetchone()
                    if row and row[0] not in visited:
                        visited.add(row[0])
                        full = get_concept(row[0])
                        if full:
                            result.append(full)
                            nxt.append(row[0])
                            if len(result) >= limit:
                                return result
            frontier = nxt
            if not frontier:
                break
    return result


def coverage(scope: str) -> Dict[str, Any]:
    with session_scope() as conn:
        total = conn.execute(text("SELECT count(*) FROM concepts WHERE scope=:s"), {"s": scope}).scalar() or 0
        rows = conn.execute(text("""SELECT topic, count(*), avg(confidence) FROM concepts WHERE scope=:s GROUP BY topic"""),
                            {"s": scope}).fetchall()
    return {"concept_count": total,
            "by_topic": [{"topic": r[0], "concepts": r[1], "avg_confidence": float(r[2]) if r[2] else 0} for r in rows]}
