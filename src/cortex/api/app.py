"""FastAPI 端点:experience/recall/answer/forget + 层直读 + lifecycle SSE。

静态 key + X-Cortex-Actor;scope 由请求体带(强制过滤在查询层)。
"""
from __future__ import annotations

import asyncio
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, Query, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sse_starlette.sse import EventSourceResponse

from ..config import load_config
from ..core import append_event, enqueue_job, emit_lifecycle, IdempotencyConflict, list_lifecycle_since
from ..db import session_scope
from .. import schemas, services
from ..retrieval import recall, get_cached_pack
from .. import ingest, export_data
from .. import erasures, episodes, temporal
from .. import maintenance as maint
from .. import understanding as und

app = FastAPI(title="cortex", version="0.1.0")
cfg = load_config()
app.add_middleware(CORSMiddleware, allow_origins=cfg.api.cors_origins or ["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── auth dependency ─────────────────────────────────────────────────────────
def auth(authorization: str = Header(default=""), x_cortex_actor: str = Header(default="user:alice")):
    if cfg.api.key:
        token = authorization.replace("Bearer ", "").strip()
        if token != cfg.api.key:
            raise HTTPException(401, "invalid api key")
    return x_cortex_actor or "user:alice"


# ── health ──────────────────────────────────────────────────────────────────
@app.get("/v1/scopes/list")
def scopes_list(prefix: str = Query(""), limit: int = 100, actor: str = Depends(auth)):
    """列出 DB 里的 scope(动态,供前端下拉框用)。可选前缀过滤。"""
    sql = "SELECT scope_path, auto_provisioned FROM scopes"
    p: dict = {"lim": limit}
    if prefix:
        sql += " WHERE scope_path LIKE :p"; p["p"] = f"{prefix}%"
    sql += " ORDER BY scope_path LIMIT :lim"
    with session_scope() as c:
        rows = c.execute(text(sql), p).fetchall()
    # 同时从 facts/events 里补上 DB 有数据但 scopes 表没注册的 scope
    with session_scope() as c:
        extra = c.execute(text("""
            SELECT DISTINCT scope FROM (
                SELECT scope FROM facts UNION SELECT scope FROM events UNION SELECT scope FROM entities
            ) t WHERE scope NOT IN (SELECT scope_path FROM scopes)
            ORDER BY scope LIMIT :lim
        """), {"lim": limit}).fetchall()
    items = [{"scope_path": r[0], "auto_provisioned": r[1]} for r in rows]
    items += [{"scope_path": r[0], "auto_provisioned": True} for r in extra]
    return {"items": items}


@app.get("/v1/health")
def health():
    from ..db import assert_services_reachable
    return {"status": "ok", **assert_services_reachable()}


# ── experience (唯一写)──────────────────────────────────────────────────────
@app.post("/v1/experience", response_model=schemas.ExperienceResponse)
def experience(body: schemas.ExperienceRequest, wait: str = "", actor: str = Depends(auth)):
    content = body.content.model_dump(exclude_none=True)
    context = body.context.model_dump(exclude_none=True)
    try:
        eid, offset = append_event(scope=body.scope, modality=body.modality, content=content,
                                   context=context, caller=actor, observed_actor=body.observed_actor,
                                   subject=body.subject, directives=body.directives,
                                   idempotency_key=body.idempotency_key)
    except IdempotencyConflict as e:
        raise HTTPException(409, str(e))
    # enqueue 抽取 job(写路径无 LLM,只入队)
    enqueue_job(job_type="extract", scope=body.scope, event_id=eid, priority=0)
    status = "captured"
    stages: list = []
    elapsed_ms: dict = {}
    if wait in ("captured", "indexed", "consolidated"):
        from ..core import wait_for_stage
        timeout = 30.0 if wait != "consolidated" else 45.0
        res = wait_for_stage(eid, wait, timeout=timeout)
        if res["reached"]:
            status = wait
            stages = res.get("stages_completed", [])
            elapsed_ms = {"wait_ms": res.get("elapsed_ms", 0)}
        else:
            status = "captured"  # 超时降级 async
    return schemas.ExperienceResponse(event_id=eid, wal_offset=offset, status=status,
                                      lifecycle_stream=f"/v1/lifecycle/stream?event_id={eid}")


# ── lifecycle SSE ───────────────────────────────────────────────────────────
@app.get("/v1/lifecycle/stream")
async def lifecycle_stream(request: Request, event_id: str = Query(None), scope: str = Query(None),
                           actor: str = Depends(auth)):
    async def gen():
        seen: set = set()
        # 先补一帧 captured
        for _ in range(120):  # 最多 ~2 分钟
            if await request.is_disconnected():
                return
            with session_scope() as conn:
                rows = list_lifecycle_since(conn, scope=scope, event_id=event_id, limit=50)
            for r in rows:
                if r["lifecycle_id"] in seen:
                    continue
                seen.add(r["lifecycle_id"])
                yield {"event": "lifecycle", "data": __import__("json").dumps(r)}
            if event_id:
                # 该 event 已 extracted/indexed 即可多等一会
                kinds = {r["kind"] for r in rows if r.get("event_id") == event_id}
                if "indexed" in kinds or "failed" in kinds:
                    await asyncio.sleep(0.2)
                    yield {"event": "done", "data": "{}"}
                    return
            await asyncio.sleep(1.0)
    return EventSourceResponse(gen())


# ── 层直读 ──────────────────────────────────────────────────────────────────
@app.get("/v1/entities")
def list_entities(scope: str, q: str = Query(None), limit: int = 100, actor: str = Depends(auth)):
    with session_scope() as c:
        sql = """SELECT entity_id::text, canonical_name, entity_type, description, merged_into::text
                 FROM entities WHERE scope=:s AND merged_into IS NULL"""
        p: dict = {"s": scope, "lim": limit}
        if q:
            sql += " AND (canonical_name ILIKE :q OR description ILIKE :q)"
            p["q"] = f"%{q}%"
        sql += " ORDER BY created_at DESC LIMIT :lim"
        rows = c.execute(text(sql), p).fetchall()
    return {"items": [dict(entity_id=r[0], canonical_name=r[1], entity_type=r[2],
                           description=r[3], merged_into=r[4]) for r in rows]}


@app.get("/v1/facts")
def list_facts(scope: str, subject: str = Query(None), predicate: str = Query(None),
               as_of: str = Query(None), include_superseded: bool = Query(False),
               limit: int = 100, actor: str = Depends(auth)):
    """列出 facts。as_of 裁剪双轴;include_superseded=true 返回历史超替版本(recorded_to<=as_of)。"""
    sql = """SELECT f.fact_id::text, f.predicate, f.object_type, f.object_value,
                    o.canonical_name AS oname, s.canonical_name AS sname,
                    s.entity_id::text AS sid, o.entity_id::text AS oid,
                    f.confidence, f.valid_from::text, f.valid_to::text
             FROM facts f JOIN entities s ON s.entity_id=f.subject_id
             LEFT JOIN entities o ON o.entity_id=f.object_entity_id
             WHERE f.scope=:s"""
    p: dict = {"s": scope, "lim": limit}
    if not include_superseded:
        sql += " AND f.recorded_to IS NULL"
    if as_of:
        if include_superseded:
            sql += " AND f.recorded_from <= CAST(:ao AS timestamptz)"
        else:
            sql += " AND f.valid_from <= CAST(:ao AS timestamptz) AND (f.valid_to IS NULL OR CAST(:ao AS timestamptz) < f.valid_to)"
        p["ao"] = as_of
    if subject:
        sql += " AND f.subject_id=CAST(:sub AS uuid)"; p["sub"] = subject
    if predicate:
        sql += " AND f.predicate=:pred"; p["pred"] = predicate
    sql += " ORDER BY f.valid_from DESC NULLS LAST LIMIT :lim"
    with session_scope() as c:
        rows = c.execute(text(sql), p).fetchall()
    return {"items": [dict(fact_id=r[0], predicate=r[1],
                           subject={"id": r[6], "name": r[5]},
                           object=({"datatype": r[2], "value": r[4] or (r[3] or {}).get("value")}
                                   if r[2] != "literal" else {"datatype": "literal", "value": (r[3] or {}).get("value")}),
                           confidence=r[8], valid_from=r[9], valid_to=r[10]) for r in rows]}


@app.get("/v1/facts/timeline", response_model=schemas.TimelineResponse)
def facts_timeline(scope: str, subject: str, predicate: str, actor: str = Depends(auth)):
    with session_scope() as c:
        rows = c.execute(text("""
            SELECT fact_id::text, object_value->>'value', valid_from::text, valid_to::text, confidence
            FROM facts WHERE scope=:s AND subject_id=CAST(:sub AS uuid) AND predicate=:p AND recorded_to IS NULL
            ORDER BY valid_from
        """), {"s": scope, "sub": subject, "p": predicate}).fetchall()
    return {"subject": subject, "predicate": predicate,
            "versions": [dict(fact_id=r[0], object_value=r[1], valid_from=r[2],
                              valid_to=r[3], confidence=r[4]) for r in rows]}


@app.get("/v1/beliefs")
def list_beliefs(scope: str, about: str = Query(None), actor: str = Depends(auth)):
    sql = """SELECT b.belief_id::text, b.stance, b.claim, b.confidence,
                    b.about_entity_id::text, e.canonical_name, b.supports::text[]
             FROM beliefs b JOIN entities e ON e.entity_id=b.about_entity_id
             WHERE b.scope=:s AND b.valid_to IS NULL AND b.recorded_to IS NULL"""
    p: dict = {"s": scope}
    if about:
        sql += " AND b.about_entity_id=CAST(:a AS uuid)"; p["a"] = about
    sql += " LIMIT 50"
    with session_scope() as c:
        rows = c.execute(text(sql), p).fetchall()
    return {"items": [dict(belief_id=r[0], stance=r[1], claim=r[2], confidence=r[3],
                           about={"id": r[4], "name": r[5]}, supports=list(r[6] or [])) for r in rows]}


@app.get("/v1/beliefs/why")
def beliefs_why(belief_id: str, actor: str = Depends(auth)):
    """遍历 belief → facts → events 支持图,渲染 narrative。"""
    with session_scope() as c:
        b = c.execute(text("""SELECT belief_id::text, stance, claim, confidence, about_entity_id::text,
            e.canonical_name, supports::text[] FROM beliefs b JOIN entities e ON e.entity_id=b.about_entity_id
            WHERE b.belief_id=CAST(:id AS uuid)"""), {"id": belief_id}).fetchone()
        if not b:
            raise HTTPException(404, "belief not found")
        belief = dict(belief_id=b[0], stance=b[1], claim=b[2], confidence=b[3],
                      about={"id": b[4], "name": b[5]}, supports=list(b[6] or []))
        nodes, edges = [], []
        nodes.append({"id": belief["belief_id"], "type": "belief", "weight": belief["confidence"],
                      "summary": belief["claim"]})
        # 取 supporting facts
        fact_ids = belief["supports"]
        facts = []
        if fact_ids:
            frows = c.execute(text("""SELECT f.fact_id::text, f.predicate, f.object_value->>'value',
                f.confidence, f.supports::text[], s.canonical_name FROM facts f
                JOIN entities s ON s.entity_id=f.subject_id WHERE f.fact_id = ANY(CAST(:ids AS uuid[]))"""),
                {"ids": "{" + ",".join(fact_ids) + "}"}).fetchall()
            for fr in frows:
                fid = fr[0]
                facts.append(fid)
                nodes.append({"id": fid, "type": "fact", "weight": fr[3], "summary": f"{fr[5]} {fr[1]} {fr[2] or ''}"})
                edges.append({"from": belief["belief_id"], "to": fid, "relation": "supported_by"})
                # fact → supporting events
                for eid in (fr[4] or []):
                    ev = c.execute(text("SELECT content->>'text' FROM events WHERE event_id=CAST(:e AS uuid)"),
                                   {"e": eid}).fetchone()
                    if ev:
                        nodes.append({"id": eid, "type": "event", "weight": 1.0, "summary": (ev[0] or "")})  # 不截:保留完整事件文本便于溯源
                        edges.append({"from": fid, "to": eid, "relation": "extracted_from"})
    # narrative
    narrative, nmodel = "", "mock"
    if services.llm_configured("synthesis"):
        try:
            import json as _j
            payload = _j.dumps({"belief": belief["claim"], "facts": [n["summary"] for n in nodes if n["type"] == "fact"]})
            raw = services.llm_chat("synthesis", __import__("cortex.prompts", fromlist=["BELIEFS_WHY_NARRATIVE"]).BELIEFS_WHY_NARRATIVE, payload)
            narrative = services.strip_think(raw); nmodel = load_config().llm.synthesis.model
        except Exception:  # noqa: BLE001
            narrative = f"{belief['about']['name']} {belief['claim']}(基于 {len(facts)} 条事实)。"
    else:
        narrative = f"{belief['about']['name']} {belief['claim']}(基于 {len(facts)} 条事实)。"
    return {"belief": belief, "support_graph": {"nodes": nodes, "edges": edges},
            "narrative": narrative, "narrative_model": nmodel}


@app.post("/v1/beliefs/build")
def beliefs_build(body: dict, actor: str = Depends(auth)):
    """手动触发某 scope 的 belief 聚合。"""
    scope = body.get("scope")
    if not scope:
        raise HTTPException(422, "scope required")
    from ..extraction.pipeline import _aggregate_belief_for_scope
    n = _aggregate_belief_for_scope(scope)
    return {"built": n, "scope": scope}


# ── recall / answer ─────────────────────────────────────────────────────────
@app.post("/v1/recall")
def do_recall(body: schemas.RecallRequest, actor: str = Depends(auth)):
    valid_during = None
    if body.temporal and body.temporal.get("natural"):
        from datetime import datetime
        ref = None
        if body.temporal.get("reference_date"):
            try:
                ref = datetime.fromisoformat(body.temporal["reference_date"].replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                ref = None
        win = temporal.parse_temporal(body.temporal["natural"], ref)
        if win:
            valid_during = (win[0].isoformat(), win[1].isoformat())
    rd = body.recorded_during
    rd_t = ((rd or {}).get("from"), (rd or {}).get("to")) if rd else None
    return recall(scope=body.scope, query=body.query, view=body.view, top_k=body.top_k,
                  as_of=body.as_of, valid_during=valid_during, recorded_during=rd_t,
                  include_superseded=body.include_superseded, budgets=body.budgets,
                  citation_mode=body.citation_mode, exclude_content=body.exclude_content)


@app.post("/v1/recall/stream")
async def recall_stream(body: schemas.RecallRequest, actor: str = Depends(auth)):
    """StratifiedPack 逐层 SSE:plan→facts→beliefs→events→context_block→provenance→diagnostics→done。"""
    import json as _j
    async def gen():
        # 先同步算 pack(复用 recall),再按层 emit
        pack = do_recall(body, actor)
        yield {"event": "plan", "data": _j.dumps({"scope": body.scope, "channels": pack["diagnostics"]["channels"]})}
        for layer in ("facts", "beliefs", "events"):
            yield {"event": "layer", "data": _j.dumps({"layer": layer, "items": pack["layers"][layer]})}
        yield {"event": "context_block", "data": _j.dumps({"text": pack["context_block"]})}
        yield {"event": "provenance", "data": _j.dumps(pack["provenance"])}
        yield {"event": "diagnostics", "data": _j.dumps(pack["diagnostics"])}
        yield {"event": "done", "data": _j.dumps({"pack_id": pack["pack_id"]})}
    return EventSourceResponse(gen())


@app.post("/v1/answer", response_model=schemas.AnswerResponse)
def do_answer(body: schemas.AnswerRequest, actor: str = Depends(auth)):
    if body.use_pack_id:
        pack = get_cached_pack(body.use_pack_id)
        if not pack:
            raise HTTPException(404, "pack expired or not found")
    else:
        pack = recall(scope=body.scope, query=body.query)
    import json
    if services.llm_configured("answer"):
        try:
            raw = services.llm_chat("answer",
                __import__("cortex.prompts", fromlist=["ANSWER_SYSTEM"]).ANSWER_SYSTEM,
                json.dumps({"query": body.query, "pack_layers": pack["layers"]}))
            ans = services.strip_think(raw)
            model = load_config().llm.answer.model
        except Exception:  # noqa: BLE001
            ans = services.mock_answer(body.query, json.dumps(pack)); model = "mock"
    else:
        ans = services.mock_answer(body.query, json.dumps(pack)); model = "mock-extractor"
    citations = [schemas.Citation(marker=f"[{i+1}]", layer="fact", id=f["fact_id"])
                 for i, f in enumerate(pack["layers"]["facts"][:6])]
    # verifier(可选):异家族 LLM 对照 citations 校验幻觉
    verified = None
    vcfg = load_config().llm.verifier
    if vcfg.enabled and services.llm_configured("answer"):
        try:
            import json as _j
            vraw = services.llm_chat("verifier",
                __import__("cortex.prompts", fromlist=["VERIFIER_SYSTEM"]).VERIFIER_SYSTEM,
                _j.dumps({"answer": ans, "citations": pack["layers"]["facts"][:6]}))
            import json as _j2
            verified = services.parse_llm_json(vraw) if vraw else None
        except Exception:  # noqa: BLE001
            verified = None
    return schemas.AnswerResponse(answer=ans, citations=citations, model_used=model, pack_id=pack["pack_id"])


# ── forget(derived_only:recorded_to 软关)──────────────────────────────────
@app.post("/v1/forget", response_model=schemas.ForgetResponse)
def forget(body: schemas.ForgetRequest, actor: str = Depends(auth)):
    if not (body.predicate or body.about_entity) and not body.confirm_all:
        raise HTTPException(422, "empty selector needs confirm_all=true")
    with session_scope() as conn:
        where = "scope=:s AND recorded_to IS NULL"
        p: dict = {"s": body.scope}
        if body.predicate:
            where += " AND predicate=:pred"; p["pred"] = body.predicate
        if body.about_entity:
            where += " AND subject_id=CAST(:a AS uuid)"; p["a"] = body.about_entity
        n_facts = conn.execute(text(f"UPDATE facts SET recorded_to=now() WHERE {where}"), p).rowcount or 0
        p2 = dict(p); p2["a"] = body.about_entity
        n_bel = conn.execute(text(f"UPDATE beliefs SET recorded_to=now() WHERE scope=:s AND recorded_to IS NULL"
                                  + (" AND about_entity_id=CAST(:a AS uuid)" if body.about_entity else "")),
                             {"s": body.scope, "a": body.about_entity}).rowcount or 0 if body.about_entity else 0
        aid = emit_lifecycle(conn, kind="forgotten", scope=body.scope,
                             payload={"facts": n_facts, "beliefs": n_bel, "cascade": body.cascade})
        if body.cascade == "redact_events":
            conn.execute(text("""UPDATE events SET content='{}'::jsonb, excluded_from_recall=true
                WHERE scope=:s AND event_id IN (
                  SELECT unnest(supports) FROM facts WHERE scope=:s AND recorded_to IS NOT NULL)"""),
                         {"s": body.scope})
    return schemas.ForgetResponse(deleted={"facts": n_facts, "beliefs": n_bel}, audit_id=aid)


# ── Stage 6: bulk / import / export ─────────────────────────────────────────
@app.post("/v1/experience/bulk", response_model=schemas.ImportResponse)
def experience_bulk(body: schemas.BulkExperienceRequest, actor: str = Depends(auth)):
    items = [{"scope": body.scope, "modality": it.modality,
              "content": it.content.model_dump(exclude_none=True),
              "context": it.context.model_dump(exclude_none=True),
              "observed_actor": it.observed_actor, "subject": it.subject,
              "directives": it.directives, "idempotency_key": it.idempotency_key} for it in body.items]
    res = ingest.bulk_ingest(scope=body.scope, items=items, source="bulk",
                             ordering=body.ordering, caller=actor)
    return schemas.ImportResponse(import_id=res["import_id"], source="bulk",
                                  accepted=res["accepted"], failed=res["failed"],
                                  lifecycle_stream=f"/v1/lifecycle/stream?scope={body.scope}")


@app.post("/v1/import/jsonl", response_model=schemas.ImportResponse)
def imp_jsonl(body: schemas.ImportJsonlRequest, actor: str = Depends(auth)):
    res = ingest.import_jsonl(body.scope, body.lines, body.scope_template)
    return schemas.ImportResponse(import_id=res["import_id"], source="jsonl",
                                  accepted=res["accepted"], failed=res["failed"],
                                  lifecycle_stream=f"/v1/lifecycle/stream?scope={body.scope}")


@app.post("/v1/import/mem0", response_model=schemas.ImportResponse)
def imp_mem0(body: schemas.ImportMem0Request, actor: str = Depends(auth)):
    res = ingest.import_mem0(body.scope, [m.model_dump() for m in body.memories], body.scope_template)
    return schemas.ImportResponse(import_id=res["import_id"], source="mem0",
                                  accepted=res["accepted"], failed=res["failed"],
                                  lifecycle_stream=f"/v1/lifecycle/stream?scope={body.scope}")


@app.post("/v1/import/zep", response_model=schemas.ImportResponse)
def imp_zep(body: schemas.ImportZepRequest, actor: str = Depends(auth)):
    res = ingest.import_zep_direct(scope=body.scope,
                                   facts=[f.model_dump() for f in body.facts])
    return schemas.ImportResponse(import_id=res["import_id"], source="zep",
                                  accepted=res["accepted"], failed=res["failed"],
                                  lifecycle_stream=f"/v1/lifecycle/stream?scope={body.scope}")


@app.post("/v1/import/letta", response_model=schemas.ImportResponse)
def imp_letta(body: schemas.ImportLettaRequest, actor: str = Depends(auth)):
    res = ingest.import_letta(body.scope, [b.model_dump() for b in body.blocks], body.scope_template)
    return schemas.ImportResponse(import_id=res["import_id"], source="letta",
                                  accepted=res["accepted"], failed=res["failed"],
                                  lifecycle_stream=f"/v1/lifecycle/stream?scope={body.scope}")


@app.post("/v1/import/openai", response_model=schemas.ImportResponse)
def imp_openai(body: schemas.ImportOpenAIRequest, actor: str = Depends(auth)):
    res = ingest.import_openai_mem(body.scope, [m.model_dump() for m in body.memories], body.scope_template)
    return schemas.ImportResponse(import_id=res["import_id"], source="openai",
                                  accepted=res["accepted"], failed=res["failed"],
                                  lifecycle_stream=f"/v1/lifecycle/stream?scope={body.scope}")


@app.get("/v1/import/{import_id}", response_model=schemas.ImportStatus)
def import_status(import_id: str, actor: str = Depends(auth)):
    st = ingest.get_import_status(import_id)
    if not st:
        raise HTTPException(404, "import not found")
    return schemas.ImportStatus(**st)


@app.post("/v1/export", response_model=schemas.ExportResponse)
def do_export(body: schemas.ExportRequest, actor: str = Depends(auth)):
    res = export_data.export_scope(body.scope)
    return schemas.ExportResponse(**res)


# ── 长文档切块入库(机械结构等)─────────────────────────────────────────────
@app.post("/v1/ingest/document")
def ingest_document(body: schemas.IngestDocumentRequest, actor: str = Depends(auth)):
    """长文档切块 → 每块一条 experience(带 path/heading context)→ 异步抽取。
    机械结构文档按标题切,块间靠 part_of 抽取自然连接。"""
    from ..chunking import chunk_document
    chunks = chunk_document(body.text, min_chars=body.min_chars, max_chars=body.max_chars)
    if not chunks:
        raise HTTPException(422, "empty document after chunking")
    items = []
    for i, c in enumerate(chunks):
        # 块文本带标题前缀,让 LLM 知道当前层级
        prefix = f"# {c['path']}\n" if c.get("path") else ""
        items.append({
            "scope": body.scope,                    # ← 修复:bulk_ingest 需要 scope 字段
            "modality": "document",
            "content": {"kind": "text", "text": prefix + c["text"]},
            "context": {"intent": body.intent, "labels": [c.get("heading", "")] if c.get("heading") else [],
                        "chunk_path": c.get("path", ""), "chunk_depth": c.get("depth", 0)},
            "idempotency_key": f"doc-{uuid.uuid4().hex[:12]}-{i}",
        })
    res = ingest.bulk_ingest(scope=body.scope, items=items, source="document", caller=actor)
    return {"chunks": len(chunks), **res}


# ── Stage 7: erasures / episodes / vocab / temporal / admin ─────────────────
# erasures
@app.post("/v1/erasures/preview")
def erasure_preview(body: schemas.ErasurePreviewRequest, actor: str = Depends(auth)):
    return erasures.preview_erasure(scope=body.scope, selector=body.selector.model_dump(exclude_none=True))


@app.get("/v1/erasures/preview/{preview_id}/manifest")
def erasure_manifest(preview_id: str, actor: str = Depends(auth)):
    mf = erasures.get_manifest(preview_id)
    if not mf:
        raise HTTPException(404, "preview not found")
    if mf.get("expired"):
        raise HTTPException(409, "manifest expired; re-run preview")
    return mf


@app.post("/v1/erasures")
def erasure_execute(body: schemas.ErasureExecuteRequest, actor: str = Depends(auth)):
    try:
        return erasures.execute_erasure(scope=body.scope,
                                        selector=body.selector.model_dump(exclude_none=True) if body.selector else None,
                                        from_preview_id=body.from_preview_id)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/v1/erasures/{erasure_id}")
def erasure_status(erasure_id: str, actor: str = Depends(auth)):
    st = erasures.get_erasure_status(erasure_id)
    if not st:
        raise HTTPException(404, "erasure not found")
    return st


@app.post("/v1/erasures/{erasure_id}/cancel")
def erasure_cancel(erasure_id: str, actor: str = Depends(auth)):
    ok = erasures.cancel_erasure(erasure_id)
    return {"cancelled": ok}


# episodes + cases
@app.get("/v1/episodes")
def list_eps(scope: str, actor: str = Depends(auth)):
    return {"items": episodes.list_episodes(scope)}


@app.post("/v1/episodes/build")
def build_eps(body: dict, actor: str = Depends(auth)):
    scope = body.get("scope")
    if not scope:
        raise HTTPException(422, "scope required")
    return episodes.segment_scope(scope)


# ── Case CRUD(诊断 case 管理)──────────────────────────────────────────────
@app.post("/v1/cases")
def case_create(body: schemas.CaseCreateRequest, actor: str = Depends(auth)):
    return episodes.create_case(scope=body.scope, title=body.title, case_id=body.case_id,
                                equipment=body.equipment, lot=body.lot, recipe=body.recipe,
                                metadata=body.metadata)


@app.get("/v1/cases")
def case_list(scope: str, status: str = Query(None), equipment: str = Query(None),
              limit: int = 50, actor: str = Depends(auth)):
    return {"items": episodes.list_cases(scope, status=status, equipment=equipment, limit=limit)}


@app.get("/v1/cases/{episode_id}")
def case_get(episode_id: str, actor: str = Depends(auth)):
    c = episodes.get_case(episode_id)
    if not c:
        raise HTTPException(404, "case not found")
    return c


@app.patch("/v1/cases/{episode_id}")
def case_update(episode_id: str, body: schemas.CaseUpdateRequest, actor: str = Depends(auth)):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(422, "no fields to update")
    res = episodes.update_case(episode_id, **fields)
    if "error" in res:
        raise HTTPException(422, res["error"])
    return res


@app.post("/v1/cases/{episode_id}/events")
def case_add_event(episode_id: str, body: schemas.CaseAddEventRequest, actor: str = Depends(auth)):
    res = episodes.add_event_to_case(episode_id, body.event_id)
    if "error" in res:
        raise HTTPException(404, res["error"])
    return res


@app.post("/v1/cases/search")
def case_search(body: schemas.CaseSearchRequest, actor: str = Depends(auth)):
    return {"items": episodes.search_cases(body.scope, body.query)}


# vocab CRUD
@app.post("/v1/vocabularies")
def vocab_create(body: schemas.VocabCreateRequest, actor: str = Depends(auth)):
    with session_scope() as conn:
        vid = conn.execute(text("""
            INSERT INTO vocabularies (scope, name, kind, description)
            VALUES (:s,:n,:k,:d)
            ON CONFLICT (scope, name) DO UPDATE SET kind=EXCLUDED.kind, description=EXCLUDED.description
            RETURNING vocab_id
        """), {"s": body.scope, "n": body.name, "k": body.kind, "d": f"vocab {body.name}"}).fetchone().vocab_id
        for v in body.values:
            conn.execute(text("""
                INSERT INTO vocabulary_values (vocab_id, canonical_value, aliases)
                VALUES (:v,:c,CAST(:a AS text[])) ON CONFLICT DO NOTHING
            """), {"v": str(vid), "c": v.canonical, "a": "{" + ",".join(v.aliases) + "}"})
    return {"vocab_id": str(vid), "scope": body.scope, "name": body.name, "kind": body.kind}


@app.get("/v1/vocabularies")
def vocab_list(scope: str, actor: str = Depends(auth)):
    with session_scope() as conn:
        rows = conn.execute(text("""
            SELECT v.vocab_id::text, v.name, v.kind, v.description,
                   coalesce(json_agg(json_build_object('canonical', vv.canonical_value, 'aliases', vv.aliases)) FILTER (WHERE vv.canonical_value IS NOT NULL), '[]') AS vals
            FROM vocabularies v LEFT JOIN vocabulary_values vv ON vv.vocab_id=v.vocab_id
            WHERE v.scope=:s GROUP BY v.vocab_id ORDER BY v.name
        """), {"s": scope}).fetchall()
    return {"items": [dict(vocab_id=r[0], name=r[1], kind=r[2], description=r[3], values=r[4]) for r in rows]}


@app.get("/v1/vocabularies/{name}")
def vocab_get(name: str, scope: str, actor: str = Depends(auth)):
    with session_scope() as conn:
        row = conn.execute(text("""
            SELECT v.vocab_id::text, v.name, v.kind, v.description,
                   coalesce(json_agg(json_build_object('canonical', vv.canonical_value, 'aliases', vv.aliases)) FILTER (WHERE vv.canonical_value IS NOT NULL), '[]') AS vals
            FROM vocabularies v LEFT JOIN vocabulary_values vv ON vv.vocab_id=v.vocab_id
            WHERE v.scope=:s AND v.name=:n GROUP BY v.vocab_id
        """), {"s": scope, "n": name}).fetchone()
    if not row:
        raise HTTPException(404, "vocab not found")
    return dict(vocab_id=row[0], name=row[1], kind=row[2], description=row[3], values=row[4])


@app.put("/v1/vocabularies/{name}")
def vocab_replace(name: str, body: schemas.VocabReplaceRequest, actor: str = Depends(auth)):
    with session_scope() as conn:
        row = conn.execute(text("SELECT vocab_id FROM vocabularies WHERE scope=:s AND name=:n"),
                           {"s": body.scope, "n": name}).fetchone()
        if not row:
            raise HTTPException(404, "vocab not found")
        if body.kind:
            conn.execute(text("UPDATE vocabularies SET kind=:k WHERE vocab_id=:v"),
                         {"k": body.kind, "v": str(row.vocab_id)})
        conn.execute(text("DELETE FROM vocabulary_values WHERE vocab_id=:v"), {"v": str(row.vocab_id)})
        for val in body.values:
            conn.execute(text("""INSERT INTO vocabulary_values (vocab_id, canonical_value, aliases)
                VALUES (:v,:c,CAST(:a AS text[]))"""), {"v": str(row.vocab_id), "c": val.canonical,
                         "a": "{" + ",".join(val.aliases) + "}"})
    return {"replaced": name}


@app.delete("/v1/vocabularies/{name}")
def vocab_delete(name: str, scope: str, actor: str = Depends(auth)):
    with session_scope() as conn:
        r = conn.execute(text("DELETE FROM vocabularies WHERE scope=:s AND name=:n"),
                         {"s": scope, "n": name})
    if not (r.rowcount or 0):
        raise HTTPException(404, "vocab not found")
    return {"deleted": name}


# temporal phrases
@app.post("/v1/temporal/phrases")
def tphrase_create(body: schemas.TemporalPhraseRequest, actor: str = Depends(auth)):
    temporal.seed_defaults()
    pid = temporal.register_phrase(body.name, body.expression)
    return {"phrase_id": pid, "name": body.name.lower(), "expression": body.expression}


@app.get("/v1/temporal/phrases")
def tphrase_list(actor: str = Depends(auth)):
    temporal.seed_defaults()
    return {"items": temporal.list_phrases()}


@app.delete("/v1/temporal/phrases/{name}")
def tphrase_delete(name: str, actor: str = Depends(auth)):
    ok = temporal.delete_phrase(name)
    if not ok:
        raise HTTPException(404, "phrase not found")
    return {"deleted": name}


# admin
@app.get("/v1/admin/metrics")
def admin_metrics(scope: str = None, actor: str = Depends(auth)):
    with session_scope() as conn:
        def cnt(sql, **p):
            return conn.execute(text(sql), p).scalar()
        sc = scope or "%"
        jobs = {}
        for st in ("queued", "running", "completed", "failed"):
            jobs[st] = cnt("SELECT count(*) FROM jobs WHERE scope LIKE :s AND status=:st", s=sc, st=st)
        return {
            "scope": scope, "events": cnt("SELECT count(*) FROM events WHERE scope LIKE :s", s=sc),
            "facts": cnt("SELECT count(*) FROM facts WHERE scope LIKE :s AND recorded_to IS NULL", s=sc),
            "beliefs": cnt("SELECT count(*) FROM beliefs WHERE scope LIKE :s AND recorded_to IS NULL", s=sc),
            "entities": cnt("SELECT count(*) FROM entities WHERE scope LIKE :s AND merged_into IS NULL", s=sc),
            "episodes": cnt("SELECT count(*) FROM episodes WHERE scope LIKE :s", s=sc),
            "blobs": cnt("SELECT count(*) FROM blobs WHERE scope LIKE :s", s=sc),
            "jobs_by_status": jobs,
        }


@app.get("/v1/admin/version")
def admin_version(actor: str = Depends(auth)):
    from cortex import __version__
    with session_scope() as conn:
        tables = conn.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname='cortex'")).scalar()
    return {"version": __version__, "schema_tables": tables}


@app.post("/v1/admin/maintenance")
def admin_maintenance(body: schemas.MaintenanceRequest, actor: str = Depends(auth)):
    if body.action == "methylation":
        return maint.methylation_run(body.scope, body.older_than_days or 30)
    if body.action == "consolidation":
        return maint.consolidation_run(body.scope)
    raise HTTPException(422, "action must be methylation|consolidation")


# ── Understanding 层 ───────────────────────────────────────────────────────
@app.get("/v1/understanding")
def understanding_list(scope: str, topic: str = Query(None), limit: int = 50, actor: str = Depends(auth)):
    return {"items": und.list_concepts(scope, topic, limit)}


@app.get("/v1/understanding/coverage")
def understanding_coverage(scope: str, actor: str = Depends(auth)):
    return und.coverage(scope)


@app.get("/v1/understanding/{concept_id}")
def understanding_get(concept_id: str, actor: str = Depends(auth)):
    c = und.get_concept(concept_id)
    if not c:
        raise HTTPException(404, "concept not found")
    return c


@app.get("/v1/understanding/{concept_id}/related")
def understanding_related(concept_id: str, relation: str = Query(None), depth: int = 2, limit: int = 20,
                          actor: str = Depends(auth)):
    return {"items": und.related_concepts(concept_id, relation, depth, limit)}


@app.post("/v1/understanding/synthesize")
def understanding_synthesize(body: dict, actor: str = Depends(auth)):
    scope = body.get("scope")
    if not scope:
        raise HTTPException(422, "scope required")
    topics = body.get("topics")
    # 同步合成(MVP;官方是 202 async,我们直接跑,小 scope 够快)
    res = und.synthesize_scope(scope, topics=topics)
    return {"status": "completed", **res, "lifecycle_stream": f"/v1/lifecycle/stream?scope={scope}"}
