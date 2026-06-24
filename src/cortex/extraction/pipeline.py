"""抽取管线:extract_event(job) → 实体链接(B over C)→ facts(超替)→ beliefs 聚合 → lifecycle。

无 LLM key 默认终止；只有显式本地验证开关允许确定性 mock。有 key 走真实 structured output。
"""
from __future__ import annotations

import json
import os
import re
import uuid
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from ..config import load_config
from ..core import emit_lifecycle
from ..db import session_scope
from ..ontology import (CAUSAL_PREDICATES, OPPOSING_PREDICATES,
                        RELATIONAL_EXCLUSION_PREDICATES, PREDICATE_CARDINALITY,
                        DIAGNOSIS_PREDICATE_NAMES)
from .. import services


class ExtractionConfigurationError(RuntimeError):
    """静态抽取配置缺失；重试不会自行恢复。"""


class ExtractionValidationError(ValueError):
    """抽取结果违反闭集或时态不变量。"""


_CONTEXT_FIELDS = ("fab", "equipment", "module", "chamber", "recipe", "recipe_revision")


def _parse_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _canonical_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).strip().split()).casefold()


def canonical_identity_context(context: Optional[Dict[str, Any]]) -> Dict[str, str]:
    raw = context or {}
    values = {
        "fab": raw.get("fab"),
        "equipment": raw.get("tool") or raw.get("equipment"),
        "module": raw.get("module"),
        "chamber": raw.get("chamber"),
        "recipe": raw.get("recipe"),
        "recipe_revision": raw.get("recipe_revision") or raw.get("recipe_rev"),
    }
    return {key: canon for key in _CONTEXT_FIELDS if (canon := _canonical_text(values.get(key) or ""))}


