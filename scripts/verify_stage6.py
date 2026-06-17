"""Stage 6 验收:bulk 50 + jsonl scope_template + zep 直写 + export 回灌。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import text

from cortex.api.app import app
from cortex.db import init_schema, session_scope

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

print("=== A. bulk: 灌 50 条 ===")
items = [{"modality": "conversation",
          "content": {"kind": "message", "role": "user", "text": f"user {i} mentioned Acme deal stage {i}"},
          "context": {"observed_at": "2026-06-18T10:00:00Z", "labels": ["acme"]},
          "idempotency_key": f"bulk-{i}"} for i in range(50)]
r = c.post("/v1/experience/bulk", json={"scope": SCOPE, "items": items, "ordering": "strict_temporal"},
           headers={"X-Cortex-Actor": "importer"})
rb = r.json()
check("bulk HTTP 200", r.status_code == 200, f"status={r.status_code}")
check("bulk accepted=50", rb.get("accepted") == 50, f"accepted={rb.get('accepted')} failed={rb.get('failed')}")
check("import_jobs row accepted=50 completed", db_one(
    "SELECT accepted||':'||status FROM import_jobs WHERE import_id=CAST(:i AS uuid)", i=rb["import_id"]) == "50:completed")
check("events=50", db_one("SELECT count(*) FROM events WHERE scope=:s", s=SCOPE) == 50)
check("extract jobs enqueued=50", db_one(
    "SELECT count(*) FROM jobs WHERE scope=:s AND job_type='extract'", s=SCOPE) == 50)

print("=== B. jsonl importer + scope_template ===")
lines = "\n".join([
    json.dumps({"user": "alice", "modality": "conversation",
                "content": {"kind": "message", "role": "user", "text": "alice note about Q3"},
                "context": {"observed_at": "2026-06-18T11:00:00Z"}, "idempotency_key": "jl-1"}),
    json.dumps({"user": "bob", "modality": "conversation",
                "content": {"kind": "message", "role": "user", "text": "bob note about infra"},
                "context": {"observed_at": "2026-06-18T11:01:00Z"}, "idempotency_key": "jl-2"}),
])
r = c.post("/v1/import/jsonl", json={"scope": "org:acme/fallback",
           "scope_template": "org:acme/user:{user}", "lines": lines},
           headers={"X-Cortex-Actor": "importer"})
rb = r.json()
check("jsonl accepted=2", rb.get("accepted") == 2, f"accepted={rb.get('accepted')}")
n_alice = db_one("SELECT count(*) FROM events WHERE scope=:s", s="org:acme/user:alice")
n_bob = db_one("SELECT count(*) FROM events WHERE scope=:s", s="org:acme/user:bob")
check("scope_template 填对(alice/bob 各 1)", n_alice == 1 and n_bob == 1, f"alice={n_alice} bob={n_bob}")

print("=== C. zep 直写双时态 facts(跳过抽取)===")
r = c.post("/v1/import/zep", json={"scope": SCOPE, "facts": [
    {"subject": "Zep Corp", "predicate": "renewed_arr", "object": "$300k",
     "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-03-01T00:00:00Z", "confidence": 0.9},
    {"subject": "Zep Corp", "predicate": "has_status", "object": "active",
     "valid_from": "2026-02-01T00:00:00Z"},
]}, headers={"X-Cortex-Actor": "importer"})
rb = r.json()
check("zep accepted=2", rb.get("accepted") == 2, f"accepted={rb.get('accepted')}")
check("zep facts 直写(无 extract job 产出,因不走抽取)",
      db_one("SELECT count(*) FROM facts WHERE scope=:s AND extraction_model='zep-import'", s=SCOPE) == 2)
# 第一条有 valid_to,验证双时态保留
closed = db_one("SELECT count(*) FROM facts WHERE scope=:s AND predicate='renewed_arr' AND valid_to IS NOT NULL", s=SCOPE)
check("zep valid_to 保留", closed == 1, f"closed={closed}")

print("=== D. export → 回灌 ===")
r = c.post("/v1/export", json={"scope": "org:acme/user:alice"}, headers={"X-Cortex-Actor": "importer"})
exp = r.json()
check("export 返回 JSONL", r.status_code == 200 and "data" in exp, f"bytes={exp.get('bytes')}")
n_ev_exp = sum(1 for ln in exp["data"].splitlines() if ln.strip() and json.loads(ln)["type"] == "event")
check(f"export 含 {n_ev_exp} event 行", n_ev_exp >= 1)
# 回灌:把 event 行作为 jsonl 导入到新 scope
ev_lines = "\n".join(ln for ln in exp["data"].splitlines()
                     if ln.strip() and json.loads(ln)["type"] == "event")
r2 = c.post("/v1/import/jsonl", json={"scope": "org:restored/user:alice", "lines": ev_lines},
            headers={"X-Cortex-Actor": "importer"})
check("回灌 import 成功", r2.status_code == 200 and r2.json().get("accepted", 0) >= 1,
      f"accepted={r2.json().get('accepted')}")
n_restored = db_one("SELECT count(*) FROM events WHERE scope=:s", s="org:restored/user:alice")
check(f"回灌后 events={n_restored}(应 == 原导出 event 数 {n_ev_exp})", n_restored == n_ev_exp)

print(f"\n=== Stage 6 验收:PASS={PASS} FAIL={FAIL} ===")
