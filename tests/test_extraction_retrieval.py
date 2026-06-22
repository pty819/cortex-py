"""extraction + retrieval + episodes 功能测试。"""
import pytest
from cortex.core import append_event
from cortex.extraction.pipeline import extract_event, _is_single_value, coerce_value
from cortex.retrieval import recall
from cortex.db import session_scope
from cortex.maintenance import seed_diagnosis_vocab
from cortex.episodes import create_case, update_case, list_cases, search_cases
from sqlalchemy import text


def test_triple_direct_write(test_scope):
    """triple 直写不经 LLM,直接建 entity + fact。"""
    seed_diagnosis_vocab(test_scope)
    eid, _ = append_event(scope=test_scope, modality="imported",
                          content={"kind": "triple", "triple": {
                              "subject": {"name": "TestFault"},
                              "predicate": "caused_by",
                              "object": {"name": "TestRootCause"}}},
                          context={"observed_at": "2026-01-01T00:00:00Z", "intent": "diagnosis"},
                          caller="test", idempotency_key="triple-test-1")
    res = extract_event(eid)
    assert res["facts_extracted"] == 1
    assert res["model"] == "triple-direct"


def test_multi_value_predicate_no_supersede(test_scope):
    """多值谓词(caused_by)不超替:两次入库同 subject+predicate 不同 object → 两条 fact。"""
    seed_diagnosis_vocab(test_scope)
    for i, obj in enumerate(["CauseA", "CauseB"]):
        eid, _ = append_event(scope=test_scope, modality="imported",
                              content={"kind": "triple", "triple": {
                                  "subject": {"name": "MultiFault"},
                                  "predicate": "caused_by",
                                  "object": {"name": obj}}},
                              context={"observed_at": f"2026-01-0{i+1}T00:00:00Z"},
                              caller="test", idempotency_key=f"multi-{i}")
        extract_event(eid)
    with session_scope() as c:
        n = c.execute(text("""SELECT count(*) FROM facts WHERE scope=:s AND predicate='caused_by'
                            AND valid_to IS NULL AND recorded_to IS NULL"""), {"s": test_scope}).scalar()
    assert n == 2  # 两条共存,不超替


def test_single_value_predicate_supersede(test_scope):
    """单值谓词(has_status)超替:两次入库同 subject+predicate → 旧 fact valid_to 闭合。"""
    seed_diagnosis_vocab(test_scope)
    for i, val in enumerate(["active", "closed"]):
        eid, _ = append_event(scope=test_scope, modality="imported",
                              content={"kind": "triple", "triple": {
                                  "subject": {"name": "StatusEntity"},
                                  "predicate": "has_status",
                                  "object": {"name": val}}},
                              context={"observed_at": f"2026-01-0{i+1}T00:00:00Z"},
                              caller="test", idempotency_key=f"single-{i}")
        extract_event(eid)
    with session_scope() as c:
        n_live = c.execute(text("""SELECT count(*) FROM facts WHERE scope=:s AND predicate='has_status'
                                 AND valid_to IS NULL AND recorded_to IS NULL"""), {"s": test_scope}).scalar()
        n_closed = c.execute(text("""SELECT count(*) FROM facts WHERE scope=:s AND predicate='has_status'
                                   AND valid_to IS NOT NULL"""), {"s": test_scope}).scalar()
    assert n_live == 1  # 只有最新一条活着
    assert n_closed == 1  # 旧的被闭合


def test_is_single_value_queries_db(test_scope):
    """_is_single_value 优先查 DB vocabularies.cardinality。"""
    seed_diagnosis_vocab(test_scope)
    with session_scope() as conn:
        assert _is_single_value(conn, test_scope, "has_status") is True   # DB 标 single
        assert _is_single_value(conn, test_scope, "caused_by") is False    # DB 标 multi
        assert _is_single_value(conn, test_scope, "deal_stage") is True    # DB 标 single
        assert _is_single_value(conn, test_scope, "has_component") is False  # DB 标 multi


def test_recall_returns_pack(test_scope):
    """recall 返回 StratifiedPack 结构。"""
    pack = recall(scope=test_scope, query="test query")
    assert "pack_id" in pack
    assert "layers" in pack
    assert "facts" in pack["layers"]
    assert "diagnostics" in pack


def test_recall_as_of_filters(test_scope):
    """recall with as_of 过滤掉未来 facts(双时态)。"""
    seed_diagnosis_vocab(test_scope)
    eid, _ = append_event(scope=test_scope, modality="imported",
                          content={"kind": "triple", "triple": {
                              "subject": {"name": "TemporalEntity"},
                              "predicate": "has_status",
                              "object": {"name": "active"}}},
                          context={"observed_at": "2026-06-01T00:00:00Z"},
                          caller="test", idempotency_key="temp-1")
    extract_event(eid)
    # as_of 在 fact valid_from 之前 → 不应召回
    pack = recall(scope=test_scope, query="TemporalEntity", as_of="2026-01-01T00:00:00Z")
    assert len(pack["layers"]["facts"]) == 0
    # as_of 在 fact valid_from 之后 → 应召回
    pack2 = recall(scope=test_scope, query="TemporalEntity", as_of="2026-12-01T00:00:00Z")
    assert len(pack2["layers"]["facts"]) >= 1


def test_case_search(test_scope):
    """case 搜索:create + search by root_cause。"""
    create_case(scope=test_scope, title="真空泄漏", equipment="PM-1")
    results = search_cases(test_scope, "真空")
    assert len(results) >= 1
    assert any(r["title"] == "真空泄漏" for r in results)