def context_key(context: Optional[Dict[str, Any]]) -> str:
    return json.dumps(canonical_identity_context(context), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def _identity_context_for_type(context: Optional[Dict[str, Any]], entity_type: Optional[str]) -> Dict[str, Any]:
    """只把稳定定位字段用于物理/配方实体；故障、材料、状态等概念不按 chamber/recipe 分裂。"""
    canonical = canonical_identity_context(context)
    etype = _canonical_text(entity_type or "")
    if etype in {"equipment", "tool"}:
        allowed = {"fab", "equipment"}
    elif etype in {"module", "chamber", "component", "sensor", "subsystem"}:
        allowed = {"fab", "equipment", "module", "chamber"}
    elif etype in {"recipe", "process_step", "process_param"}:
        allowed = set(_CONTEXT_FIELDS)
    else:
        allowed = set()
    return {key: value for key, value in canonical.items() if key in allowed}


def _assertion_semantics(predicate: str, fact: Dict[str, Any], *, trusted: bool = False,
                         source_text: Optional[str] = None) -> Tuple[str, str]:
    polarity = "negative" if fact.get("negation") else fact.get("polarity", "positive")
    requested = fact.get("assertion_status")
    if predicate in OPPOSING_PREDICATES:
        return "negative", "ruled_out"
    if predicate in RELATIONAL_EXCLUSION_PREDICATES:
        return "positive", requested or "observed"
    if predicate in CAUSAL_PREDICATES:
        if polarity == "negative" or requested in {"ruled_out", "rejected"}:
            return polarity, "ruled_out"
        evidence = str(fact.get("evidence_span") or "").strip()
        grounded_in_source = bool(source_text and evidence and evidence in source_text)
        if requested == "confirmed" and evidence and (trusted or grounded_in_source):
            return polarity, "confirmed"
        return polarity, "hypothesized"
    return polarity, requested or "observed"


def _validate_extraction_config(cfg) -> None:
    tier = cfg.llm.extraction
    parsed = urlparse(tier.api_base)
    if not tier.model.strip() or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ExtractionConfigurationError("invalid extraction LLM model or api_base")


_IDENTITY_SENSITIVE_TYPES = {
    "component", "sensor", "controller", "process_param", "measurement",
    "metrology_result", "recipe", "recipe_revision",
}


def _critical_identity_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    identifiers = re.findall(r"[a-z]+(?:[-_][a-z]+)*[-_]?\d+(?:\.\d+)?[a-z%°μ]*", normalized)
    quantities = re.findall(
        r"\d+(?:\.\d+)?\s*(?:kw|w|v|a|ma|pa|torr|mtorr|sccm|slm|°?c|nm|um|μm|%|hz|khz|mhz)",
        normalized,
    )
    return tuple(sorted(set(identifiers + quantities)))


def _identity_candidate_compatible(name: str, candidate_name: str, entity_type: Optional[str]) -> bool:
    if _canonical_text(entity_type or "") not in _IDENTITY_SENSITIVE_TYPES:
        return True
    incoming = _critical_identity_tokens(name)
    existing = _critical_identity_tokens(candidate_name)
    return not (incoming or existing) or incoming == existing


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
                       model: str, context_text: str = "",
                       identity_context: Optional[Dict[str, Any]] = None) -> str:
    """返回 entity_id。A 层别名→C 层向量召回→阈值→新建。
    灰区(merge_thr > cos >= new_thr)走 LLM 判定:传入候选实体+原文上下文,LLM 决定复用/新建。
    无 LLM key 时灰区默认新建(保守,不错误合并)。"""
    # A 层:别名精确命中
    canonical_ctx = _identity_context_for_type(identity_context, etype)
    ctx_key = json.dumps(canonical_ctx, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    exact = conn.execute(text("""
        SELECT DISTINCT e.entity_id, e.context_key FROM entities e
        LEFT JOIN entity_aliases a ON a.entity_id=e.entity_id
        WHERE e.scope=:s AND e.merged_into IS NULL
          AND (lower(e.canonical_name)=lower(:n) OR lower(a.alias)=lower(:n))
          AND ((:t IS NULL AND e.entity_type IS NULL) OR e.entity_type=:t)
        ORDER BY e.entity_id
    """), {"s": scope, "n": name, "t": etype}).fetchall()
    if canonical_ctx:
        matches = [r for r in exact if r.context_key == ctx_key]
        if len(matches) == 1:
            return str(matches[0].entity_id)
        legacy = [r for r in exact if r.context_key == "{}"]
        if not matches and len(exact) == 1 and len(legacy) == 1:
            conn.execute(text("""UPDATE entities SET identity_context=CAST(:ctx AS jsonb), context_key=:ck,
                                 updated_at=now() WHERE entity_id=:e"""),
                         {"ctx": json.dumps(canonical_ctx, ensure_ascii=False), "ck": ctx_key,
                          "e": legacy[0].entity_id})
            return str(legacy[0].entity_id)
    elif len(exact) == 1:
        return str(exact[0].entity_id)
    # 规范名直接命中
    # 同名但上下文不同必须保守分离，不能进入向量强制合并。
    if exact:
        cands = []
    else:
        cands = None
    # C 层:向量召回 top-5
    emb = services.embed_one(load_config().extraction.embedding_text.format(name=name, description=description), role="passage")
    cands = cands if cands is not None else conn.execute(text("""
        SELECT entity_id, canonical_name, description, entity_type, context_key,
               1-(embedding <=> CAST(:q AS vector)) AS cos
        FROM entities WHERE scope=:s AND merged_into IS NULL AND embedding IS NOT NULL
          AND ((:t IS NULL AND entity_type IS NULL) OR entity_type=:t)
          AND context_key=:ck
        ORDER BY embedding <=> CAST(:q AS vector) LIMIT 5
    """), {"q": str(emb), "s": scope, "t": etype, "ck": ctx_key}).fetchall()
    cands = [c for c in cands if _identity_candidate_compatible(name, c.canonical_name, etype)]
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
        INSERT INTO entities (scope, canonical_name, entity_type, description, embedding,
                              identity_context, context_key)
        VALUES (:s,:n,:t,:d,CAST(:e AS vector),CAST(:ctx AS jsonb),:ck) RETURNING entity_id
    """), {"s": scope, "n": name, "t": etype, "d": description, "e": str(emb),
             "ctx": json.dumps(canonical_ctx, ensure_ascii=False), "ck": ctx_key}).fetchone().entity_id
    conn.execute(text("""
        INSERT INTO entity_aliases (entity_id, alias, alias_type, scope)
        VALUES (CAST(:e AS uuid), :n, 'canonical', :s) ON CONFLICT DO NOTHING
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
        "context": context_text,  # 原始上下文原文
    }, ensure_ascii=False)
    raw = services.llm_chat("extraction", ENTITY_LINK_SYSTEM, payload, max_tokens=1024)
    data = services.parse_llm_json(raw)
    return {"reuse": data.get("reuse", False),
            "entity_name": data.get("entity_name"),
            "reason": data.get("reason", "")}


