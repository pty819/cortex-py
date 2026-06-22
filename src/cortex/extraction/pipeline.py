"""抽取管线:extract_event(job) → 实体链接(B over C)→ facts(超替)→ beliefs 聚合 → lifecycle。

无 LLM key 走确定性 mock 抽取(管线可端到端跑);有 key 走真实 Minimax structured output。
"""
from __future__ import annotations

import json
import os
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
                       model: str, context_text: str = "") -> str:
    """返回 entity_id。A 层别名→C 层向量召回→阈值→新建。
    灰区(merge_thr > cos >= new_thr)走 LLM 判定:传入候选实体+原文上下文,LLM 决定复用/新建。
    无 LLM key 时灰区默认新建(保守,不错误合并)。"""
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
        SELECT entity_id, canonical_name, description, entity_type, 1-(embedding <=> CAST(:q AS vector)) AS cos
        FROM entities WHERE scope=:s AND merged_into IS NULL AND embedding IS NOT NULL
        ORDER BY embedding <=> CAST(:q AS vector) LIMIT 5
    """), {"q": str(emb), "s": scope}).fetchall()
    merge_thr, new_thr = thresholds
    if cands and cands[0].cos >= merge_thr:
        return str(cands[0].entity_id)         # 直接合并(省 LLM)
    if cands and cands[0].cos >= new_thr:
        # 灰区:LLM 判定(传入候选实体列表 + 原文上下文)
        best = cands[0]
        if services.llm_configured("extraction") and context_text:
            try:
                cand_list = [{"name": c.canonical_name, "type": c.entity_type,
                              "description": c.description, "cosine": round(c.cos, 3)} for c in cands[:5]]
                decision = _llm_entity_link(name, etype, description, cand_list, context_text)
                if decision.get("reuse") and decision.get("entity_name"):
                    # LLM 判定复用:找到对应的 entity_id
                    for c in cands:
                        if c.canonical_name == decision["entity_name"]:
                            return str(c.entity_id)
                # LLM 判定新建 or 无法判定 → 新建
            except Exception:  # noqa: BLE001 LLM 不可用 → 保守新建
                pass
        # 无 LLM 或 LLM 判定新建:保守新建(不错误合并)
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


def _llm_entity_link(name: str, etype: Optional[str], description: str,
                     candidates: List[Dict], context_text: str) -> Dict[str, Any]:
    """LLM 灰区判定:给定新实体 + 候选列表 + 原文上下文,决定复用哪个还是新建。
    返回 {reuse: bool, entity_name: str|None, reason: str}。"""
    from ..prompts import ENTITY_LINK_SYSTEM
    import json as _j
    payload = _j.dumps({
        "new_entity": {"name": name, "type": etype, "description": description},
        "candidates": candidates,
        "context": context_text[:2000],  # 截取上下文(超长时)
    }, ensure_ascii=False)
    raw = services.llm_chat("extraction", ENTITY_LINK_SYSTEM, payload, max_tokens=1024)
    data = services.parse_llm_json(raw)
    return {"reuse": data.get("reuse", False),
            "entity_name": data.get("entity_name"),
            "reason": data.get("reason", "")}


# 单值谓词默认集合(代码级 fallback,优先查 DB vocabularies.cardinality)。
_SINGLE_VALUE_PREDICATES = {
    "has_status", "deal_stage", "renewed_arr", "has_policy", "has_quota",
    "configured_as", "valid_value",
}


def _is_single_value(conn, scope: str, predicate: str) -> bool:
    """判断谓词是否单值(新值到达应超替旧值)。
    优先查 DB vocabulary_values.cardinality(per-value 级);无词表时用代码级 fallback。"""
    row = conn.execute(text("""
        SELECT vv.cardinality FROM vocabularies v
        JOIN vocabulary_values vv ON vv.vocab_id = v.vocab_id
        WHERE v.scope=:s AND v.name='predicate'
        AND (vv.canonical_value=:p OR :p = ANY(vv.aliases))
        LIMIT 1
    """), {"s": scope, "p": predicate}).fetchone()
    if row and row[0]:
        return row[0] == "single"
    return predicate in _SINGLE_VALUE_PREDICATES


def _close_superseded(conn, scope: str, subject_id: str, predicate: str,
                      valid_from: str, object_value: Optional[str] = None) -> int:
    """超替:仅对单值谓词,把同 (subject,predicate) 的当前活 fact 的 valid_to 闭合。
    多值谓词(has_component/caused_by/has_symptom/correlates_with 等)不超替,允许多条共存。
    cardinality 从 DB vocabularies 查(scope 级),无词表时用代码级 fallback。
    对单值谓词,闭合所有同 (subject,predicate) 的活 fact(不论 object),因为单值谓词同一时刻只有一条为真。"""
    if not _is_single_value(conn, scope, predicate):
        return 0  # 多值谓词:不超替
    # 单值谓词:闭合所有同 (subject,predicate) 的活 fact
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
    """对单个 event 跑抽取。返回 {facts_extracted, entities, model}。
    架构:LLM 调用在 DB session 外(不在事务里持有连接等外部服务,防代理超时断连)。"""
    cfg = load_config()
    thresholds = (cfg.extraction.link_thresholds.merge, cfg.extraction.link_thresholds.new)

    # ── Step 1: 加载 event(短事务)──
    with session_scope() as conn:
        ev = conn.execute(text("""
            SELECT scope, modality, content, context, observed_at, caller, recorded_at
            FROM events WHERE event_id=CAST(:e AS uuid)
        """), {"e": event_id}).fetchone()
        if not ev:
            return {"error": "event not found"}

        content = ev.content if isinstance(ev.content, dict) else {}

        # triple 直写:快,不经 LLM,留在同一事务
        if content.get("kind") == "triple":
            res = _direct_write_triple(conn, ev.scope, content.get("triple", {}),
                                       ev.observed_at, event_id, thresholds)
            emit_lifecycle(conn, kind="extracted", scope=ev.scope, event_id=event_id,
                           payload={"facts_extracted": res["facts_extracted"], "model": "triple-direct"})
            return {**res, "model": "triple-direct"}

        text_body = content.get("text")
        if content.get("kind") == "message":
            text_body = content.get("text")
        if not text_body:
            emit_lifecycle(conn, kind="extracted", scope=ev.scope, event_id=event_id,
                           payload={"facts_extracted": 0, "note": "non-text content, skipped"})
            return {"facts_extracted": 0, "model": "skip", "reason": "non-text"}

        intent = (ev.context or {}).get("intent") if isinstance(ev.context, dict) else None
        is_diagnosis = intent in ("diagnosis", "incident_retrospective", "structure")
        scope = ev.scope
        observed_at = ev.observed_at.isoformat() if hasattr(ev.observed_at, "isoformat") else str(ev.observed_at)
    # session 已关闭——下面调 LLM 不持有 DB 连接

    # ── Step 2: LLM 抽取(无 DB session)──
    if services.llm_configured("extraction"):
        try:
            extraction = _llm_extract(text_body, is_diagnosis=is_diagnosis, intent=intent)
            model = cfg.llm.extraction.model
        except Exception as e:  # noqa: BLE001
            # 真实 LLM 失败:不静默降级到 mock!标记失败,让 worker 重试。
            raise RuntimeError(f"LLM extraction failed: {type(e).__name__}: {e}") from e
    elif os.environ.get("CORTEX_ALLOW_MOCK_EXTRACTION", "false").lower() == "true":
        # 显式测试模式才用 mock
        extraction = services.mock_extract(text_body)
        model = "mock-extractor"
    else:
        # 无 key 且非测试模式:标记跳过(不伪装成功)
        with session_scope() as conn:
            emit_lifecycle(conn, kind="extracted", scope=scope, event_id=event_id,
                           payload={"facts_extracted": 0, "note": "no LLM key configured, extraction skipped"})
        return {"facts_extracted": 0, "entities": 0, "model": "skipped",
                "reason": "no LLM key (set CORTEX_LLM_EXTRACTION_API_KEY or CORTEX_ALLOW_MOCK_EXTRACTION=true)"}

    # ── Step 3: 实体链接 + 建 facts + belief 聚合(短事务)──
    with session_scope() as conn:
        ent_map: Dict[str, str] = {}
        for ent in extraction.get("entities", []):
            ent_map[ent["name"].lower()] = _resolve_or_create(
                conn, scope, ent["name"], ent.get("type"), ent.get("description", ent["name"]),
                thresholds, model, context_text=text_body)

        fact_ids: List[str] = []
        for f in extraction.get("facts", []):
            subj = ent_map.get(f["subject"].lower())
            if not subj:
                continue
            raw_pred = f["predicate"]
            pred = coerce_value(conn, scope, "predicate", raw_pred)
            # 词表归一:closed 词表未命中→原始谓词;open 词表未命中→保留
            # 不再 `or raw_pred` 绕过——如果 coerce 返回 None(closed 未命中),仍然用原始值
            # 但走词表路径,不伪装命中
            if pred is None:
                pred = raw_pred  # closed 未命中,仍用原值(但 predicate 不在词表内)
            obj_type = f.get("object_type", "entity")
            fact_conf = float(f.get("confidence", 0.8)) if f.get("confidence") else 0.8
            fact_vf = f.get("valid_from") or observed_at
            fact_vt = f.get("valid_to")  # 可选: fact 何时停止为真
            evidence_span = f.get("evidence_span")  # 可选: 源文本引用
            is_negated = f.get("negation", False)  # 可选: 否定断言("X不是Y")
            # 否定断言: confidence 取反 + 标记 evidence_span
            if is_negated:
                fact_conf = max(0.1, 1.0 - fact_conf)
                if evidence_span:
                    evidence_span = f"[NEGATED] {evidence_span}"
                else:
                    evidence_span = "[NEGATED]"
            if obj_type == "entity":
                obj_eid = ent_map.get(f["object"].lower())
                if not obj_eid:
                    continue
                _close_superseded(conn, scope, subj, pred, fact_vf, f["object"])
                fid = _insert_fact(conn, scope=scope, subject_id=subj, predicate=pred,
                                   object_type="entity", object_entity_id=obj_eid, object_value=None,
                                   valid_from=fact_vf, confidence=fact_conf, supports=[event_id], model=model)
            else:
                val = coerce_value(conn, scope, _guess_vocab(pred), f["object"])
                obj_value = {"datatype": "string", "value": val}
                if evidence_span:
                    obj_value["evidence_span"] = evidence_span
                if fact_vt:
                    obj_value["valid_to_extracted"] = fact_vt
                _close_superseded(conn, scope, subj, pred, fact_vf, val)
                fid = _insert_fact(conn, scope=scope, subject_id=subj, predicate=pred,
                                   object_type="literal", object_entity_id=None, object_value=obj_value,
                                   valid_from=fact_vf, confidence=fact_conf, supports=[event_id], model=model)
            # entity 类型的 fact 也存 evidence_span 到 fact 的 supports 旁(通过 update)
            if evidence_span and obj_type == "entity":
                conn.execute(text("UPDATE facts SET object_value = jsonb_build_object('datatype','entity','evidence_span',:es) WHERE fact_id=CAST(:fid AS uuid)"),
                             {"es": evidence_span, "fid": fid})
            fact_ids.append(fid)

        if fact_ids:
            _detect_conflicts(conn, scope)  # 检测冲突并标记
            _aggregate_belief(conn, scope, ent_map, fact_ids, observed_at, model)

        emit_lifecycle(conn, kind="extracted", scope=scope, event_id=event_id,
                       job_id=None, payload={"facts_extracted": len(fact_ids), "entities": len(ent_map),
                                             "model": model})
        conn.execute(text("UPDATE events SET embed_status='done' WHERE event_id=CAST(:e AS uuid)"),
                     {"e": event_id})
        return {"facts_extracted": len(fact_ids), "entities": len(ent_map), "model": model,
                "fact_ids": fact_ids}


def _guess_vocab(predicate: str) -> str:
    """字面值 fact 的 object 可能属于某词表;猜词表名=谓词名。"""
    return predicate  # 无词表则原样(coerce 返回 raw)


def _detect_conflicts(conn, scope: str) -> int:
    """检测冲突:同 (scope, subject, predicate) 但 object 不同,仅单值谓词。
    多值谓词(如 caused_by/has_symptom)允许多条不同 object 共存,不算冲突。"""
    rows = conn.execute(text("""
        SELECT f.subject_id::text, f.predicate,
               count(DISTINCT CASE WHEN f.object_type='entity' THEN f.object_entity_id::text
                                   ELSE f.object_value->>'value' END) AS n_objects
        FROM facts f
        WHERE f.scope=:s AND f.valid_to IS NULL AND f.recorded_to IS NULL
          AND f.object_type IS NOT NULL
        GROUP BY f.subject_id, f.predicate
        HAVING count(DISTINCT CASE WHEN f.object_type='entity' THEN f.object_entity_id::text
                                    ELSE f.object_value->>'value' END) > 1
    """), {"s": scope}).fetchall()
    n_conflicts = 0
    for r in rows:
        subj_id, pred = r[0], r[1]
        # 仅对单值谓词标记冲突
        if not _is_single_value(conn, scope, pred):
            continue
        conn.execute(text("""
            UPDATE facts
            SET object_value = CASE
                WHEN object_value IS NULL THEN jsonb_build_object('datatype','entity','conflict',true)
                ELSE object_value || jsonb_build_object('conflict',true)
            END
            WHERE scope=:s AND subject_id=CAST(:sub AS uuid) AND predicate=:p
            AND valid_to IS NULL AND recorded_to IS NULL
        """), {"s": scope, "sub": subj_id, "p": pred})
        n_conflicts += 1
    return n_conflicts


def _compute_belief_confidence(conn, scope: str, subj_id: str, fact_ids: List[str]) -> float:
    """基于证据质量计算 belief 置信度(非固定值):
    基础分 = fact 平均 confidence,加权:
      + 独立来源数(不同 event 来源 +0.1 每个)
      + 因果谓词质量(caused_by/correlates_with +0.05)
      + 排除项支持(ruled_out/confirmed_by +0.05)
    上限 0.95,下限 0.1。"""
    if not fact_ids:
        return 0.5
    # fact avg confidence
    avg_conf = conn.execute(text("""
        SELECT avg(confidence) FROM facts
        WHERE fact_id = ANY(CAST(:ids AS uuid[])) AND scope=:s
    """), {"ids": "{" + ",".join(fact_ids) + "}", "s": scope}).scalar() or 0.7
    # 独立来源数(不同 event)
    n_sources = conn.execute(text("""
        SELECT count(DISTINCT unnest(supports)) FROM facts
        WHERE fact_id = ANY(CAST(:ids AS uuid[])) AND scope=:s
    """), {"ids": "{" + ",".join(fact_ids) + "}", "s": scope}).scalar() or 0
    # 因果/相关谓词 bonus
    n_diag = conn.execute(text("""
        SELECT count(*) FROM facts
        WHERE fact_id = ANY(CAST(:ids AS uuid[])) AND scope=:s
        AND predicate IN ('caused_by','correlates_with','confirmed_by','supports')
    """), {"ids": "{" + ",".join(fact_ids) + "}", "s": scope}).scalar() or 0
    confidence = avg_conf + 0.1 * min(n_sources, 3) + 0.05 * min(n_diag, 3)
    # 冲突检测:同 subject 有 conflict 标记的 fact → confidence 降权 + stance 降级
    n_conflict = conn.execute(text("""
        SELECT count(*) FROM facts
        WHERE fact_id = ANY(CAST(:ids AS uuid[])) AND scope=:s
        AND object_value ? 'conflict'
    """), {"ids": "{" + ",".join(fact_ids) + "}", "s": scope}).scalar() or 0
    if n_conflict > 0:
        confidence *= 0.6  # 冲突 fact 存在 → belief 置信度打 6 折
    return min(0.95, max(0.1, round(confidence, 3)))


def _detect_belief_stance(conn, scope: str, subj_id: str, fact_ids: List[str]) -> str:
    """判断 belief stance:有冲突 → uncertain;有 contradicts/ruled_out → likely_false;否则 likely_true。"""
    n_conflict = conn.execute(text("""
        SELECT count(*) FROM facts
        WHERE fact_id = ANY(CAST(:ids AS uuid[])) AND scope=:s
        AND object_value ? 'conflict'
    """), {"ids": "{" + ",".join(fact_ids) + "}", "s": scope}).scalar() or 0
    if n_conflict > 0:
        return "uncertain"
    n_contradict = conn.execute(text("""
        SELECT count(*) FROM facts
        WHERE fact_id = ANY(CAST(:ids AS uuid[])) AND scope=:s
        AND predicate IN ('contradicts','ruled_out')
    """), {"ids": "{" + ",".join(fact_ids) + "}", "s": scope}).scalar() or 0
    if n_contradict > 0:
        return "likely_false"
    return "likely_true"


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
            conf = _compute_belief_confidence(conn, scope, subj_id, fids)
            stance = _detect_belief_stance(conn, scope, subj_id, fids)
            conn.execute(text("""INSERT INTO beliefs (scope, about_entity_id, stance, claim, confidence, supports, valid_from)
                VALUES (:s,CAST(:a AS uuid),:stance,:claim,:conf,CAST(:sup AS uuid[]),CAST(:vf AS timestamptz))"""),
                {"s": scope, "a": subj_id, "stance": stance,
                 "claim": f"{name} is associated with {cnt} observed facts",
                 "conf": conf, "sup": "{" + ",".join(fids) + "}", "vf": vf or "now()"})
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
        conf = _compute_belief_confidence(conn, scope, subj_id, fids)
        stance = _detect_belief_stance(conn, scope, subj_id, fids)
        conn.execute(text("""
            INSERT INTO beliefs (scope, about_entity_id, stance, claim, confidence, supports, valid_from)
            VALUES (:s,CAST(:a AS uuid),:stance,:claim,:conf,CAST(:sup AS uuid[]),CAST(:vf AS timestamptz))
        """), {"s": scope, "a": subj_id, "stance": stance,
               "claim": f"{name} is associated with {len(fids)} observed facts",
               "conf": conf, "sup": "{%s}" % ",".join(fids), "vf": valid_from})


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
            "properties": {
                "subject": {"type": "string"},
                "predicate": {"type": "string"},
                "object": {"type": "string"},
                "object_type": {"type": "string"},
                "confidence": {"type": "number", "description": "0.0-1.0, extraction confidence"},
                "valid_from": {"type": "string", "description": "ISO8601 when this fact became true (optional, defaults to event time)"},
                "valid_to": {"type": "string", "description": "ISO8601 when this fact stopped being true (optional)"},
                "evidence_span": {"type": "string", "description": "quote or reference to the source text supporting this fact"},
                "negation": {"type": "boolean", "description": "true if the text says this fact is NOT true (negated assertion)"}
            },
            "required": ["subject", "predicate", "object"]}}},
    "required": ["entities", "facts"],
}
_SYS = ("Extract knowledge-graph triples from the text. Output JSON {entities:[{name,type,description}], "
        "facts:[{subject,predicate,object,object_type:'entity'|'literal'}]}. "
        "subject/object names must match entity names verbatim. Be concise.")


# ── triple 直写(前置 agent 产出的结构化三元组,零损失)──────────────────────
def _direct_write_triple(conn, scope: str, triple: Dict[str, Any], observed_at,
                         event_id: str, thresholds) -> Dict[str, Any]:
    """content.kind=triple → 直接建 entity + fact,不经 LLM。
    triple = {subject:{name}, predicate, object:{name}, valid_from?, confidence?}"""
    if not triple or not triple.get("subject") or not triple.get("predicate"):
        return {"facts_extracted": 0, "entities": 0, "error": "incomplete triple"}
    sub_name = triple["subject"].get("name") or triple["subject"].get("id")
    obj_name = (triple.get("object") or {}).get("name") or (triple.get("object") or {}).get("id")
    pred = triple["predicate"]
    if not sub_name or not obj_name:
        return {"facts_extracted": 0, "entities": 0, "error": "missing subject/object name"}
    # 实体链接(复用 B over C)
    sid = _resolve_or_create(conn, scope, sub_name, triple["subject"].get("type"),
                             sub_name, thresholds, "triple-direct")
    oid = _resolve_or_create(conn, scope, obj_name, (triple.get("object") or {}).get("type"),
                             obj_name, thresholds, "triple-direct")
    pred = coerce_value(conn, scope, "predicate", pred) or pred  # 词表归一
    vf = triple.get("valid_from") or (observed_at.isoformat() if hasattr(observed_at, "isoformat") else str(observed_at))
    _close_superseded(conn, scope, sid, pred, vf)
    fid = _insert_fact(conn, scope=scope, subject_id=sid, predicate=pred,
                       object_type="entity", object_entity_id=oid, object_value=None,
                       valid_from=vf, confidence=triple.get("confidence", 0.9),
                       supports=[event_id], model="triple-direct")
    return {"facts_extracted": 1, "entities": 2, "fact_ids": [fid]}


def _llm_extract(text_body: str, is_diagnosis: bool = False,
                 intent: str = None) -> Dict[str, Any]:
    """真实 LLM 抽取 + R1 fallback 链。按 intent 选详细 prompt。"""
    from ..prompts import (EXTRACTION_SYSTEM_DIAGNOSIS, EXTRACTION_SYSTEM_STRUCTURE,
                           EXTRACTION_SYSTEM_GENERAL)
    cfg = load_config().llm.extraction
    # 按 intent 选 prompt(结构文档 vs 故障诊断 vs 通用)
    if intent == "structure":
        sys_msg = EXTRACTION_SYSTEM_STRUCTURE
    elif intent in ("diagnosis", "incident_retrospective") or is_diagnosis:
        sys_msg = EXTRACTION_SYSTEM_DIAGNOSIS
    else:
        sys_msg = EXTRACTION_SYSTEM_GENERAL

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
