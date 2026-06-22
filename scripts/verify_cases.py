"""验证 Case CRUD: create → update → add_event → get → list → search。"""
from __future__ import annotations

from fastapi.testclient import TestClient
from cortex.api.app import app
from cortex.db import init_schema
from cortex.core import append_event

c = TestClient(app)
SCOPE = "mech:case-test/line:A/user:diag"
PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1; print(f"  ✓ {name} {detail}")
    else:
        FAIL += 1; print(f"  ✗ {name} {detail}")


print("=== reset ===")
init_schema(drop=True)

print("=== 1. create_case ===")
r = c.post("/v1/cases", json={"scope": SCOPE, "title": "刻蚀速率漂移",
       "equipment": "PM-3", "lot": "LOT-20260615-001", "recipe": "ETCH_MAIN_V2"})
ep_id = r.json().get("episode_id")
check("create_case 200", r.status_code == 200, f"episode_id={ep_id[:8] if ep_id else 'None'}")
check("case has equipment", r.json().get("status") == "open")

print("=== 2. update_case (phase + root_cause) ===")
r = c.patch(f"/v1/cases/{ep_id}", json={"phase": "root_cause", "root_cause": "V-3阀门密封老化",
                                        "status": "investigating"})
check("update_case 200", r.status_code == 200, str(r.json()))
r = c.get(f"/v1/cases/{ep_id}")
check("phase updated", r.json().get("phase") == "root_cause")
check("root_cause updated", r.json().get("root_cause") == "V-3阀门密封老化")
check("status updated", r.json().get("status") == "investigating")

print("=== 3. add_event_to_case ===")
eid, _ = append_event(scope=SCOPE, modality="conversation",
                      content={"kind": "message", "role": "user", "text": "排查发现V-3阀门泄漏"},
                      context={"observed_at": "2026-06-15T05:00:00Z"},
                      caller="test", idempotency_key="case-test-ev1")
r = c.post(f"/v1/cases/{ep_id}/events", json={"event_id": eid})
check("add_event 200", r.status_code == 200, str(r.json()))

print("=== 4. get_case (含 events + facts + beliefs) ===")
r = c.get(f"/v1/cases/{ep_id}")
case = r.json()
check("get_case 200", r.status_code == 200)
check("case has events", len(case.get("events", [])) >= 1, f"events={len(case.get('events', []))}")
check("case has event_ids", eid in (case.get("event_ids") or []))
check("case has facts list", "facts" in case)
check("case has beliefs list", "beliefs" in case)

print("=== 5. list_cases (按 status 过滤) ===")
r = c.get(f"/v1/cases?scope={SCOPE}&status=investigating")
check("list by status", len(r.json()["items"]) >= 1, f"items={len(r.json()['items'])}")
r = c.get(f"/v1/cases?scope={SCOPE}&status=resolved")
check("list resolved (empty)", len(r.json()["items"]) == 0)

print("=== 6. search_cases ===")
r = c.post("/v1/cases/search", json={"scope": SCOPE, "query": "V-3阀门"})
check("search by root_cause", len(r.json()["items"]) >= 1, f"items={len(r.json()['items'])}")
r = c.post("/v1/cases/search", json={"scope": SCOPE, "query": "不存在的关键词"})
check("search no match", len(r.json()["items"]) == 0)

print("=== 7. update to resolved ===")
r = c.patch(f"/v1/cases/{ep_id}", json={"status": "resolved", "resolution": "更换V-3密封圈",
                                        "phase": "regression"})
check("update to resolved", r.status_code == 200)
r = c.get(f"/v1/cases?scope={SCOPE}&status=resolved")
check("list resolved now has 1", len(r.json()["items"]) == 1)

print("=== 8. invalid phase/status ===")
r = c.patch(f"/v1/cases/{ep_id}", json={"phase": "invalid_phase"})
check("invalid phase rejected", r.status_code == 422)

print(f"\n=== Case 验收:PASS={PASS} FAIL={FAIL} ===")