# 单值谓词默认集合(代码级 fallback,优先查 DB vocabularies.cardinality)。
_SINGLE_VALUE_PREDICATES = {
    "has_status", "deal_stage", "renewed_arr", "has_policy", "has_quota",
    "valid_value",
} | {predicate for predicate, cardinality in PREDICATE_CARDINALITY.items() if cardinality == "single"}


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
                      valid_from: str, object_value: Optional[str] = None) -> Optional[str]:
    """超替:仅对单值谓词,把同 (subject,predicate) 的当前活 fact 的 valid_to 闭合。
    多值谓词(has_component/caused_by/has_symptom/correlates_with 等)不超替,允许多条共存。
    cardinality 从 DB vocabularies 查(scope 级),无词表时用代码级 fallback。
    对单值谓词,闭合所有同 (subject,predicate) 的活 fact(不论 object),因为单值谓词同一时刻只有一条为真。"""
    if not _is_single_value(conn, scope, predicate):
        return None
    rows = conn.execute(text("""
        SELECT fact_id::text, valid_from::text, valid_to::text
        FROM facts WHERE scope=:s AND subject_id=CAST(:sub AS uuid) AND predicate=:p
          AND recorded_to IS NULL AND polarity='positive'
          AND assertion_status IN ('observed','confirmed')
        ORDER BY valid_from, recorded_from
        FOR UPDATE
    """), {"s": scope, "sub": subject_id, "p": predicate}).fetchall()
    target = _parse_timestamp(valid_from)
    predecessor = None
    successor = None
    for row in rows:
        point = _parse_timestamp(row.valid_from)
        if point == target:
            conn.execute(text("UPDATE facts SET recorded_to=now() WHERE fact_id=CAST(:f AS uuid)"), {"f": row.fact_id})
            return row.valid_to
        if point < target:
            predecessor = row
        elif successor is None:
            successor = row
    if predecessor and (predecessor.valid_to is None or
                        _parse_timestamp(predecessor.valid_to) > target):
        conn.execute(text("UPDATE facts SET recorded_to=now() WHERE fact_id=CAST(:f AS uuid)"), {"f": predecessor.fact_id})
        conn.execute(text("""
            INSERT INTO facts(scope,subject_id,predicate,object_type,object_entity_id,object_value,
                              valid_from,valid_to,confidence,polarity,assertion_status,evidence_span,
                              supports,extraction_model,extracted_at)
            SELECT scope,subject_id,predicate,object_type,object_entity_id,object_value,
                   valid_from,CAST(:vf AS timestamptz),confidence,polarity,assertion_status,evidence_span,
                   supports,extraction_model,extracted_at
            FROM facts WHERE fact_id=CAST(:f AS uuid)
        """), {"vf": valid_from, "f": predecessor.fact_id})
    return successor.valid_from if successor else None


def _insert_fact(conn, *, scope: str, subject_id: str, predicate: str,
                 object_type: str, object_entity_id: Optional[str], object_value: Optional[Dict],
                 valid_from: str, confidence: float, supports: List[str], model: str,
                 valid_to: Optional[str] = None, polarity: str = "positive",
                 assertion_status: str = "observed", evidence_span: Optional[str] = None) -> str:
    if valid_to:
        start = _parse_timestamp(valid_from)
        end = _parse_timestamp(valid_to)
        if end <= start:
            raise ExtractionValidationError("valid_to must be later than valid_from")
    fid = conn.execute(text("""
        INSERT INTO facts (scope, subject_id, predicate, object_type, object_entity_id, object_value,
                           valid_from, valid_to, confidence, polarity, assertion_status, evidence_span,
                           supports, extraction_model)
        VALUES (:s,CAST(:sub AS uuid),:p,:ot,CAST(:oe AS uuid),CAST(:ov AS jsonb),
                CAST(:vf AS timestamptz),CAST(:vt AS timestamptz),:c,:pol,:ast,:es,CAST(:sup AS uuid[]),:m)
        RETURNING fact_id
    """), {"s": scope, "sub": subject_id, "p": predicate, "ot": object_type,
           "oe": object_entity_id, "ov": json.dumps(object_value) if object_value else None,
           "vf": valid_from, "vt": valid_to, "c": confidence, "pol": polarity,
           "ast": assertion_status, "es": evidence_span,
           "sup": "{%s}" % ",".join(supports) if supports else "{}", "m": model}).fetchone().fact_id
    return str(fid)


