"""API 端点测试(使用 FastAPI TestClient)。"""
import pytest
from fastapi.testclient import TestClient
from cortex.api.app import app
from cortex.db import init_schema


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    """health 端点返回 ok。"""
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_experience_endpoint(client, test_scope):
    """POST /experience 入库 + 返回 event_id。"""
    r = client.post("/v1/experience", json={
        "scope": test_scope, "modality": "conversation",
        "content": {"kind": "message", "role": "user", "text": "API test"},
        "context": {"observed_at": "2026-01-01T00:00:00Z"},
        "idempotency_key": "api-test-1",
    }, headers={"X-Cortex-Actor": "test"})
    assert r.status_code in (200, 202)
    assert "event_id" in r.json()


def test_entities_endpoint(client, test_scope):
    """GET /entities 返回 items 列表。"""
    r = client.get("/v1/entities", params={"scope": test_scope},
                   headers={"X-Cortex-Actor": "test"})
    assert r.status_code == 200
    assert "items" in r.json()


def test_facts_endpoint(client, test_scope):
    """GET /facts 返回 items 列表。"""
    r = client.get("/v1/facts", params={"scope": test_scope},
                   headers={"X-Cortex-Actor": "test"})
    assert r.status_code == 200
    assert "items" in r.json()


def test_scopes_list(client, test_scope):
    """GET /scopes/list 返回 scope 列表(含测试 scope)。"""
    r = client.get("/v1/scopes/list", headers={"X-Cortex-Actor": "test"})
    assert r.status_code == 200
    assert "items" in r.json()


def test_case_crud(client, test_scope):
    """Case CRUD: create → get → update → list。"""
    # create
    r = client.post("/v1/cases", json={"scope": test_scope, "title": "Test Case",
                  "equipment": "PM-1"}, headers={"X-Cortex-Actor": "test"})
    assert r.status_code == 200
    ep_id = r.json()["episode_id"]
    # get
    r = client.get(f"/v1/cases/{ep_id}", headers={"X-Cortex-Actor": "test"})
    assert r.status_code == 200
    assert r.json()["title"] == "Test Case"
    # update
    r = client.patch(f"/v1/cases/{ep_id}", json={"status": "investigating", "phase": "investigation"},
                     headers={"X-Cortex-Actor": "test"})
    assert r.status_code == 200
    # list
    r = client.get("/v1/cases", params={"scope": test_scope, "status": "investigating"},
                   headers={"X-Cortex-Actor": "test"})
    assert r.status_code == 200
    assert len(r.json()["items"]) >= 1


def test_recall_endpoint(client, test_scope):
    """POST /recall 返回 pack 结构。"""
    r = client.post("/v1/recall", json={"scope": test_scope, "query": "test query"},
                    headers={"X-Cortex-Actor": "test"})
    assert r.status_code == 200
    assert "pack_id" in r.json()
    assert "layers" in r.json()
