"""抽取管线:extract_event(job) → 实体链接(B over C)→ facts(超替)→ beliefs 聚合 → lifecycle。

无 LLM key 走确定性 mock 抽取(管线可端到端跑);有 key 走真实 Minimax structured output。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from ..config import load_config
from ..core import emit_lifecycle
from ..db import session_scope
from .. import services


# ── vocab coerce ────────────────────────────────────────────────────────────
def coerce_value(conn, scope: str, vocab_name: str, raw: str) -> Optional[str]:
    """closed:未命中→null;open:未命中→保留;命中别名→canonical。无词表→原样。"""
    row = conn.execute(text("SELECT vocab_id, kind FROM vocabularies WHERE scope=:s AND name=:n"),
                       {"s": scope, "n": vocab_name}).fetchone()
    if not row:
        return raw
    hit = conn.execute(text("""
        SELECT vv.canonical_value FROM vocabulary_values vv WHERE vv.vocab_id=:v
        AND (vv.canonical_value=:r OR :r = ANY(vv.aliases)) LIMIT 1
    """), {"v": row.vocab_id, "r": raw}).fetchone()
    if hit:
        return hit.canonical_value
    return raw if row.kind == "open" else None


# ── entity linking (B over C) ───────────────────────────────────────────────
def _resolve_or_create(conn, scope: str, name: str, etype: Optional[str],
                       description: str, thresholds: Tuple[float, float],
                       model: str) -> str:
    """返回 entity_id。A 层别名→C 层向量召回→阈值→新建。"""
    # A 层:别名精确命中
    a = conn.execute(text("""
        SELECT e.entity_id FROM entity_aliases a JOIN entities e ON e.entity_id=a.entity_id
        WHERE a.scope=:s AND lower(a.alias)=lower(:n) AND e.merged_into IS NULL LIMIT 1
    """), {"s": scope, "n": name}).fetchone()
    if a:
        return str(a.entity_id)
    # 规范名直接命中
    nm = conn.execute(text("""
        SELECT entity_id FROM entities WHERE scope=:s AND lower(canonical_name)=lower(:n)
        AND merged_into IS NULL LIMIT 1
    """), {"s": scope, "n": name}).fetchone()
    if nm:
        return str(nm.entity_id)
    # C 层:向量召回 top-5
    emb = services.embed_one(load_config().extraction.embedding_text.format(name=name, description=description))
    cands = conn.execute(text("""
        SELECT entity_id, canonical_name, 1-(embedding <=> CAST(:q AS vector)) AS cos
        FROM entities WHERE scope=:s AND merged_into IS NULL AND embedding IS NOT NULL
        ORDER BY embedding <=> CAST(:q AS vector) LIMIT 5
    """), {"q": str(emb), "s": scope}).fetchall()
    merge_thr, new_thr = thresholds
    if cands and cands[0].cos >= merge_thr:
        return str(cands[0].entity_id)         # 直接合并(省 LLM)
    if cands and cands[0].cos >= 0.5:           # 灰区:MVP 规则版(>0.5 复用,< 则新建;有 key 可升级 LLM)
        return str(cands[0].entity_id)
    # 新建
    eid = conn.execute(text("""
        INSERT INTO entities (scope, canonical_name, entity_type, description, embedding)
        VALUES (:s,:n,:t,:d,CAST(:e AS vector)) RETURNING entity_id
    """), {"s": scope, "n": name, "t": etype, "d": description, "e": str(emb)}).fetchone().entity_id
    conn.execute(text("""
        INSERT INTO entity_aliases (entity_id, alias, alias_type, scope)
        VALUES (CAST(:e AS uuid), :n, 'canonical', :s)
    """), {"e": str(eid), "n": name, "s": scope})
    return str(eid)


def _close_superseded(conn, scope: str, subject_id: str, predicate: str, valid_from: str) -> int:
    """超替:把同 (subject,predicate) 的当前活 fact 的 valid_to 闭合为 valid_from。"""
    r = conn.execute(text("""
        UPDATE facts SET valid_to = CAST(:vf AS timestamptz)
        WHERE scope=:s AND subject_id=CAST(:sub AS uuid) AND predicate=:p
        AND valid_to IS NULL AND recorded_to IS NULL
    """), {"s": scope, "sub": subject_id, "p": predicate, "vf": valid_from})
    return r.rowcount or 0


def _insert_fact(conn, *, scope: str, subject_id: str, predicate: str,
                 object_type: str, object_entity_id: Optional[str], object_value: Optional[Dict],
                 valid_from: str, confidence: float, supports: List[str], model: str) -> str:
    fid = conn.execute(text("""
        INSERT INTO facts (scope, subject_id, predicate, object_type, object_entity_id, object_value,
                           valid_from, confidence, supports, extraction_model)
        VALUES (:s,CAST(:sub AS uuid),:p,:ot,CAST(:oe AS uuid),CAST(:ov AS jsonb),
                CAST(:vf AS timestamptz),:c,CAST(:sup AS uuid[]),:m)
        RETURNING fact_id
    """), {"s": scope, "sub": subject_id, "p": predicate, "ot": object_type,
           "oe": object_entity_id, "ov": json.dumps(object_value) if object_value else None,
           "vf": valid_from, "c": confidence,
           "sup": "{%s}" % ",".join(supports) if supports else "{}", "m": model}).fetchone().fact_id
    return str(fid)


# ── 主入口 ──────────────────────────────────────────────────────────────────
def extract_event(event_id: str) -> Dict[str, Any]:
    """对单个 event 跑抽取。返回 {facts_extracted, entities, model}。"""
    cfg = load_config()
    thresholds = (cfg.extraction.link_thresholds.merge, cfg.extraction.link_thresholds.new)
    with session_scope() as conn:
        ev = conn.execute(text("""
            SELECT scope, modality, content, context, observed_at, caller, recorded_at
            FROM events WHERE event_id=CAST(:e AS uuid)
        """), {"e": event_id}).fetchone()
        if not ev:
            return {"error": "event not found"}
        text_body = ev.content.get("text") if isinstance(ev.content, dict) else None
        if not text_body:
            # 非 message/text 类:跳过结构化抽取
            emit_lifecycle(conn, kind="extracted", scope=ev.scope, event_id=event_id,
                           payload={"facts_extracted": 0, "note": "non-text content, skipped"})
            return {"facts_extracted": 0, "model": "skip", "reason": "non-text"}

        # 抽取:真 LLM or mock
        if services.llm_configured("extraction"):
            try:
                extraction = _llm_extract(text_body)
                model = cfg.llm.extraction.model
            except Exception as e:  # noqa: BLE001
                extraction = services.mock_extract(text_body)
                model = f"mock-fallback({e.__class__.__name__})"
        else:
            extraction = services.mock_extract(text_body)
            model = "mock-extractor"

        observed_at = ev.observed_at.isoformat() if hasattr(ev.observed_at, "isoformat") else str(ev.observed_at)
        # 链接 + 建 facts
        ent_map: Dict[str, str] = {}
        for ent in extraction.get("entities", []):
            ent_map[ent["name"].lower()] = _resolve_or_create(
                conn, ev.scope, ent["name"], ent.get("type"), ent.get("description", ent["name"]),
                thresholds, model)

        fact_ids: List[str] = []
        for f in extraction.get("facts", []):
            subj = ent_map.get(f["subject"].lower())
            if not subj:
                continue
            pred = coerce_value(conn, ev.scope, "predicate", f["predicate"]) or f["predicate"]
            obj_type = f.get("object_type", "entity")
            if obj_type == "entity":
                obj_eid = ent_map.get(f["object"].lower())
                if not obj_eid:
                    continue
                _close_superseded(conn, ev.scope, subj, pred, observed_at)
                fid = _insert_fact(conn, scope=ev.scope, subject_id=subj, predicate=pred,
                                   object_type="entity", object_entity_id=obj_eid, object_value=None,
                                   valid_from=observed_at, confidence=0.8, supports=[event_id], model=model)
            else:
                val = coerce_value(conn, ev.scope, _guess_vocab(pred), f["object"])
                obj_value = {"datatype": "string", "value": val}
                _close_superseded(conn, ev.scope, subj, pred, observed_at)
                fid = _insert_fact(conn, scope=ev.scope, subject_id=subj, predicate=pred,
                                   object_type="literal", object_entity_id=None, object_value=obj_value,
                                   valid_from=observed_at, confidence=0.8, supports=[event_id], model=model)
            fact_ids.append(fid)

        # 简单 belief 聚合:同 subject ≥2 facts → 一个 likely_true belief
        if fact_ids:
            _aggregate_belief(conn, ev.scope, ent_map, fact_ids, observed_at, model)

        emit_lifecycle(conn, kind="extracted", scope=ev.scope, event_id=event_id,
                       job_id=None, payload={"facts_extracted": len(fact_ids), "entities": len(ent_map),
                                             "model": model})
        # embed_status 标记
        conn.execute(text("UPDATE events SET embed_status='done' WHERE event_id=CAST(:e AS uuid)"),
                     {"e": event_id})
        return {"facts_extracted": len(fact_ids), "entities": len(ent_map), "model": model,
                "fact_ids": fact_ids}


def _guess_vocab(predicate: str) -> str:
    """字面值 fact 的 object 可能属于某词表;猜词表名=谓词名。"""
    return predicate  # 无词表则原样(coerce 返回 raw)


def _aggregate_belief_for_scope(scope: str) -> int:
    """全 scope belief 聚合:每个有 live facts 的 subject,若无 belief 则建一个。返回新建数。"""
    n = 0
    with session_scope() as conn:
        rows = conn.execute(text("""
            SELECT subject_id::text, count(*) AS c, min(valid_from)::text AS vf
            FROM facts WHERE scope=:s AND valid_to IS NULL AND recorded_to IS NULL
            GROUP BY subject_id HAVING count(*) >= 2
        """), {"s": scope}).fetchall()
        for r in rows:
            subj_id, cnt, vf = r[0], r[1], r[2]
            existing = conn.execute(text("""
                SELECT belief_id FROM beliefs WHERE scope=:s AND about_entity_id=CAST(:a AS uuid)
                AND valid_to IS NULL AND recorded_to IS NULL LIMIT 1"""), {"s": scope, "a": subj_id}).fetchone()
            if existing:
                continue
            ent = conn.execute(text("SELECT canonical_name FROM entities WHERE entity_id=CAST(:e AS uuid)"),
                               {"e": subj_id}).fetchone()
            name = ent.canonical_name if ent else subj_id
            fids = [x[0] for x in conn.execute(text(
                "SELECT fact_id::text FROM facts WHERE scope=:s AND subject_id=CAST(:a AS uuid) AND valid_to IS NULL AND recorded_to IS NULL"),
                {"s": scope, "a": subj_id}).fetchall()]
            conn.execute(text("""INSERT INTO beliefs (scope, about_entity_id, stance, claim, confidence, supports, valid_from)
                VALUES (:s,CAST(:a AS uuid),'likely_true',:claim,0.7,CAST(:sup AS uuid[]),CAST(:vf AS timestamptz))"""),
                {"s": scope, "a": subj_id, "claim": f"{name} is associated with {cnt} observed facts",
                 "sup": "{" + ",".join(fids) + "}", "vf": vf or "now()"})
            n += 1
    return n


def _aggregate_belief(conn, scope: str, ent_map: Dict[str, str], fact_ids: List[str],
                      valid_from: str, model: str) -> None:
    by_subj: Dict[str, List[str]] = {}
    for fid in fact_ids:
        row = conn.execute(text("SELECT subject_id FROM facts WHERE fact_id=CAST(:f AS uuid)"),
                           {"f": fid}).fetchone()
        if row:
            by_subj.setdefault(str(row.subject_id), []).append(fid)
    for subj_id, fids in by_subj.items():
        if len(fids) < 2:
            continue
        existing = conn.execute(text("""
            SELECT belief_id FROM beliefs WHERE scope=:s AND about_entity_id=CAST(:a AS uuid)
            AND valid_to IS NULL AND recorded_to IS NULL LIMIT 1
        """), {"s": scope, "a": subj_id}).fetchone()
        if existing:
            conn.execute(text("""
                UPDATE beliefs SET last_revised_at=now(),
                    supports=ARRAY(SELECT DISTINCT unnest(supports || CAST(:new AS uuid[])))
                WHERE belief_id=CAST(:b AS uuid)
            """), {"new": "{%s}" % ",".join(fids), "b": existing.belief_id})
            continue
        ent = conn.execute(text("SELECT canonical_name FROM entities WHERE entity_id=CAST(:e AS uuid)"),
                           {"e": subj_id}).fetchone()
        name = ent.canonical_name if ent else subj_id
        conn.execute(text("""
            INSERT INTO beliefs (scope, about_entity_id, stance, claim, confidence, supports, valid_from)
            VALUES (:s,CAST(:a AS uuid),'likely_true',
                    :claim, 0.7, CAST(:sup AS uuid[]), CAST(:vf AS timestamptz))
        """), {"s": scope, "a": subj_id,
               "claim": f"{name} is associated with {len(fids)} observed facts",
               "sup": "{%s}" % ",".join(fids), "vf": valid_from})


# ── 真实 LLM 抽取(structured output)────────────────────────────────────────
_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "type": {"type": "string"},
                           "description": {"type": "string"}},
            "required": ["name"]}},
        "facts": {"type": "array", "items": {
            "type": "object",
            "properties": {"subject": {"type": "string"}, "predicate": {"type": "string"},
                           "object": {"type": "string"}, "object_type": {"type": "string"}},
            "required": ["subject", "predicate", "object"]}}},
    "required": ["entities", "facts"],
}
_SYS = ("Extract knowledge-graph triples from the text. Output JSON {entities:[{name,type,description}], "
        "facts:[{subject,predicate,object,object_type:'entity'|'literal'}]}. "
        "subject/object names must match entity names verbatim. Be concise.")


def _llm_extract(text_body: str) -> Dict[str, Any]:
    """真实 LLM 抽取 + R1 fallback 链:json_schema → json_object → prompt → 健壮解析。"""
    cfg = load_config().llm.extraction
    sys_msg = ("Extract knowledge-graph triples from the text. Output ONLY a JSON object "
               "{entities:[{name,type,description}], facts:[{subject,predicate,object,object_type}]}. "
               "subject/object names must match entity names verbatim. No prose, no thinking tags.")

    attempts = []
    modes = []
    configured = cfg.structured_output_mode
    modes.append(configured)
    for extra in ("json_object", "prompt"):
        if extra not in modes:
            modes.append(extra)

    last_err = None
    for mode in modes:
        if mode == "json_schema":
            rf = {"type": "json_schema", "json_schema": {"name": "extraction", "schema": _SCHEMA}}
        elif mode == "json_object":
            rf = {"type": "json_object"}
        else:
            rf = None
        try:
            raw = services.llm_chat("extraction", sys_msg, text_body, response_format=rf)
            data = services.parse_llm_json(raw)
            if isinstance(data, dict) and ("facts" in data or "entities" in data):
                attempts.append(mode)
                data["_mode"] = mode
                return data
            last_err = f"mode {mode}: parsed but no facts/entities keys"
        except Exception as e:  # noqa: BLE001
            last_err = f"mode {mode}: {type(e).__name__} {str(e)[:120]}"
            continue
    raise RuntimeError(f"all extraction modes failed: {last_err}")