def _effective_valid_to(valid_from: str, explicit: Optional[str], inferred: Optional[str]) -> Optional[str]:
    """合并显式区间与相邻版本边界；显式区间不得越过 successor。"""
    if explicit and inferred:
        explicit_dt = _parse_timestamp(explicit)
        inferred_dt = _parse_timestamp(inferred)
        if explicit_dt > inferred_dt:
            raise ExtractionValidationError("explicit valid_to overlaps the next single-value version")
    chosen = explicit or inferred
    if chosen:
        start = _parse_timestamp(valid_from)
        end = _parse_timestamp(chosen)
        if end <= start:
            raise ExtractionValidationError("valid_to must be later than valid_from")
    return chosen


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
                                       ev.observed_at, event_id, thresholds, ev.context or {})
            emit_lifecycle(conn, kind="extracted", scope=ev.scope, event_id=event_id,
                           payload={"facts_extracted": res["facts_extracted"],
                                    "accepted_facts": res.get("accepted_facts", res["facts_extracted"]),
                                    "rejected_facts": res.get("rejected_facts", 0),
                                    "model": "triple-direct"})
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
        _validate_extraction_config(cfg)
        try:
            extraction = _llm_extract(text_body, is_diagnosis=is_diagnosis, intent=intent)
            model = cfg.llm.extraction.model
        except ExtractionConfigurationError:
            raise
        except Exception as e:  # noqa: BLE001
            # 真实 LLM 失败:不静默降级到 mock!标记失败,让 worker 重试。
            raise RuntimeError(f"LLM extraction failed: {type(e).__name__}: {e}") from e
    elif os.environ.get("CORTEX_ALLOW_MOCK_EXTRACTION", "false").lower() == "true":
        # 显式测试模式才用 mock
        extraction = services.mock_extract(text_body)
        model = "mock-extractor"
    else:
        # 静态配置错误是终止失败；不能发出 extracted/indexed 成功状态。
        raise ExtractionConfigurationError(
            "no extraction LLM key configured (set CORTEX_LLM_EXTRACTION_API_KEY or explicit test mock)"
        )

    # ── Step 3: 实体链接 + 建 facts + belief 聚合(短事务)──
    with session_scope() as conn:
        accepted: List[Dict[str, Any]] = []
        referenced_names = set()
        for raw_fact in extraction.get("facts", []):
            normalized = dict(raw_fact)
            pred = coerce_value(conn, scope, "predicate", raw_fact["predicate"])
            if pred is None:
                _quarantine(conn, event_id, raw_fact["predicate"], raw_fact.get("object"),
                            "unknown_closed_predicate", raw_fact.get("evidence_span"), model)
                continue
            normalized["_predicate"] = pred
            accepted.append(normalized)
            referenced_names.add(str(raw_fact.get("subject", "")).casefold())
            if raw_fact.get("object_type", "entity") == "entity":
                referenced_names.add(str(raw_fact.get("object", "")).casefold())

        ent_map: Dict[str, str] = {}
        for ent in extraction.get("entities", []):
            if ent["name"].casefold() not in referenced_names:
                continue
            ent_map[ent["name"].lower()] = _resolve_or_create(
                conn, scope, ent["name"], ent.get("type"), ent.get("description", ent["name"]),
                thresholds, model, context_text=text_body, identity_context=ev.context or {})

        fact_ids: List[str] = []
        for f in accepted:
            subj = ent_map.get(f["subject"].lower())
            if not subj:
                continue
            pred = f["_predicate"]
            obj_type = f.get("object_type", "entity")
            fact_conf = float(f.get("confidence", 0.8)) if f.get("confidence") else 0.8
            fact_vf = f.get("valid_from") or observed_at
            fact_vt = f.get("valid_to")  # 可选: fact 何时停止为真
            evidence_span = f.get("evidence_span")  # 可选: 源文本引用
            polarity, status = _assertion_semantics(pred, f, trusted=False, source_text=text_body)
            advances_state = polarity == "positive" and status in {"observed", "confirmed"}
            if obj_type == "entity":
                obj_eid = ent_map.get(f["object"].lower())
                if not obj_eid:
                    continue
                inferred_vt = (_close_superseded(conn, scope, subj, pred, fact_vf, f["object"])
                               if advances_state else None)
                fid = _insert_fact(conn, scope=scope, subject_id=subj, predicate=pred,
                                   object_type="entity", object_entity_id=obj_eid, object_value=None,
                                   valid_from=fact_vf, valid_to=_effective_valid_to(fact_vf, fact_vt, inferred_vt),
                                   confidence=fact_conf, supports=[event_id], model=model,
                                   polarity=polarity, assertion_status=status,
                                   evidence_span=evidence_span)
            else:
                val = coerce_value(conn, scope, _guess_vocab(pred), f["object"])
                obj_value = {"datatype": "string", "value": val}
                inferred_vt = (_close_superseded(conn, scope, subj, pred, fact_vf, val)
                               if advances_state else None)
                fid = _insert_fact(conn, scope=scope, subject_id=subj, predicate=pred,
                                   object_type="literal", object_entity_id=None, object_value=obj_value,
                                   valid_from=fact_vf, valid_to=_effective_valid_to(fact_vf, fact_vt, inferred_vt),
                                   confidence=fact_conf, supports=[event_id], model=model,
                                   polarity=polarity, assertion_status=status,
                                   evidence_span=evidence_span)
            fact_ids.append(fid)

        if fact_ids:
            _detect_conflicts(conn, scope)  # 检测冲突并标记
            _aggregate_belief(conn, scope, ent_map, fact_ids, observed_at, model)

        diagnostics = conn.execute(text("SELECT extraction_diagnostics FROM events WHERE event_id=CAST(:e AS uuid)"),
                                   {"e": event_id}).scalar() or []
        rejected = len(diagnostics)
        emit_lifecycle(conn, kind="extracted", scope=scope, event_id=event_id,
                       job_id=None, payload={"facts_extracted": len(fact_ids),
                                             "accepted_facts": len(fact_ids),
                                             "rejected_facts": rejected,
                                             "entities": len(ent_map), "model": model})
        conn.execute(text("UPDATE events SET embed_status='done' WHERE event_id=CAST(:e AS uuid)"),
                     {"e": event_id})
        return {"facts_extracted": len(fact_ids), "accepted_facts": len(fact_ids),
                "rejected": rejected, "rejected_facts": rejected, "diagnostics": diagnostics,
                "entities": len(ent_map), "model": model, "fact_ids": fact_ids}


