"""cortex MCP server — 把 cortex 全部能力暴露为 MCP tools,供 agent 注册使用。

两种传输:
  - stdio(`cortex mcp`):本地单 agent 注册(每 agent 一个子进程)。
  - streamable-http(`cortex mcp-http`):多人共享,一个中心服务,多 agent 网络连。
    多用户按 `X-Cortex-Scope` 请求头隔离(每个 agent 配置自己的 scope 头);
    若 config.api.key 非空,需 `Authorization: Bearer <key>`。

进程内实现:直调 cortex.* 函数,memory_store 同步抽取(存完立即可搜)。
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from .config import load_config, llm_configured
from .core import append_event
from .db import session_scope, assert_services_reachable
from .extraction.pipeline import extract_event
from .retrieval import recall
from . import services, ingest, export_data, erasures, episodes, temporal
from . import maintenance as maint

try:
    from mcp.server.fastmcp import FastMCP, Context
except ImportError as e:  # pragma: no cover
    raise SystemExit(f"需要 mcp SDK: uv sync (mcp>=1.2)。{e}")

mcp = FastMCP("cortex")
DEFAULT_SCOPE = os.environ.get("CORTEX_SCOPE", "org:local/user:default")


def _eff_scope(ctx: Optional[Context], scope_arg: Optional[str]) -> str:
    """解析本次调用的 scope:显式 arg > HTTP 请求头 X-Cortex-Scope(多人隔离)> 环境默认。"""
    if scope_arg:
        return scope_arg
    try:
        req = ctx.request_context.request  # streamable-http 下是 Starlette Request
        h = req.headers.get("x-cortex-scope")
        if h:
            return h
    except Exception:  # stdio 无 HTTP request
        pass
    return DEFAULT_SCOPE


# ── core memory tools ──────────────────────────────────────────────────────
@mcp.tool()
def health_check() -> Dict[str, Any]:
    """Check DB reachability and row counts. Use first to confirm cortex is wired up."""
    s = assert_services_reachable()
    if not s["ok"]:
        return {"ok": False, "error": s["detail"], "hint": "确保 scripts/db_proxy.py 在跑"}
    with session_scope() as c:
        counts = {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                  for t in ("events", "entities", "facts", "beliefs", "episodes")}
    return {"ok": True, "llm_configured": llm_configured("extraction"), "counts": counts}


@mcp.tool()
def memory_store(text: str, scope: Optional[str] = None, modality: str = "conversation",
                 ctx: Context = None) -> Dict[str, Any]:
    """Store a free-form memory (text) AND synchronously extract triples into the knowledge graph,
    so it's immediately searchable. Returns event_id + extracted facts. Scope defaults to the
    request's X-Cortex-Scope header (multi-user isolation) or CORTEX_SCOPE env."""
    sc = _eff_scope(ctx, scope)
    eid, off = append_event(scope=sc, modality=modality,
                            content={"kind": "message", "role": "user", "text": text},
                            context={}, caller="mcp", idempotency_key=f"mcp-{uuid.uuid4().hex[:16]}")
    res = extract_event(eid)
    return {"event_id": eid, "wal_offset": off, "scope": sc,
            "facts_extracted": res.get("facts_extracted", 0), "entities": res.get("entities", 0),
            "model": res.get("model")}


@mcp.tool()
def memory_search(query: str, scope: Optional[str] = None, view: str = "local", top_k: int = 20,
                  ctx: Context = None) -> Dict[str, Any]:
    """Hybrid recall (vector + fulltext + graph + RRF + rerank). Main 'remember' lookup.
    Scope from X-Cortex-Scope header or CORTEX_SCOPE env."""
    pack = recall(scope=_eff_scope(ctx, scope), query=query, view=view, top_k=top_k)
    return {"pack_id": pack["pack_id"], "scope": _eff_scope(ctx, scope),
            "channels": pack["diagnostics"].get("channels", {}),
            "facts": pack["layers"]["facts"], "beliefs": pack["layers"]["beliefs"],
            "context_block": pack["context_block"]}


