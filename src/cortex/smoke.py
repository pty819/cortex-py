"""端到端冒烟:reset → 入库 → 抽取 → recall → answer。直接调函数 + HTTP(TestClient)。"""
from __future__ import annotations

import json
import os
import time

from sqlalchemy import text

from . import services
from .config import load_config, llm_configured
from .core import append_event, enqueue_job
from .db import init_schema, session_scope
from .extraction.pipeline import extract_event
from .retrieval import recall


def _banner(s):
    print(f"\n=== {s} ===")


def run_smoke() -> int:
    _banner("0. initialize schema")
    init_schema(drop=False)
    print("  schema initialized (non-destructive)")

    scope = "org:acme/dept:sales/user:alice"
    text_body = ("Priya Rao works at Acme Corp. Priya Rao owns Q3 Renewal. "
                 "Acme Corp signed the renewal deal. Q3 Renewal uses the platform.")

    _banner("1. ingest experience (WAL append)")
    eid, off = append_event(scope=scope, modality="conversation",
                            content={"kind": "message", "role": "user", "text": text_body},
                            context={"observed_at": "2026-06-18T10:00:00Z", "labels": ["acme"]},
                            caller="user:alice", idempotency_key=f"smoke-{int(time.time())}")
    print(f"  event_id={eid} wal_offset={off}")
    jid = enqueue_job(job_type="extract", scope=scope, event_id=eid)
    print(f"  enqueued extract job={jid}")

    extraction_configured = llm_configured("extraction")
    _banner("2. async extraction (mock" + ("" if not extraction_configured else f"/{load_config().llm.extraction.model}") + ")")
    previous_mock_flag = os.environ.get("CORTEX_ALLOW_MOCK_EXTRACTION")
    if not extraction_configured:
        os.environ["CORTEX_ALLOW_MOCK_EXTRACTION"] = "true"
    try:
        res = extract_event(eid)
    finally:
        if not extraction_configured:
            if previous_mock_flag is None:
                os.environ.pop("CORTEX_ALLOW_MOCK_EXTRACTION", None)
            else:
                os.environ["CORTEX_ALLOW_MOCK_EXTRACTION"] = previous_mock_flag
    print(f"  {res}")
    if res.get("facts_extracted", 0) == 0:
        print("  ⚠️ 0 facts extracted — 抽取/链接可能需调试")

    _banner("3. verify graph grown")
    with session_scope() as c:
        ne = c.execute(text("SELECT count(*) FROM entities WHERE scope=:s AND merged_into IS NULL"), {"s": scope}).scalar()
        nf = c.execute(text("SELECT count(*) FROM facts WHERE scope=:s AND valid_to IS NULL AND recorded_to IS NULL"), {"s": scope}).scalar()
        print(f"  entities={ne} facts={nf}")
        sample = c.execute(text("""SELECT s.canonical_name, f.predicate, o.canonical_name, f.object_value->>'value'
            FROM facts f JOIN entities s ON s.entity_id=f.subject_id
            LEFT JOIN entities o ON o.entity_id=f.object_entity_id
            WHERE f.scope=:s LIMIT 6"""), {"s": scope}).fetchall()
        for r in sample:
            print(f"    ({r[0]}) --{r[1]}--> {r[2] or r[3]}")

    _banner("4. recall (4 通道 + RRF + rerank,真实 embedding)")
    pack = recall(scope=scope, query="who owns the Q3 Renewal", view="local")
    print(f"  pack_id={pack['pack_id']}")
    print(f"  channels={pack['diagnostics']['channels']} time_ms={pack['diagnostics']['time_ms']}")
    print(f"  facts={len(pack['layers']['facts'])} beliefs={len(pack['layers']['beliefs'])}")
    print(f"  context_block: {pack['context_block'][:200]}")

    _banner("5. answer")
    if services.llm_configured("answer"):
        try:
            raw = services.llm_chat("answer",
                "依据记忆(含[n]引用)回答,保留引用标记。",
                json.dumps({"query": "who owns the Q3 Renewal", "pack_layers": pack["layers"]}))
            print(f"  [real LLM] {services.strip_think(raw)[:300]}")
        except Exception as e:  # noqa: BLE001
            print(f"  [mock fallback,{e.__class__.__name__}] {services.mock_answer('who owns Q3', json.dumps(pack))[:200]}")
    else:
        ans = services.mock_answer("who owns the Q3 Renewal", json.dumps(pack))
        print(f"  [mock] {ans[:300]}")

    _banner("done — pipeline 端到端跑通")
    return 0
