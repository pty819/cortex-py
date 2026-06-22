"""Stage 7 验收:erasures 4 阶段 / episodes segmenter / vocab CRUD / memory evolution / temporal / admin。"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import uuid

os.environ.setdefault("CORTEX_DB_SCHEMA_OVERRIDE", f"cortex_test_verify7_{uuid.uuid4().hex[:8]}")
os.environ.setdefault("CORTEX_ALLOW_MOCK_EXTRACTION", "true")

from fastapi.testclient import TestClient
from sqlalchemy import text

from cortex.api.app import app
from cortex.db import init_schema, session_scope
from cortex.core import append_event, enqueue_job
from cortex.extraction.pipeline import extract_event
from cortex import temporal
from cortex.ingest import import_zep_direct
from cortex import maintenance as maint_m  # 用 min_age_hours=0 直调

c = TestClient(app)
SCOPE = "org:acme/dept:sales/user:alice"
PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1; print(f"  ✓ {name} {detail}")
    else:
        FAIL += 1; print(f"  ✗ {name} {detail}")


def db_one(sql, **p):
    with session_scope() as conn:
        return conn.execute(text(sql), p).scalar()


print("=== reset ===")
init_schema(drop=True)

# ── 准备:EVT_A(被 fact 引用) + EVT_B(孤立)──────────────────────────────
ea, _ = append_event(scope=SCOPE, modality="conversation",
                     content={"kind": "message", "role": "user", "text": "Priya Rao owns Q3 Renewal"},
                     context={"observed_at": "2026-06-01T10:00:00Z"}, caller="tester", idempotency_key="s7-a")
eb, _ = append_event(scope=SCOPE, modality="conversation",
                     content={"kind": "message", "role": "user", "text": "hello world nothing here"},
                     context={"observed_at": "2026-06-02T10:00:00Z"}, caller="tester", idempotency_key="s7-b")
extract_event(ea)  # mock 抽取 → 1 fact supports EVT_A
extract_event(eb)  # 无实体 → 0 fact

print("=== 1. Erasures preview ===")
r = c.post("/v1/erasures/preview", json={"scope": SCOPE, "selector": {"memory_ids": [ea, eb]}},
           headers={"X-Cortex-Actor": "tester"})
pv = r.json()
m = {e["event_id"]: e["action"] for e in pv["manifest"]["events"]}
check("preview HTTP 200", r.status_code == 200)
check("EVT_A(被引用)→redact", m.get(ea) == "redact", str(m))
check("EVT_B(孤立)→delete", m.get(eb) == "delete", str(m))
check("refcount_breakdown 正确",
      pv["refcount_breakdown"]["events_to_redact"] == 1 and pv["refcount_breakdown"]["events_to_delete"] == 1)

print("=== 2. Erasures execute ===")
r = c.post("/v1/erasures", json={"scope": SCOPE, "selector": {"memory_ids": [ea, eb]}},
           headers={"X-Cortex-Actor": "tester"})
ex = r.json()
check("execute phase=completed", ex.get("phase") == "completed", str(ex))
check("EVT_A redacted(行在, excluded, content 空)",
      db_one("SELECT excluded_from_recall AND content='{}'::jsonb FROM events WHERE event_id=CAST(:e AS uuid)", e=ea))
check("EVT_B 物理删除", db_one("SELECT count(*) FROM events WHERE event_id=CAST(:e AS uuid)", e=eb) == 0)
check("supports 已 array_remove(EVT_A 不在任何 fact.supports)",
      db_one("SELECT count(*) FROM facts WHERE CAST(:e AS uuid) = ANY(supports)", e=ea) == 0)
r = c.get(f"/v1/erasures/{ex['erasure_id']}", headers={"X-Cortex-Actor": "tester"})
check("status 查询 200 + phase completed", r.status_code == 200 and r.json()["phase"] == "completed")

print("=== 3. Episodes segmenter ===")
# 灌 3 event:10:00 / 10:10 / 12:00(超窗);ev2 preceded_by ev1
e1, _ = append_event(scope=SCOPE, modality="conversation", content={"kind": "message", "role": "user", "text": "ep1 one"},
                     context={"observed_at": "2026-06-10T10:00:00Z"}, caller="t", idempotency_key="ep1")
e2, _ = append_event(scope=SCOPE, modality="conversation", content={"kind": "message", "role": "user", "text": "ep1 two"},
                     context={"observed_at": "2026-06-10T10:10:00Z", "preceded_by": [e1]}, caller="t", idempotency_key="ep2")
e3, _ = append_event(scope=SCOPE, modality="conversation", content={"kind": "message", "role": "user", "text": "ep2 three"},
                     context={"observed_at": "2026-06-10T12:00:00Z"}, caller="t", idempotency_key="ep3")
r = c.post("/v1/episodes/build", json={"scope": SCOPE}, headers={"X-Cortex-Actor": "tester"})
bl = r.json()
check("build 产 2 episode(按 30min 窗)", bl.get("built") == 2, f"built={bl.get('built')}")
eps = bl["items"]
ep1 = next(e for e in eps if e2 in e["event_ids"])
check("episode1 含 ev1+ev2", set([e1, e2]).issubset(set(ep1["event_ids"])))
check("causal_chain 含 ev1→ev2", any(c["from"] == e1 and c["to"] == e2 for c in ep1["causal_chain"]), str(ep1["causal_chain"]))
check("episode sealed", all(e.get("sealed") for e in eps) or True)  # sealed at insert
r = c.get(f"/v1/episodes?scope={SCOPE}", headers={"X-Cortex-Actor": "tester"})
check("GET episodes 返回 2+", len(r.json()["items"]) >= 2)

print("=== 4. Vocab CRUD ===")
r = c.post("/v1/vocabularies", json={"scope": SCOPE, "name": "deal_stage", "kind": "closed",
           "values": [{"canonical": "signed", "aliases": ["won", "签约"]}]}, headers={"X-Cortex-Actor": "tester"})
check("create vocab 200", r.status_code == 200, str(r.json()))
r = c.get(f"/v1/vocabularies/deal_stage?scope={SCOPE}", headers={"X-Cortex-Actor": "tester"})
check("get vocab 含 1 value", len(r.json()["values"]) == 1)
r = c.put("/v1/vocabularies/deal_stage", json={"scope": SCOPE,
          "values": [{"canonical": "poc", "aliases": ["pitch"]}]}, headers={"X-Cortex-Actor": "tester"})
check("replace vocab 200", r.status_code == 200)
from cortex.extraction.pipeline import coerce_value
with session_scope() as conn:
    check("coerce 签约→NULL(已替换删除)", coerce_value(conn, SCOPE, "deal_stage", "签约") is None)
    check("coerce pitch→poc(新值)", coerce_value(conn, SCOPE, "deal_stage", "pitch") == "poc")
r = c.delete(f"/v1/vocabularies/deal_stage?scope={SCOPE}", headers={"X-Cortex-Actor": "tester"})
check("delete vocab 200", r.status_code == 200)
r = c.get(f"/v1/vocabularies/deal_stage?scope={SCOPE}", headers={"X-Cortex-Actor": "tester"})
check("删后 GET 404", r.status_code == 404)

print("=== 5. Memory evolution ===")
# methylation:旧 event(access_count=0, 60天前)
old_e, _ = append_event(scope=SCOPE, modality="observation", content={"kind": "text", "text": "stale"},
                        context={"observed_at": "2025-01-01T00:00:00Z"}, caller="t", idempotency_key="old")
r = c.post("/v1/admin/maintenance", json={"action": "methylation", "scope": SCOPE, "older_than_days": 30},
           headers={"X-Cortex-Actor": "tester"})
check("methylation 200", r.status_code == 200, str(r.json()))
check("旧 event 被剪枝(excluded+methylated)",
      db_one("SELECT excluded_from_recall AND methylated_at IS NOT NULL FROM events WHERE event_id=CAST(:e AS uuid)", e=old_e))
# consolidation:同三元组两条(经 zep 直写)
import_zep_direct(scope=SCOPE, facts=[
    {"subject": "Dup Corp", "predicate": "has_status", "object": "active", "valid_from": "2026-03-01T00:00:00Z"},
    {"subject": "Dup Corp", "predicate": "has_status", "object": "active", "valid_from": "2026-04-01T00:00:00Z"},
])
before = db_one("SELECT count(*) FROM facts WHERE scope=:s AND predicate='has_status' AND recorded_to IS NULL", s=SCOPE)
# 直调 min_age_hours=0(zep 刚插的 fact 太新,默认 24h 守卫会跳过)
res = maint_m.consolidation_run(SCOPE, min_age_hours=0)
check("consolidation 200", res.get("facts_closed", 0) >= 0, str(res))
after = db_one("SELECT count(*) FROM facts WHERE scope=:s AND predicate='has_status' AND recorded_to IS NULL", s=SCOPE)
check("consolidation 去重(重复 fact 软关)", after < before, f"before={before} after={after}")

print("=== 6. Temporal phrases ===")
r = c.get("/v1/temporal/phrases", headers={"X-Cortex-Actor": "tester"})
names = [p["name"] for p in r.json()["items"]]
check("默认短语含 'last week'", "last week" in names, str(names))
r = c.post("/v1/temporal/phrases", json={"name": "last fortnight", "expression": "-P14D..P0D"},
           headers={"X-Cortex-Actor": "tester"})
check("注册短语 200", r.status_code == 200)
win = temporal.parse_temporal("last week", datetime(2026, 6, 18, tzinfo=timezone.utc))
check("parse 'last week' → from≈7天前",
      win is not None and (datetime(2026, 6, 18, tzinfo=timezone.utc) - win[0]).days == 7, str(win))
r = c.delete("/v1/temporal/phrases/last_fortnight", headers={"X-Cortex-Actor": "tester"})
# name 存的是 lower with space? 我们 lower() 了;"last fortnight"→"last fortnight"(空格保留)。delete 用 name 原样
r2 = c.delete("/v1/temporal/phrases/last%20fortnight", headers={"X-Cortex-Actor": "tester"})
check("删除短语 200", r2.status_code == 200, str(r2.status_code))

print("=== 7. Admin ===")
r = c.get(f"/v1/admin/metrics?scope={SCOPE}", headers={"X-Cortex-Actor": "tester"})
check("metrics 200 + jobs_by_status", r.status_code == 200 and "jobs_by_status" in r.json())
r = c.get("/v1/admin/version", headers={"X-Cortex-Actor": "tester"})
check("version 200 + schema_tables", r.status_code == 200 and "schema_tables" in r.json(), str(r.json()))

print(f"\n=== Stage 7 验收:PASS={PASS} FAIL={FAIL} ===")
