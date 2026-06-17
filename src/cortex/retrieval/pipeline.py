"""4 通道混合检索 + RRF + rerank + StratifiedPack 装配。

通道:向量(实体近邻→其 facts)、BM25(facts/events tsvector)、图(种子实体 BFS)。
融合:RRF(k=60)。融合后 top-N 走 prism rerank。组装 StratifiedPack。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from .. import services
from ..config import load_config
from ..db import session_scope


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
    obj = row.object_value.get("value") if row.object_value else (row.object_name or "")
    return f"{subj} {row.predicate} {obj}"


# ── 通道 ────────────────────────────────────────────────────────────────────
def _chan_vector(conn, scope: str, view: str, q_emb: List[float], top_k: int) -> List[str]:
    """向量:query embedding → 最近实体 → 其 live facts。"""
    frag, p = _scope_filter(scope, view)
    p["q"] = str(q_emb); p["k"] = top_k
    sql = f"""
        WITH near AS (
          SELECT entity_id FROM entities
          WHERE merged_into IS NULL AND embedding IS NOT NULL AND {frag}
          ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k
        )
        SELECT DISTINCT f.fact_id::text FROM facts f
        WHERE f.{frag} AND f.valid_to IS NULL AND f.recorded_to IS NULL
          AND (f.subject_id IN (SELECT entity_id FROM near)
               OR f.object_entity_id IN (SELECT entity_id FROM near))
        LIMIT :k
    """
    return [r[0] for r in conn.execute(text(sql), p).fetchall()]


def _chan_bm25(conn, scope: str, view: str, query: str, top_k: int) -> List[str]:
    frag, p = _scope_filter(scope, view)
    p["q"] = query; p["k"] = top_k
    sql = f"""
        SELECT fact_id::text FROM facts
        WHERE {frag} AND valid_to IS NULL AND recorded_to IS NULL
          AND to_tsvector('english', coalesce(predicate,'')||' '||coalesce(object_value->>'value','')||' '||coalesce((SELECT canonical_name FROM entities WHERE entity_id=facts.subject_id),'')) @@ plainto_tsquery(:q)
        ORDER BY ts_rank(to_tsvector('english',coalesce(predicate,'')||' '||coalesce(object_value->>'value','')), plainto_tsquery(:q)) DESC
        LIMIT :k
    """
    return [r[0] for r in conn.execute(text(sql), p).fetchall()]


def _chan_graph(conn, scope: str, view: str, q_emb: List[float], max_hops: int, top_k: int) -> List[str]:
    frag, p = _scope_filter(scope, view)
    p["q"] = str(q_emb); p["k"] = top_k
    # 种子=最近实体;返回种子及其直接邻居(1 跳)上的 facts
    sql = f"""
      WITH seeds AS (
        SELECT entity_id FROM entities WHERE merged_into IS NULL AND embedding IS NOT NULL AND {frag}
        ORDER BY embedding <=> CAST(:q AS vector) LIMIT 5
      ),
      reach AS (
        SELECT object_entity_id AS node FROM facts f, seeds s
        WHERE f.subject_id=s.entity_id AND f.{frag} AND f.valid_to IS NULL AND f.recorded_to IS NULL
          AND f.object_entity_id IS NOT NULL
      )
      SELECT DISTINCT f.fact_id::text FROM facts f
      WHERE f.{frag} AND f.valid_to IS NULL AND f.recorded_to IS NULL
        AND (f.subject_id IN (SELECT entity_id FROM seeds)
             OR f.object_entity_id IN (SELECT entity_id FROM seeds)
             OR f.subject_id IN (SELECT node FROM reach))
      LIMIT :k
    """
    return [r[0] for r in conn.execute(text(sql), p).fetchall()]


def _rrf(rank_lists: List[List[str]], k: float = 60.0) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    for lst in rank_lists:
        for rank, fid in enumerate(lst, 1):
            scores[fid] = scores.get(fid, 0.0) + 1.0 / (k + rank)
    return scores


# ── 主入口 ──────────────────────────────────────────────────────────────────
def recall(*, scope: str, query: Optional[str] = None, view: str = "local",
           top_k: Optional[int] = None, as_of: Optional[str] = None,
           valid_during: Optional[Tuple[str, str]] = None) -> Dict[str, Any]:
    cfg = load_config()
    top_k = top_k or cfg.retrieval.top_k
    t_start = time.time()
    t = {"plan": 0.0, "fetch": 0.0, "fuse": 0.0, "rerank": 0.0, "pack": 0.0}
    ch_counts: Dict[str, int] = {}

    with session_scope() as conn:
        # 无 query:返回 bounded slice(最近 facts/events)
        if not query:
            return _empty_pack(scope, view)

        t0 = time.time()
        q_emb = services.embed_one(query)
        t["plan"] = (time.time() - t0) * 1000

        t0 = time.time()
        c_vec = _chan_vector(conn, scope, view, q_emb, top_k)
        c_bm25 = _chan_bm25(conn, scope, view, query, top_k)
        c_graph = _chan_graph(conn, scope, view, q_emb, cfg.retrieval.graph_max_hops, top_k)
        t["fetch"] = (time.time() - t0) * 1000
        ch_counts = {"vector": len(c_vec), "bm25": len(c_bm25), "graph": len(c_graph)}

        t0 = time.time()
        scores = _rrf([c_vec, c_bm25, c_graph], cfg.retrieval.rrf_k)
        ranked = sorted(scores, key=lambda fid: scores[fid], reverse=True)[: top_k]
        t["fuse"] = (time.time() - t0) * 1000

        if not ranked:
            return _pack(scope, view, query, [], [], [], t, ch_counts, "", model="none")

        # 载入候选 facts
        rows = conn.execute(text("""
            SELECT f.fact_id::text, f.scope, f.predicate, f.object_type, f.object_value,
                   f.object_entity_id::text, f.subject_id::text, f.confidence,
                   f.valid_from::text, f.valid_to::text,
                   s.canonical_name AS subject_name, o.canonical_name AS object_name
            FROM facts f LEFT JOIN entities s ON s.entity_id=f.subject_id
                         LEFT JOIN entities o ON o.entity_id=f.object_entity_id
            WHERE f.fact_id = ANY(CAST(:ids AS uuid[]))
        """), {"ids": "{" + ",".join(ranked) + "}"}).fetchall()
        rowmap = {r.fact_id: r for r in rows}
        ordered_rows = [rowmap[fid] for fid in ranked if fid in rowmap]

        # temporal.natural → valid_during 过滤(fact 与窗重叠)
        if valid_during:
            vf, vt = valid_during
            ordered_rows = [r for r in ordered_rows
                            if (r.valid_from or "") <= vt and (r.valid_to is None or (r.valid_to or "") >= vf)]
            scores = {fid: scores[fid] for fid in (rowmap and [r.fact_id for r in ordered_rows])}

        # rerank(真实 prism)
        t0 = time.time()
        docs = [_fact_text(r) for r in ordered_rows]
        try:
            rered = services.rerank(query, docs)
            keep_idx = [item["index"] for item in rered
                        if item.get("relevance_score", 0) >= cfg.rerank.threshold]
            if not keep_idx:
                keep_idx = [item["index"] for item in rered[:10]]
            reranked_rows = [ordered_rows[i] for i in keep_idx]
            reranked_scores = {ordered_rows[i].fact_id: rered_map_score(rered, i)
                               for i in keep_idx}
        except Exception:  # noqa: BLE001  rerank 不可用时退回 RRF 顺序
            reranked_rows = ordered_rows[:10]
            reranked_scores = {r.fact_id: scores.get(r.fact_id, 0) for r in reranked_rows}
        t["rerank"] = (time.time() - t0) * 1000

        t0 = time.time()
        pack = _assemble_pack(conn, scope, view, query, reranked_rows, t, ch_counts)
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
            "valid_from": r.valid_from, "valid_to": r.valid_to, "supports": []}


def _assemble_pack(conn, scope, view, query, fact_rows, t, ch_counts) -> Dict[str, Any]:
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
    # context_block:有 key 走 synthesis LLM,否则规则拼
    cb = _context_block(query, facts_out, beliefs)

    pack_id = "pack_" + uuid.uuid4().hex[:24]
    pack = {
        "pack_id": pack_id, "scope": scope, "view": view,
        "layers": {"events": events, "facts": facts_out, "beliefs": beliefs},
        "context_block": cb,
        "provenance": {"trail": [{"step": "fetch", "kept": ch_counts},
                                 {"step": "fuse_rrf", "kept": len(facts_out)},
                                 {"step": "rerank", "kept": len(facts_out)}],
                       "citations": {f"[{i+1}]": {"layer": "fact", "id": f["fact_id"]}
                                     for i, f in enumerate(facts_out)}},
        "diagnostics": {"time_ms": t, "channels": ch_counts},
    }
    _cache_pack(conn, pack)
    return pack


def _context_block(query, facts, beliefs) -> str:
    if not facts:
        return "(无相关记忆)"
    if services.llm_configured("synthesis"):
        try:
            payload = json.dumps({"facts": facts[:8], "beliefs": beliefs[:3]})
            raw = services.llm_chat("synthesis",
                                     "用引用标记[n]把给定事实串成一段中文综述,只输出综述本身。",
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
