"""4 通道混合检索 + RRF + rerank + StratifiedPack 装配。

通道:向量(实体近邻→其 facts)、BM25(facts/events tsvector)、图(种子实体 BFS)。
融合:RRF(k=60)。融合后 top-N 走 prism rerank。组装 StratifiedPack。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from .. import services
from ..config import load_config
from ..db import session_scope
from ..ontology import CAUSAL_PREDICATES, GRAPH_EXCLUDED_PREDICATES
from ..prompts import SYNTHESIS_CONTEXT_BLOCK, HYDE_SYSTEM, MULTIHOP_SYSTEM


def _scope_filter(scope: str, view: str) -> Tuple[str, Dict[str, Any]]:
    """返回 (SQL fragment, params)。"""
    if view == "holistic":
        prefixes = ["/".join(scope.split("/")[:i]) for i in range(1, len(scope.split("/")) + 1)]
        return "scope = ANY(:scopes)", {"scopes": prefixes}
    if view == "descend":
        return "(scope = :scope0 OR scope LIKE :scopep)", {"scope0": scope, "scopep": scope + "/%"}
    return "scope = :scope0", {"scope0": scope}


def _fact_text(row) -> str:
    subj = row.subject_name or "?"
    obj = (row.object_name or "") if row.object_type == "entity" else (row.object_value or {}).get("value", "")
    return f"{subj} {row.predicate} {obj}"


def _graph_eligible_sql(alias: str = "f") -> str:
    causal = ",".join(f"'{predicate}'" for predicate in sorted(CAUSAL_PREDICATES))
    excluded = ",".join(f"'{predicate}'" for predicate in sorted(GRAPH_EXCLUDED_PREDICATES))
    return (f"{alias}.polarity='positive' AND {alias}.predicate NOT IN ({excluded}) AND (({alias}.predicate IN ({causal}) "
            f"AND {alias}.assertion_status='confirmed') OR ({alias}.predicate NOT IN ({causal}) "
            f"AND {alias}.assertion_status IN ('observed','confirmed')))" )


# ── 时态过滤辅助(通道统一用,支持 as_of / include_superseded)─────────────────
def _temporal_clause(as_of: Optional[str], include_superseded: bool) -> str:
    """返回通道 SQL 的时间过滤片段。
    默认(无 as_of): valid_to IS NULL AND recorded_to IS NULL(当前 live facts)
    as_of(不含 include_superseded): valid_from<=t<valid_to AND recorded_to IS NULL(当时为真+当前认知)
    as_of + include_superseded: valid_from<=t<valid_to AND recorded_from<=t(含历史认知)"""
    if as_of:
        base = "valid_from <= CAST(:ao AS timestamptz) AND (valid_to IS NULL OR CAST(:ao AS timestamptz) < valid_to)"
        if include_superseded:
            return (base + " AND recorded_from <= CAST(:ao AS timestamptz) "
                    "AND (recorded_to IS NULL OR CAST(:ao AS timestamptz) < recorded_to)")
        return base + " AND recorded_to IS NULL"
    if not include_superseded:
        return "valid_to IS NULL AND recorded_to IS NULL"
    return "valid_to IS NULL"  # include_superseded 但无 as_of: 返回所有 valid 的(含被软关的)


def _temporal_params(as_of: Optional[str]) -> Dict[str, Any]:
    """返回通道 SQL 需要的时态参数。"""
    return {"ao": as_of} if as_of else {}


# ── 通道 ────────────────────────────────────────────────────────────────────
def _chan_vector(conn, scope: str, view: str, q_emb: List[float], top_k: int,
                 as_of: str = None, include_superseded: bool = False) -> List[str]:
    """向量:query embedding → 最近实体 → 其 facts(时态过滤)。"""
    frag, p = _scope_filter(scope, view)
    p["q"] = str(q_emb); p["k"] = top_k
    p.update(_temporal_params(as_of))
    tc = _temporal_clause(as_of, include_superseded)
    sql = f"""
        WITH near AS (
          SELECT entity_id FROM entities
          WHERE merged_into IS NULL AND embedding IS NOT NULL AND {frag}
          ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k
        )
        SELECT DISTINCT f.fact_id::text FROM facts f
        WHERE f.{frag} AND f.{tc}
          AND (f.subject_id IN (SELECT entity_id FROM near)
               OR f.object_entity_id IN (SELECT entity_id FROM near))
        LIMIT :k
    """
    return [r[0] for r in conn.execute(text(sql), p).fetchall()]


def _chan_bm25(conn, scope: str, view: str, query: str, top_k: int,
               as_of: str = None, include_superseded: bool = False) -> List[str]:
    frag, p = _scope_filter(scope, view)
    p["q"] = query; p["k"] = top_k
    p.update(_temporal_params(as_of))
    tc = _temporal_clause(as_of, include_superseded)
    sql = f"""
        SELECT fact_id::text FROM facts
        WHERE {frag} AND {tc}
          AND (to_tsvector('simple', coalesce(predicate,'')||' '||coalesce(object_value->>'value','')||' '||coalesce((SELECT canonical_name FROM entities WHERE entity_id=facts.subject_id),'')) @@ plainto_tsquery(:q)
               OR coalesce(object_value->>'value','') ILIKE :likeq
               OR coalesce((SELECT canonical_name FROM entities WHERE entity_id=facts.subject_id),'') ILIKE :likeq
               OR coalesce((SELECT canonical_name FROM entities WHERE entity_id=facts.object_entity_id),'') ILIKE :likeq)
        ORDER BY ts_rank(to_tsvector('simple',coalesce(predicate,'')||' '||coalesce(object_value->>'value','')), plainto_tsquery(:q)) DESC
        LIMIT :k
    """
    p["likeq"] = f"%{query.strip()}%"
    return [r[0] for r in conn.execute(text(sql), p).fetchall()]


def _chan_graph(conn, scope: str, view: str, q_emb: List[float], max_hops: int, top_k: int,
                as_of: str = None, include_superseded: bool = False) -> List[str]:
    """图遍历:种子实体出发,递归 CTE BFS max_hops 跳,返回路径上的 facts(时态过滤)。"""
    frag, p = _scope_filter(scope, view)
    p["q"] = str(q_emb); p["k"] = top_k; p["h"] = max_hops
    p.update(_temporal_params(as_of))
    tc = _temporal_clause(as_of, include_superseded)
    sql = f"""
      WITH RECURSIVE seeds AS (
        SELECT entity_id FROM entities WHERE merged_into IS NULL AND embedding IS NOT NULL AND {frag}
        ORDER BY embedding <=> CAST(:q AS vector) LIMIT 5
      ),
      graph_walk AS (
        SELECT f.object_entity_id AS node, f.fact_id, 1 AS hop,
               ARRAY[s.entity_id, f.object_entity_id]::uuid[] AS visited
          FROM facts f, seeds s
         WHERE f.subject_id = s.entity_id AND f.{frag}
           AND f.{tc}
           AND {_graph_eligible_sql('f')}
           AND f.object_entity_id IS NOT NULL
        UNION ALL
        SELECT f.object_entity_id, f.fact_id, gw.hop + 1, gw.visited || f.object_entity_id
          FROM facts f JOIN graph_walk gw ON f.subject_id = gw.node
         WHERE f.{frag} AND f.{tc}
           AND {_graph_eligible_sql('f')}
           AND f.object_entity_id IS NOT NULL
           AND gw.hop < :h
           AND NOT f.object_entity_id = ANY(gw.visited)
      )
      SELECT DISTINCT fact_id::text FROM graph_walk WHERE hop <= :h LIMIT :k
    """
    return [r[0] for r in conn.execute(text(sql), p).fetchall()]


def _expand_synonyms(conn, scope: str, query: str,
                   as_of: str = None, include_superseded: bool = False) -> List[str]:
    """synonym 通道。"""
    _tc = _temporal_clause(as_of, include_superseded)
    _tp = _temporal_params(as_of)
    words = re.findall(r"\w+", query.lower())
    terms = set(words)
    for w in words:
        rows = conn.execute(text("""
            SELECT term, aliases FROM synonyms WHERE scope=:s AND (term=:w OR :w = ANY(aliases))
        """), {"s": scope, "w": w}).fetchall()
        for r in rows:
            terms.add(r[0]); terms.update(r[1] or [])
    if terms == set(words):
        return []
    expanded = " ".join(sorted(terms))
    rows = conn.execute(text("""
        SELECT fact_id::text FROM facts
        WHERE scope=:s AND """ + _tc + """
          AND to_tsvector('simple', coalesce(predicate,'')||' '||coalesce(object_value->>'value',''))
              @@ plainto_tsquery(:q) LIMIT :k
    """), {**{"s": scope, "q": expanded, "k": 40}, **_tp}).fetchall()
    return [r[0] for r in rows]


def _chan_entity_name(conn, scope: str, view: str, query: str, top_k: int,
                     as_of: str = None, include_superseded: bool = False) -> List[str]:
    """entity-name 通道。"""
    frag, p = _scope_filter(scope, view)
    p["k"] = top_k
    p.update(_temporal_params(as_of))
    tc = _temporal_clause(as_of, include_superseded)
    names = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b", query)
    if not names:
        names = [w for w in re.findall(r"\w+", query) if len(w) > 3][:5]
    if not names:
        return []
    eids: list = []
    for nm in names:
        rows = conn.execute(text(f"""
            SELECT entity_id::text FROM entities
            WHERE {frag} AND merged_into IS NULL
              AND (canonical_name ILIKE :nm
                   OR EXISTS (SELECT 1 FROM entity_aliases a WHERE a.entity_id=entities.entity_id AND a.alias ILIKE :nm)
                   OR similarity(canonical_name, :nm) > 0.3)
            LIMIT 5
        """), {**p, "nm": f"%{nm}%"}).fetchall()
        eids.extend(r[0] for r in rows)
    eids = list(dict.fromkeys(eids))
    if not eids:
        return []
    eid_arr = "{" + ",".join(eids) + "}"
    rows = conn.execute(text(f"""
        SELECT DISTINCT fact_id::text FROM facts
        WHERE {frag} AND {tc}
          AND (subject_id = ANY(CAST(:eids AS uuid[])) OR object_entity_id = ANY(CAST(:eids AS uuid[])))
        LIMIT :k
    """), {**p, "eids": eid_arr}).fetchall()
    return [r[0] for r in rows]


def _chan_temporal_decay(conn, scope: str, view: str, top_k: int, decay_days: int = 30,
                       as_of: str = None, include_superseded: bool = False) -> List[str]:
    """temporal-decay 通道:近因窗内 facts,按时间衰减(越新越靠前)。"""
    frag, p = _scope_filter(scope, view)
    p["k"] = top_k; p["d"] = decay_days
    p.update(_temporal_params(as_of))
    tc = _temporal_clause(as_of, include_superseded)
    anchor = "CAST(:ao AS timestamptz)" if as_of else "now()"
    sql = f"""
        SELECT fact_id::text FROM facts
        WHERE {frag} AND {tc}
          AND valid_from >= {anchor} - make_interval(secs => :d * 86400)
        ORDER BY valid_from DESC LIMIT :k
    """
    return [r[0] for r in conn.execute(text(sql), p).fetchall()]


def _rrf(rank_lists: List[List[str]], k: float = 60.0) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    for lst in rank_lists:
        for rank, fid in enumerate(lst, 1):
            scores[fid] = scores.get(fid, 0.0) + 1.0 / (k + rank)
    return scores


# ── 主入口 ──────────────────────────────────────────────────────────────────
def _question_type(query: str) -> str:
    """规则版路由:有多 session 信号(时间词/多实体/who what when 交叉)→ multi,否则 single。"""
    multi_signals = sum(1 for w in ("last", "previous", "earlier", "before", "yesterday", "history") if w in query.lower())
    if multi_signals >= 1 or query.lower().count(" ") >= 8:
        return "multi-session"
    return "single-session"


def recall(*, scope: str, query: Optional[str] = None, view: str = "local",
           top_k: Optional[int] = None, as_of: Optional[str] = None,
           valid_during: Optional[Tuple[str, str]] = None,
           recorded_during: Optional[Tuple[str, str]] = None,
           include_superseded: bool = False,
           budgets: Optional[Dict[str, Any]] = None,
           citation_mode: str = "inline_with_markers",
           exclude_content: bool = False) -> Dict[str, Any]:
    cfg = load_config()
    adv = cfg.retrieval.advanced
    # question-type routing → top_k
    if adv.question_routing and query:
        qtype = _question_type(query)
        if top_k is None:
            top_k = 160 if qtype == "multi-session" else 40
    top_k = top_k or cfg.retrieval.top_k
    t_start = time.time()
    t = {"plan": 0.0, "fetch": 0.0, "fuse": 0.0, "rerank": 0.0, "pack": 0.0}
    ch_counts: Dict[str, int] = {}

    if not query:
        return _empty_pack(scope, view)

    # ── Phase 0: query embedding + HyDE(无 DB session,纯 HTTP)──
    t0 = time.time()
    q_emb = services.embed_one(query)
    extra_embs: List[List[float]] = []
    if adv.hyde_enabled and services.llm_configured("synthesis"):
        try:
            for _ in range(adv.hyde_passages):
                raw = services.llm_chat("synthesis",
                    "写一段假设性回答(假设记忆里有答案),纯文本无前缀。", query)
                extra_embs.append(services.embed_one(services.strip_think(raw)))
        except Exception:  # noqa: BLE001
            pass
    t["plan"] = (time.time() - t0) * 1000

    # ── Phase 1: 6 通道 + RRF + 载入候选 facts(DB session,快)──
    with session_scope() as conn:
        t0 = time.time()
        if adv.entity_vector_seed:
            for nm in re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b", query)[:5]:
                row = conn.execute(text(
                    "SELECT embedding FROM entities WHERE scope=:s AND merged_into IS NULL "
                    "AND lower(canonical_name)=lower(:n) AND embedding IS NOT NULL LIMIT 1"),
                    {"s": scope, "n": nm}).fetchone()
                if row and row[0]:
                    extra_embs.append(list(row[0]))

        c_vec = _chan_vector(conn, scope, view, q_emb, top_k, as_of, include_superseded)
        for e in extra_embs:
            c_vec = list(dict.fromkeys(c_vec + _chan_vector(conn, scope, view, e, top_k, as_of, include_superseded)))
        c_bm25 = _chan_bm25(conn, scope, view, query, top_k, as_of, include_superseded)
        c_graph = _chan_graph(conn, scope, view, q_emb, cfg.retrieval.graph_max_hops, top_k, as_of, include_superseded)
        c_ent = _chan_entity_name(conn, scope, view, query, top_k, as_of, include_superseded)
        c_syn = _expand_synonyms(conn, scope, query, as_of, include_superseded)
        c_tmp = _chan_temporal_decay(conn, scope, view, top_k, as_of=as_of, include_superseded=include_superseded)
        if adv.multihop_enabled and services.llm_configured("synthesis"):
            try:
                import json as _j
                raw = services.llm_chat("synthesis", MULTIHOP_SYSTEM,
                    _j.dumps({"query": query, "n": adv.multihop_count}))
                subs = services.parse_llm_json(raw)
                for sq in (subs.get("queries") or [])[:adv.multihop_count]:
                    c_bm25 = list(dict.fromkeys(c_bm25 + _chan_bm25(conn, scope, view, sq, top_k, as_of, include_superseded)))
            except Exception:  # noqa: BLE001
                pass
        t["fetch"] = (time.time() - t0) * 1000
        ch_counts = {"vector": len(c_vec), "bm25": len(c_bm25), "graph": len(c_graph),
                     "entity_name": len(c_ent), "synonym": len(c_syn), "temporal": len(c_tmp)}

        t0 = time.time()
        scores = _rrf([c_vec, c_bm25, c_graph, c_ent, c_syn, c_tmp], cfg.retrieval.rrf_k)
        if adv.salience_weight > 0 and scores:
            for fid in list(scores.keys()):
                ac = conn.execute(text("""SELECT coalesce(max(e.access_count),0) FROM events e
                    WHERE e.event_id = ANY((SELECT supports FROM facts WHERE fact_id=CAST(:f AS uuid)))"""),
                    {"f": fid}).scalar() or 0
                scores[fid] += adv.salience_weight * (ac / 10.0)
        ranked = sorted(scores, key=lambda fid: scores[fid], reverse=True)[: top_k]
        t["fuse"] = (time.time() - t0) * 1000

        if not ranked:
            return _pack(scope, view, query, [], [], [], t, ch_counts, "", model="none")

        # ── 双时态过滤:在候选加载时应用 as_of / valid_during / recorded_during ──
        temporal_where = ""
        temporal_params: Dict[str, Any] = {}
        if as_of:
            # as_of: 裁剪双轴 — valid_from <= as_of < valid_to AND recorded_from <= as_of
            temporal_where += " AND f.valid_from <= CAST(:ao AS timestamptz) AND (f.valid_to IS NULL OR CAST(:ao AS timestamptz) < f.valid_to)"
            if include_superseded:
                # include_superseded: 返回历史版本(recorded_from <= as_of),不强制 recorded_to IS NULL
                temporal_where = " AND f.valid_from <= CAST(:ao AS timestamptz) AND (f.valid_to IS NULL OR CAST(:ao AS timestamptz) < f.valid_to) AND f.recorded_from <= CAST(:ao AS timestamptz) AND (f.recorded_to IS NULL OR CAST(:ao AS timestamptz) < f.recorded_to)"
            temporal_params["ao"] = as_of
        else:
            if not include_superseded:
                temporal_where += " AND f.recorded_to IS NULL"
        if valid_during:
            vf, vt = valid_during
            temporal_where += " AND f.valid_from <= CAST(:vt AS timestamptz) AND (f.valid_to IS NULL OR f.valid_to >= CAST(:vf AS timestamptz))"
            temporal_params["vf"] = vf; temporal_params["vt"] = vt
        if recorded_during:
            rf, rt = recorded_during
            temporal_where += " AND f.recorded_from <= CAST(:rt AS timestamptz) AND (f.recorded_to IS NULL OR f.recorded_to >= CAST(:rf AS timestamptz))"
            temporal_params["rf"] = rf; temporal_params["rt"] = rt

        rows = conn.execute(text(f"""
            SELECT f.fact_id::text, f.scope, f.predicate, f.object_type, f.object_value,
                   f.object_entity_id::text, f.subject_id::text, f.confidence,
                   f.valid_from::text, f.valid_to::text, f.supports::text[] AS supports,
                   f.polarity, f.assertion_status, f.evidence_span,
                   s.canonical_name AS subject_name, o.canonical_name AS object_name
            FROM facts f LEFT JOIN entities s ON s.entity_id=f.subject_id
                         LEFT JOIN entities o ON o.entity_id=f.object_entity_id
            WHERE f.fact_id = ANY(CAST(:ids AS uuid[])){temporal_where}
        """), {"ids": "{" + ",".join(ranked) + "}", **temporal_params}).fetchall()
        rowmap = {r.fact_id: r for r in rows}
        ordered_rows = [rowmap[fid] for fid in ranked if fid in rowmap]
    # session 关闭——下面 rerank 不持有 DB 连接

    # ── Phase 2: rerank(无 DB session,纯 HTTP)──
    t0 = time.time()
    docs = [_fact_text(r) for r in ordered_rows]
    try:
        rered = services.rerank(query, docs)
        keep_idx = [item["index"] for item in rered
                    if item.get("relevance_score", 0) >= cfg.rerank.threshold]
        if not keep_idx:
            keep_idx = [item["index"] for item in rered[:10]]
        reranked_rows = [ordered_rows[i] for i in keep_idx]
    except Exception:  # noqa: BLE001
        reranked_rows = ordered_rows[:10]
    t["rerank"] = (time.time() - t0) * 1000

    # ── Phase 3: pack 装配 + 缓存(DB session,快)──
    t0 = time.time()
    pack = None
    for _attempt in range(3):
        try:
            with session_scope() as conn:
                pack = _assemble_pack(conn, scope, view, query, reranked_rows, t, ch_counts,
                                      budgets=budgets, citation_mode=citation_mode,
                                      exclude_content=exclude_content, recorded_during=recorded_during,
                                      include_superseded=include_superseded)
            break
        except Exception:  # noqa: BLE001 代理抖动,重试
            if _attempt < 2:
                time.sleep(0.3)
                continue
            # 三次都失败:返回最小 pack(至少有 facts)
            pack = {"pack_id": "pack_" + uuid.uuid4().hex[:24], "scope": scope, "view": view,
                    "layers": {"events": [], "facts": [_fact_to_out(r) for r in reranked_rows[:10]],
                               "beliefs": []},
                    "context_block": "", "provenance": {"trail": [], "citations": {}},
                    "diagnostics": {"time_ms": t, "channels": ch_counts, "note": "pack assembly failed, partial"}}
    t["pack"] = (time.time() - t0) * 1000
    return pack


def rered_map_score(rered, idx):
    for it in rered:
        if it["index"] == idx:
            return it.get("relevance_score", 0)
    return 0


def _fact_to_out(r) -> Dict[str, Any]:
    obj = ({"datatype": "entity", "value": r.object_name} if r.object_type == "entity"
           else {"datatype": "literal", "value": (r.object_value or {}).get("value")})
    return {"fact_id": r.fact_id, "scope": r.scope,
            "subject": {"id": r.subject_id, "name": r.subject_name},
            "predicate": r.predicate, "object": obj, "confidence": r.confidence,
            "valid_from": r.valid_from, "valid_to": r.valid_to,
            "supports": list(getattr(r, "supports", None) or []),
            "evidence": getattr(r, "evidence_span", None),
            "polarity": getattr(r, "polarity", "positive"),
            "assertion_status": getattr(r, "assertion_status", "observed")}


def _assemble_pack(conn, scope, view, query, fact_rows, t, ch_counts,
                   budgets=None, citation_mode="inline_with_markers",
                   exclude_content=False, recorded_during=None, include_superseded=False) -> Dict[str, Any]:
    # budgets.per_layer_limits 硬上限裁剪
    per_layer = (budgets or {}).get("per_layer_limits") or {}
    if per_layer.get("facts"):
        fact_rows = fact_rows[: per_layer["facts"]]
    fact_ids = [r.fact_id for r in fact_rows]
    subj_ids = list({r.subject_id for r in fact_rows})
    # beliefs about these subjects
    beliefs: List[Dict[str, Any]] = []
    if subj_ids:
        brows = conn.execute(text("""
            SELECT b.belief_id::text, b.stance, b.claim, b.confidence, b.about_entity_id::text,
                   e.canonical_name, b.supports::text[] AS supports
            FROM beliefs b JOIN entities e ON e.entity_id=b.about_entity_id
            WHERE b.valid_to IS NULL AND b.recorded_to IS NULL AND b.about_entity_id = ANY(CAST(:a AS uuid[]))
            LIMIT 10
        """), {"a": "{" + ",".join(subj_ids) + "}"}).fetchall()
        if per_layer.get("beliefs"):
            brows = brows[: per_layer["beliefs"]]
        beliefs = [{"belief_id": b.belief_id, "about": {"id": b.about_entity_id, "name": b.canonical_name},
                    "stance": b.stance, "claim": b.claim, "confidence": b.confidence,
                    "supports": [s for s in (b.supports or [])]} for b in brows]
    # supporting events
    events: List[Dict[str, Any]] = []
    if fact_ids:
        evrows = conn.execute(text("""
            SELECT DISTINCT e.event_id::text, e.modality, e.content, e.observed_actor, e.observed_at::text
            FROM events e WHERE e.event_id = ANY(
              SELECT unnest(f.supports) FROM facts f WHERE f.fact_id = ANY(CAST(:fids AS uuid[])))
            LIMIT 5
        """), {"fids": "{" + ",".join(fact_ids) + "}"}).fetchall()
        events = [{"event_id": ev.event_id, "scope": scope, "modality": ev.modality,
                   "observed_actor": ev.observed_actor, "content": ev.content or {},
                   "observed_at": ev.observed_at, "excluded_from_recall": False} for ev in evrows]

    facts_out = [_fact_to_out(r) for r in fact_rows]
    # exclude_content: 去掉大文本字段
    if exclude_content:
        for ev in events:
            ev["content"] = {}
        for f in facts_out:
            f.pop("supports", None)
    # max_tokens knapsack: 估算 token(~4 字符/token),裁到预算(events 优先裁)
    max_tokens = (budgets or {}).get("max_tokens")
    if max_tokens:
        def _est(obj):
            return len(json.dumps(obj, ensure_ascii=False, default=str)) // 4
        while events and _est({"events": events, "facts": facts_out, "beliefs": beliefs}) > max_tokens:
            events.pop()
        while len(facts_out) > 1 and _est({"events": events, "facts": facts_out, "beliefs": beliefs}) > max_tokens:
            facts_out.pop()
    # context_block
    cb = _context_block(query, facts_out, beliefs, citation_mode, budgets=budgets)

    pack_id = "pack_" + uuid.uuid4().hex[:24]
    # citations 按 citation_mode
    if citation_mode == "none":
        citations = {}
        cb = "" if citation_mode == "none" else cb
    elif citation_mode == "structured_only":
        citations = {f"[{i+1}]": {"layer": "fact", "id": f["fact_id"]}
                     for i, f in enumerate(facts_out)}
        cb = ""
    else:  # inline_with_markers / block_at_end
        citations = {f"[{i+1}]": {"layer": "fact", "id": f["fact_id"]}
                     for i, f in enumerate(facts_out)}
    pack = {
        "pack_id": pack_id, "scope": scope, "view": view,
        "layers": {"events": events, "facts": facts_out, "beliefs": beliefs},
        "context_block": cb,
        "provenance": {"trail": [{"step": "fetch", "kept": ch_counts},
                                 {"step": "fuse_rrf", "kept": len(facts_out)},
                                 {"step": "rerank", "kept": len(facts_out)}],
                       "citations": citations},
        "diagnostics": {"time_ms": t, "channels": ch_counts},
    }
    try:
        _cache_pack(conn, pack)
    except Exception:  # noqa: BLE001 缓存失败不影响召回结果
        pass
    return pack


def _context_block(query, facts, beliefs, citation_mode="inline_with_markers", budgets=None) -> str:
    if not facts:
        return "(无相关记忆)"
    # token 预算:留 30% 给 LLM 生成的叙述,70% 填证据。无预算默认填 12 条(保守上限,非硬截)
    from ..token_budget import fit_to_budget, estimate_tokens
    max_ctx = (budgets or {}).get("max_tokens")
    ctx_budget = int(max_ctx * 0.7) if max_ctx else None
    facts_in = fit_to_budget(facts, ctx_budget) if ctx_budget else facts[:12]
    if services.llm_configured("synthesis"):
        try:
            payload = json.dumps({"facts": facts_in, "beliefs": beliefs[:5]})
            raw = services.llm_chat("synthesis", SYNTHESIS_CONTEXT_BLOCK,
                                     payload)
            return services.strip_think(raw)
        except Exception:  # noqa: BLE001
            pass
    parts = [f"[{i+1}] {f['subject']['name']} {f['predicate']} {f['object']['value']}"
             for i, f in enumerate(facts[:6])]
    return "相关记忆:" + "; ".join(parts) + "。"


def _cache_pack(conn, pack) -> None:
    qh = hashlib.sha256((pack["scope"] + json.dumps(pack["layers"], sort_keys=True)).encode()).hexdigest()[:16]
    conn.execute(text("""
        INSERT INTO recall_packs (pack_id, scope, query_hash, pack_json, expires_at)
        VALUES (:id,:s,:h,CAST(:j AS jsonb), now() + interval '60 second')
    """), {"id": pack["pack_id"], "s": pack["scope"], "h": qh, "j": json.dumps(pack)})


def _pack(scope, view, query, events, facts, beliefs, t, ch_counts, context_block, model="mock") -> Dict[str, Any]:
    pack_id = "pack_" + uuid.uuid4().hex[:24]
    return {"pack_id": pack_id, "scope": scope, "view": view,
            "layers": {"events": events, "facts": facts, "beliefs": beliefs},
            "context_block": context_block,
            "provenance": {"trail": [], "citations": {}},
            "diagnostics": {"time_ms": t, "channels": ch_counts}}


def _empty_pack(scope, view):
    return {"pack_id": "pack_" + uuid.uuid4().hex[:24], "scope": scope, "view": view,
            "layers": {"events": [], "facts": [], "beliefs": []}, "context_block": "",
            "provenance": {"trail": [], "citations": {}}, "diagnostics": {"time_ms": {}, "channels": {}}}


def get_cached_pack(pack_id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as conn:
        row = conn.execute(text("""
            SELECT pack_json FROM recall_packs WHERE pack_id=:p AND expires_at > now()
        """), {"p": pack_id}).fetchone()
        return json.loads(row.pack_json) if row else None