def _quarantine(conn, event_id: str, raw_predicate: str, raw_object: Any,
                reason: str, evidence_span: Optional[str], model: str) -> None:
    diagnostic = {"event_id": event_id, "raw_predicate": raw_predicate,
                  "raw_object": raw_object, "reason": reason,
                  "evidence_span": evidence_span, "model": model, "schema_version": 1}
    conn.execute(text("""UPDATE events SET extraction_diagnostics = extraction_diagnostics || CAST(:d AS jsonb)
                         WHERE event_id=CAST(:e AS uuid)"""),
                 {"e": event_id, "d": json.dumps([diagnostic], ensure_ascii=False)})


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
    """全 scope belief 聚合：按当前断言及指向目标的 contradicts 重算。"""
    n = 0
    with session_scope() as conn:
        rows = conn.execute(text("""
            SELECT about_id, min(valid_from)::text AS vf FROM (
              SELECT subject_id AS about_id, valid_from FROM facts
               WHERE scope=:s AND predicate NOT IN ('contradicts','no_correlation')
                 AND valid_from <= now() AND (valid_to IS NULL OR now() < valid_to) AND recorded_to IS NULL
              UNION ALL
              SELECT object_entity_id AS about_id, valid_from FROM facts
               WHERE scope=:s AND predicate='contradicts' AND object_entity_id IS NOT NULL
                 AND valid_from <= now() AND (valid_to IS NULL OR now() < valid_to) AND recorded_to IS NULL
            ) current_assertions
            GROUP BY about_id
        """), {"s": scope}).fetchall()
        for r in rows:
            subj_id, vf = str(r[0]), r[1]
            fids = _current_belief_fact_ids(conn, scope, subj_id)
            _revise_belief(conn, scope, subj_id, fids, vf)
            n += 1
    return n


def _aggregate_belief(conn, scope: str, ent_map: Dict[str, str], fact_ids: List[str],
                      valid_from: str, model: str) -> None:
    impacted: set[str] = set()
    for fid in fact_ids:
        row = conn.execute(text("SELECT subject_id, predicate, object_entity_id FROM facts WHERE fact_id=CAST(:f AS uuid)"),
                           {"f": fid}).fetchone()
        if row:
            if row.predicate not in RELATIONAL_EXCLUSION_PREDICATES:
                impacted.add(str(row.subject_id))
            if row.predicate == "contradicts" and row.object_entity_id:
                impacted.add(str(row.object_entity_id))
    for subj_id in impacted:
        current = _current_belief_fact_ids(conn, scope, subj_id)
        _revise_belief(conn, scope, subj_id, current, valid_from)