@mcp.tool()
def answer(query: str, scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Recall + LLM answer with [n] citations."""
    import json
    sc = _eff_scope(ctx, scope)
    pack = recall(scope=sc, query=query)
    if llm_configured("answer"):
        try:
            raw = services.llm_chat("answer",
                "Answer using the given memories. Keep [n] citation markers.",
                json.dumps({"query": query, "pack_layers": pack["layers"]}))
            ans = services.strip_think(raw); model = load_config().llm.answer.model
        except Exception as e:  # noqa: BLE001
            ans = services.mock_answer(query, json.dumps(pack)); model = f"mock({type(e).__name__})"
    else:
        ans = services.mock_answer(query, json.dumps(pack)); model = "mock"
    return {"answer": ans, "model_used": model, "scope": sc, "pack_id": pack["pack_id"],
            "citations": [{"marker": f"[{i+1}]", "fact_id": f["fact_id"]}
                          for i, f in enumerate(pack["layers"]["facts"][:6])]}


@mcp.tool()
def get_context(scope: Optional[str] = None, query: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Holistic recall (scope + ancestors) — 'what should I know before responding'."""
    pack = recall(scope=_eff_scope(ctx, scope), query=query, view="holistic")
    return {"context_block": pack["context_block"],
            "facts": pack["layers"]["facts"][:10], "beliefs": pack["layers"]["beliefs"][:5]}


# ── list / get ──────────────────────────────────────────────────────────────
@mcp.tool()
def memory_list(scope: Optional[str] = None, limit: int = 20, ctx: Context = None) -> Dict[str, Any]:
    """List raw events (WAL) for a scope, newest first."""
    sc = _eff_scope(ctx, scope)
    with session_scope() as c:
        rows = c.execute(text("""SELECT event_id::text, modality, content->>'text' AS text,
            observed_at::text, excluded_from_recall, methylated_at IS NOT NULL AS methylated
            FROM events WHERE scope=:s ORDER BY observed_at DESC LIMIT :lim"""), {"s": sc, "lim": limit}).fetchall()
    return {"items": [dict(event_id=r[0], modality=r[1], text=r[2], observed_at=r[3],
                           excluded=r[4], methylated=r[5]) for r in rows]}


@mcp.tool()
def memory_get(event_id: str) -> Dict[str, Any]:
    """Fetch a single event by id (full payload)."""
    with session_scope() as c:
        r = c.execute(text("""SELECT event_id::text, scope, modality, content, context, observed_at::text
            FROM events WHERE event_id=CAST(:e AS uuid)"""), {"e": event_id}).fetchone()
    if not r:
        return {"error": "not found"}
    return {"event_id": r[0], "scope": r[1], "modality": r[2], "content": r[3], "context": r[4], "observed_at": r[5]}


# ── entities / graph ────────────────────────────────────────────────────────
@mcp.tool()
def entity_list(scope: Optional[str] = None, q: Optional[str] = None, limit: int = 50,
                ctx: Context = None) -> Dict[str, Any]:
    """List entities (graph nodes). Optional substring filter q."""
    sc = _eff_scope(ctx, scope)
    sql = "SELECT entity_id::text, canonical_name, entity_type, description FROM entities WHERE scope=:s AND merged_into IS NULL"
    p: Dict[str, Any] = {"s": sc, "lim": limit}
    if q:
        sql += " AND (canonical_name ILIKE :q OR description ILIKE :q)"; p["q"] = f"%{q}%"
    sql += " ORDER BY created_at DESC LIMIT :lim"
    with session_scope() as c:
        rows = c.execute(text(sql), p).fetchall()
    return {"items": [dict(entity_id=r[0], name=r[1], type=r[2], description=r[3]) for r in rows]}


@mcp.tool()
def entity_edges(entity_id: str, scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """All live facts about one entity (graph edges), grouped by predicate."""
    sc = _eff_scope(ctx, scope)
    with session_scope() as c:
        rows = c.execute(text("""SELECT f.fact_id::text, f.predicate, f.object_type, f.object_value,
            o.canonical_name AS oname, f.confidence FROM facts f LEFT JOIN entities o ON o.entity_id=f.object_entity_id
            WHERE f.scope=:s AND f.subject_id=CAST(:e AS uuid) AND f.valid_to IS NULL AND f.recorded_to IS NULL
            ORDER BY f.predicate"""), {"s": sc, "e": entity_id}).fetchall()
    return {"entity_id": entity_id, "edges": [dict(fact_id=r[0], predicate=r[1],
             object=(r[4] if r[2] != "literal" else (r[3] or {}).get("value")), confidence=r[5]) for r in rows]}


@mcp.tool()
def facts_timeline(subject: str, predicate: str, scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Bi-temporal supersession chain: every historical value of (subject, predicate)."""
    sc = _eff_scope(ctx, scope)
    with session_scope() as c:
        rows = c.execute(text("""SELECT fact_id::text, object_value->>'value', valid_from::text, valid_to::text, confidence
            FROM facts WHERE scope=:s AND subject_id=CAST(:sub AS uuid) AND predicate=:p AND recorded_to IS NULL
            ORDER BY valid_from"""), {"s": sc, "sub": subject, "p": predicate}).fetchall()
    return {"versions": [dict(fact_id=r[0], value=r[1], valid_from=r[2], valid_to=r[3], confidence=r[4]) for r in rows]}


@mcp.tool()
def list_beliefs(scope: Optional[str] = None, about: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """List probabilistic beliefs (higher-order claims with evidence chains)."""
    sc = _eff_scope(ctx, scope)
    sql = """SELECT b.belief_id::text, b.stance, b.claim, b.confidence, e.canonical_name, b.supports::text[]
             FROM beliefs b JOIN entities e ON e.entity_id=b.about_entity_id
             WHERE b.scope=:s AND b.valid_to IS NULL AND b.recorded_to IS NULL"""
    p: Dict[str, Any] = {"s": sc}
    if about:
        sql += " AND b.about_entity_id=CAST(:a AS uuid)"; p["a"] = about
    sql += " LIMIT 50"
    with session_scope() as c:
        rows = c.execute(text(sql), p).fetchall()
    return {"items": [dict(belief_id=r[0], stance=r[1], claim=r[2], confidence=r[3],
                           about=r[4], supports=list(r[5] or [])) for r in rows]}


# ── bulk / forget / erasures ────────────────────────────────────────────────
@mcp.tool()
def bulk_ingest(texts: List[str], scope: Optional[str] = None, modality: str = "conversation",
                ctx: Context = None) -> Dict[str, Any]:
    """Store many memories at once (async extraction via worker queue)."""
    sc = _eff_scope(ctx, scope)
    items = [{"modality": modality, "content": {"kind": "message", "role": "user", "text": t},
              "context": {}, "idempotency_key": f"mcp-bulk-{uuid.uuid4().hex[:12]}"} for t in texts]
    return ingest.bulk_ingest(scope=sc, items=items, source="mcp", caller="mcp")


@mcp.tool()
def memory_forget(predicate: Optional[str] = None, about_entity: Optional[str] = None,
                  scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Soft-forget derived facts matching selector (non-destructive: closes recorded_to)."""
    sc = _eff_scope(ctx, scope)
    where, p = "scope=:s AND recorded_to IS NULL", {"s": sc}
    if predicate:
        where += " AND predicate=:p"; p["p"] = predicate
    if about_entity:
        where += " AND subject_id=CAST(:a AS uuid)"; p["a"] = about_entity
    with session_scope() as c:
        n = c.execute(text(f"UPDATE facts SET recorded_to=now() WHERE {where}"), p).rowcount or 0
    return {"forgotten_facts": n, "scope": sc}


@mcp.tool()
def erasure_preview(about_entity: Optional[str] = None, predicate: Optional[str] = None,
                    scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """GDPR erasure dry-run: which events REDACTED (referenced) vs DELETED (orphaned)."""
    return erasures.preview_erasure(scope=_eff_scope(ctx, scope),
                                    selector={"about_entity": about_entity, "predicate": predicate})


@mcp.tool()
def erasure_execute(scope: Optional[str] = None, about_entity: Optional[str] = None,
                    predicate: Optional[str] = None, from_preview_id: Optional[str] = None,
                    ctx: Context = None) -> Dict[str, Any]:
    """Execute GDPR erasure (true delete orphan events, redact referenced ones)."""
    return erasures.execute_erasure(scope=_eff_scope(ctx, scope),
                                    selector={"about_entity": about_entity, "predicate": predicate} if not from_preview_id else None,
                                    from_preview_id=from_preview_id)


# ── episodes / vocab / temporal / admin ────────────────────────────────────
@mcp.tool()
def episodes_build(scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Segment events into episodes (30-min gaps + causal chains)."""
    return episodes.segment_scope(_eff_scope(ctx, scope))


@mcp.tool()
def episodes_list(scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """List sealed episodes."""
    return {"items": episodes.list_episodes(_eff_scope(ctx, scope))}


@mcp.tool()
def vocab_list(scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """List controlled vocabularies."""
    sc = _eff_scope(ctx, scope)
    with session_scope() as c:
        rows = c.execute(text("""SELECT v.name, v.kind,
            coalesce(json_agg(json_build_object('canonical', vv.canonical_value, 'aliases', vv.aliases)) FILTER (WHERE vv.canonical_value IS NOT NULL),'[]') AS vals
            FROM vocabularies v LEFT JOIN vocabulary_values vv ON vv.vocab_id=v.vocab_id
            WHERE v.scope=:s GROUP BY v.vocab_id"""), {"s": sc}).fetchall()
    return {"items": [dict(name=r[0], kind=r[1], values=r[2]) for r in rows]}


@mcp.tool()
def vocab_create(name: str, kind: str = "closed", values: Optional[List[Dict[str, Any]]] = None,
                 scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Create a vocabulary. values=[{canonical, aliases:[...]}]. kind=closed|open."""
    sc = _eff_scope(ctx, scope)
    with session_scope() as c:
        vid = c.execute(text("INSERT INTO vocabularies (scope,name,kind,description) VALUES (:s,:n,:k,:d) RETURNING vocab_id"),
                        {"s": sc, "n": name, "k": kind, "d": name}).fetchone().vocab_id
        for v in (values or []):
            c.execute(text("INSERT INTO vocabulary_values (vocab_id,canonical_value,aliases) VALUES (:v,:c,CAST(:a AS text[])) ON CONFLICT DO NOTHING"),
                      {"v": str(vid), "c": v.get("canonical"), "a": "{" + ",".join(v.get("aliases", [])) + "}"})
    return {"vocab_id": str(vid), "name": name, "kind": kind, "scope": sc}


@mcp.tool()
def temporal_list() -> Dict[str, Any]:
    """List NL temporal phrases + ISO-duration expressions."""
    temporal.seed_defaults()
    return {"items": temporal.list_phrases()}


@mcp.tool()
def temporal_register(name: str, expression: str) -> Dict[str, Any]:
    """Register a temporal phrase. expression = 'dur..dur' (e.g. -P7D..P0D)."""
    return {"phrase_id": temporal.register_phrase(name, expression), "name": name.lower(), "expression": expression}


@mcp.tool()
def admin_metrics(scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Storage metrics: row counts + job queue status."""
    sc = scope or "%"
    with session_scope() as c:
        def cnt(sql, **p):
            return c.execute(text(sql), p).scalar()
        jobs = {st: cnt("SELECT count(*) FROM jobs WHERE scope LIKE :s AND status=:st", s=sc, st=st)
                for st in ("queued", "running", "completed", "failed")}
    return {"scope": scope, "jobs_by_status": jobs}


@mcp.tool()
def export_scope(scope: Optional[str] = None, ctx: Context = None) -> Dict[str, Any]:
    """Export a scope's events+facts+beliefs as JSONL (round-trippable)."""
    return export_data.export_scope(_eff_scope(ctx, scope))


# ── 传输入口 ────────────────────────────────────────────────────────────────
class _AuthASGI:
    """可选静态 key 鉴权(config.api.key 非空时,要求 Authorization: Bearer <key>)。"""

    def __init__(self, app, key: str):
        self.app = app
        self.key = (key or "").strip()

    async def __call__(self, scope, receive, send):
        if self.key and scope.get("type") == "http":
            auth = ""
            for k, v in scope.get("headers", []):
                if k.decode().lower() == "authorization":
                    auth = v.decode(); break
            if auth != f"Bearer {self.key}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"error":"unauthorized: need Authorization: Bearer <api.key>"}'})
                return
        await self.app(scope, receive, send)


def http_app(require_auth: bool = True):
    """返回 streamable-http ASGI app(可选静态 key 鉴权)。"""
    app = mcp.streamable_http_app()
    key = load_config().api.key if require_auth else ""
    return _AuthASGI(app, key) if key else app


def main_stdio() -> None:
    """stdio 传输(本地单 agent)。"""
    mcp.run(transport="stdio")


def main_http(host: str = "0.0.0.0", port: int = 8001) -> None:
    """streamable-http 传输(多人共享)。agent 连 http://<host>:<port>/mcp,
    带 X-Cortex-Scope 头(每用户自己的 scope)。"""
    import uvicorn
    key = load_config().api.key
    print(f"cortex MCP(streamable-http)on http://{host}:{port}/mcp  "
          f"{'(需 Authorization: Bearer <api.key>)' if key else '(无鉴权,仅受信网络)'}", flush=True)
    uvicorn.run(http_app(require_auth=True), host=host, port=port)


if __name__ == "__main__":
    main_stdio()