def _current_belief_fact_ids(conn, scope: str, about_id: str) -> List[str]:
    return [r[0] for r in conn.execute(text("""
        SELECT fact_id::text FROM facts
         WHERE scope=:s AND valid_from <= now() AND (valid_to IS NULL OR now() < valid_to)
           AND recorded_to IS NULL
           AND ((subject_id=CAST(:a AS uuid) AND predicate NOT IN ('contradicts','no_correlation'))
                OR (object_entity_id=CAST(:a AS uuid) AND predicate='contradicts'))
         ORDER BY valid_from, fact_id
    """), {"s": scope, "a": about_id}).fetchall()]


def _revise_belief(conn, scope: str, subj_id: str, fact_ids: List[str], valid_from: str) -> None:
    if not fact_ids:
        return
    rows = conn.execute(text("""
        SELECT fact_id::text, confidence, polarity, assertion_status, supports::text[], valid_from::text,
               predicate, object_entity_id::text
        FROM facts WHERE fact_id=ANY(CAST(:ids AS uuid[])) ORDER BY fact_id
    """), {"ids": "{" + ",".join(fact_ids) + "}"}).fetchall()
    target_opposing = [r for r in rows if r.predicate == "contradicts" and r.object_entity_id == subj_id]
    hypothesized = [r for r in rows if r.assertion_status == "hypothesized" and r not in target_opposing]
    opposing = [r for r in rows if r.assertion_status != "hypothesized"
                and (r in target_opposing or r.polarity == "negative"
                     or r.assertion_status in ("ruled_out", "rejected"))]
    supporting = [r for r in rows if r not in hypothesized and r not in opposing
                  and r.polarity == "positive" and r.assertion_status in ("observed", "confirmed")]
    if hypothesized or (supporting and opposing):
        stance = "uncertain"
    elif opposing:
        stance = "likely_false"
    else:
        stance = "likely_true"
    pure = supporting if stance == "likely_true" else opposing if stance == "likely_false" else []
    if pure:
        sources = {sid for r in pure for sid in (r.supports or [])}
        confidence = min(.95, sum(float(r.confidence) for r in pure) / len(pure)
                         + min(.15, .05 * max(0, len(sources) - 1)))
    else:
        sw = sum(float(r.confidence) for r in supporting)
        ow = sum(float(r.confidence) for r in opposing)
        hw = .5 * sum(float(r.confidence) for r in hypothesized)
        confidence = max(.1, abs(sw - ow) / max(.0001, sw + ow + hw))
    name = conn.execute(text("SELECT canonical_name FROM entities WHERE entity_id=CAST(:e AS uuid)"),
                        {"e": subj_id}).scalar() or subj_id
    claim = (f"{name} has {len(supporting)} supporting, {len(opposing)} opposing, and "
             f"{len(hypothesized)} hypothesized current assertions")
    conn.execute(text("""UPDATE beliefs SET recorded_to=now(), last_revised_at=now()
                         WHERE scope=:s AND about_entity_id=CAST(:a AS uuid) AND recorded_to IS NULL"""),
                 {"s": scope, "a": subj_id})
    belief_valid_from = min((_parse_timestamp(r.valid_from) for r in rows)).isoformat()
    conn.execute(text("""
        INSERT INTO beliefs(scope,about_entity_id,stance,claim,confidence,supports,valid_from)
        VALUES(:s,CAST(:a AS uuid),:st,:cl,:co,CAST(:ids AS uuid[]),CAST(:vf AS timestamptz))
    """), {"s": scope, "a": subj_id, "st": stance, "cl": claim, "co": round(confidence, 3),
             "ids": "{" + ",".join(fact_ids) + "}", "vf": belief_valid_from})


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
                "negation": {"type": "boolean", "description": "true if the text says this fact is NOT true (negated assertion)"},
                "polarity": {"type": "string", "enum": ["positive", "negative"]},
                "assertion_status": {"type": "string", "enum": ["observed", "hypothesized", "confirmed", "ruled_out", "rejected"],
                                     "description": "epistemic status; causal language without explicit confirmation is hypothesized"}
            },
            "required": ["subject", "predicate", "object"]}}},
    "required": ["entities", "facts"],
}
_SYS = ("Extract knowledge-graph triples from the text. Output JSON {entities:[{name,type,description}], "
        "facts:[{subject,predicate,object,object_type:'entity'|'literal',polarity,assertion_status,evidence_span}]}. "
        "subject/object names must match entity names verbatim. Be concise.")


# ── triple 直写(前置 agent 产出的结构化三元组,零损失)──────────────────────
def _direct_write_triple(conn, scope: str, triple: Dict[str, Any], observed_at,
                         event_id: str, thresholds, event_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """content.kind=triple → 直接建 entity + fact,不经 LLM。
    triple = {subject:{name}, predicate, object:{name}, valid_from?, confidence?}"""
    if not triple or not triple.get("subject") or not triple.get("predicate"):
        return {"facts_extracted": 0, "entities": 0, "error": "incomplete triple"}
    sub_name = triple["subject"].get("name") or triple["subject"].get("id")
    obj_name = (triple.get("object") or {}).get("name") or (triple.get("object") or {}).get("id")
    pred = triple["predicate"]
    if not sub_name or not obj_name:
        return {"facts_extracted": 0, "entities": 0, "error": "missing subject/object name"}
    coerced_pred = coerce_value(conn, scope, "predicate", pred)
    if coerced_pred is None:
        _quarantine(conn, event_id, pred, obj_name, "unknown_closed_predicate",
                    triple.get("evidence_span"), "triple-direct")
        diagnostics = conn.execute(text("SELECT extraction_diagnostics FROM events WHERE event_id=CAST(:e AS uuid)"),
                                   {"e": event_id}).scalar() or []
        return {"facts_extracted": 0, "accepted_facts": 0, "entities": 0,
                "rejected": 1, "rejected_facts": 1, "diagnostics": diagnostics, "fact_ids": []}
    pred = coerced_pred
    # 实体链接(复用 B over C)
    sid = _resolve_or_create(conn, scope, sub_name, triple["subject"].get("type"),
                             sub_name, thresholds, "triple-direct", identity_context=event_context)
    oid = _resolve_or_create(conn, scope, obj_name, (triple.get("object") or {}).get("type"),
                             obj_name, thresholds, "triple-direct", identity_context=event_context)
    vf = triple.get("valid_from") or (observed_at.isoformat() if hasattr(observed_at, "isoformat") else str(observed_at))
    polarity, status = _assertion_semantics(pred, triple, trusted=True)
    inferred_vt = (_close_superseded(conn, scope, sid, pred, vf)
                   if polarity == "positive" and status in {"observed", "confirmed"} else None)
    fid = _insert_fact(conn, scope=scope, subject_id=sid, predicate=pred,
                       object_type="entity", object_entity_id=oid, object_value=None,
                       valid_from=vf, valid_to=_effective_valid_to(vf, triple.get("valid_to"), inferred_vt),
                       confidence=triple.get("confidence", 0.9), polarity=polarity,
                       assertion_status=status, evidence_span=triple.get("evidence_span"),
                       supports=[event_id], model="triple-direct")
    _aggregate_belief(conn, scope, {sub_name.casefold(): sid}, [fid], vf, "triple-direct")
    return {"facts_extracted": 1, "accepted_facts": 1, "entities": 2,
            "rejected": 0, "rejected_facts": 0, "diagnostics": [], "fact_ids": [fid]}


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
    sys_msg += ("\n\nMachine-enforced predicate vocabulary (use exactly one): "
                + ", ".join(sorted(DIAGNOSIS_PREDICATE_NAMES)))

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
            status_code = getattr(e, "status_code", None)
            response = getattr(e, "response", None)
            status_code = status_code or getattr(response, "status_code", None)
            detail = str(e).casefold()
            static_400 = status_code == 400 and any(marker in detail for marker in (
                "invalid model", "unknown model", "model_not_found", "invalid api key", "invalid_api_key"
            ))
            if status_code in {401, 403, 404} or static_400:
                raise ExtractionConfigurationError(
                    f"invalid extraction LLM configuration: HTTP {status_code}"
                ) from e
            last_err = f"mode {mode}: {type(e).__name__} {str(e)[:120]}"
            continue
    raise RuntimeError(f"all extraction modes failed: {last_err}")
