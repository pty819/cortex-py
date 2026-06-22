from types import SimpleNamespace
import os

import pytest
from sqlalchemy import text

from cortex.core import append_event, fail_job
from cortex.db import init_schema, session_scope
from cortex.episodes import add_event_to_case, create_case, get_case
from cortex.extraction.pipeline import ExtractionConfigurationError, extract_event
from cortex.maintenance import seed_diagnosis_vocab
from cortex.retrieval.pipeline import _graph_eligible_sql, _temporal_clause


def test_bitemporal_recorded_interval_has_upper_bound():
    clause = _temporal_clause("2026-01-01T00:00:00Z", True)
    assert "recorded_from <=" in clause
    assert "recorded_to IS NULL" in clause
    assert "< recorded_to" in clause


def test_graph_eligibility_excludes_hypotheses_and_negation():
    clause = _graph_eligible_sql("f")
    assert "polarity='positive'" in clause.replace(" ", "")
    assert "assertion_status='confirmed'" in clause.replace(" ", "")


def test_default_schema_drop_refused(monkeypatch):
    monkeypatch.delenv("CORTEX_DB_SCHEMA_OVERRIDE", raising=False)
    with pytest.raises(PermissionError):
        init_schema(drop=True)


def test_runtime_connections_follow_configured_schema(monkeypatch):
    from types import SimpleNamespace
    from cortex import db

    monkeypatch.delenv("CORTEX_DB_SCHEMA_OVERRIDE", raising=False)
    monkeypatch.setattr(db, "load_config",
                        lambda: SimpleNamespace(database=SimpleNamespace(schema_="cortex_custom")))
    assert db._runtime_schema_name() == "cortex_custom"


def test_cli_reset_rejects_default_schema_without_traceback(monkeypatch, capsys):
    from cortex.cli import main

    monkeypatch.delenv("CORTEX_DB_SCHEMA_OVERRIDE", raising=False)
    assert main(["db", "reset"]) == 2
    assert "cortex_test_<name>" in capsys.readouterr().err


def test_config_failure_is_terminal(test_scope):
    with session_scope() as conn:
        jid = conn.execute(text("""INSERT INTO jobs(job_type,scope,status,attempts,max_attempts)
                                  VALUES('extract',:s,'running',1,3) RETURNING job_id::text"""), {"s": test_scope}).scalar()
        fail_job(conn, jid, "missing extraction key", terminal=True, error_kind="config_error")
    with session_scope() as conn:
        row = conn.execute(text("SELECT status, result->>'error_kind' FROM jobs WHERE job_id=CAST(:j AS uuid)"), {"j": jid}).one()
    assert row == ("failed", "config_error")


def test_missing_key_does_not_emit_extracted_lifecycle(test_scope, monkeypatch):
    monkeypatch.delenv("CORTEX_ALLOW_MOCK_EXTRACTION", raising=False)
    monkeypatch.setattr("cortex.extraction.pipeline.services.llm_configured", lambda tier: False)
    eid, _ = append_event(scope=test_scope, modality="conversation",
                          content={"kind": "message", "text": "PM1 alarm"}, context={},
                          caller="test", idempotency_key="missing-key")
    with pytest.raises(ExtractionConfigurationError):
        extract_event(eid)
    with session_scope() as conn:
        kinds = conn.execute(text("SELECT kind FROM lifecycle_events WHERE event_id=CAST(:e AS uuid) ORDER BY ts"),
                             {"e": eid}).scalars().all()
    assert kinds == ["captured"]


def test_case_beliefs_are_evidence_scoped(test_scope):
    seed_diagnosis_vocab(test_scope)
    case = create_case(scope=test_scope, title="PM1 incident")
    event_ids = []
    for idx, subject in enumerate(("PM1", "PM2")):
        eid, _ = append_event(scope=test_scope, modality="imported", content={"kind": "triple", "triple": {
            "subject": {"name": subject}, "predicate": "has_status", "object": {"name": "down"}}},
            context={"observed_at": f"2026-01-0{idx + 1}T00:00:00Z", "intent": "diagnosis"},
            caller="test", idempotency_key=f"case-belief-{idx}")
        extract_event(eid)
        event_ids.append(eid)
    add_event_to_case(case["episode_id"], event_ids[0])
    loaded = get_case(case["episode_id"])
    assert len(loaded["beliefs"]) == 1
    assert loaded["beliefs"][0]["claim"].startswith("PM1 has")


def test_case_preserves_direct_fact_after_global_supersession(test_scope):
    seed_diagnosis_vocab(test_scope)
    case = create_case(scope=test_scope, title="state history")
    ids = []
    for idx, status in enumerate(("idle", "running")):
        eid, _ = append_event(scope=test_scope, modality="imported", content={"kind": "triple", "triple": {
            "subject": {"name": "PM1"}, "predicate": "has_status", "object": {"name": status}}},
            context={"observed_at": f"2026-01-0{idx + 1}T00:00:00Z", "intent": "diagnosis"},
            caller="test", idempotency_key=f"case-history-{idx}")
        extract_event(eid)
        ids.append(eid)
    add_event_to_case(case["episode_id"], ids[0])
    loaded = get_case(case["episode_id"])
    assert any(f["object_name"] == "idle" for f in loaded["facts"])
    assert any(f["valid_to"] is not None or f["recorded_to"] is not None for f in loaded["facts"])


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_static_llm_http_error_is_terminal(monkeypatch, status_code):
    from cortex.extraction.pipeline import _llm_extract

    class StaticConfigError(Exception):
        pass

    error = StaticConfigError("invalid model" if status_code == 400 else "bad static config")
    error.status_code = status_code

    monkeypatch.setattr("cortex.extraction.pipeline.services.llm_chat",
                        lambda *args, **kwargs: (_ for _ in ()).throw(error))
    with pytest.raises(ExtractionConfigurationError):
        _llm_extract("PM1 alarm", is_diagnosis=True)


def test_unsupported_response_format_400_falls_back(monkeypatch):
    from cortex.extraction.pipeline import _llm_extract

    class UnsupportedFormat(Exception):
        status_code = 400

    calls = []

    def fake_chat(*_args, **kwargs):
        calls.append(kwargs.get("response_format"))
        if len(calls) == 1:
            raise UnsupportedFormat("response_format json_schema is unsupported")
        return '{"entities": [], "facts": []}'

    monkeypatch.setattr("cortex.extraction.pipeline.services.llm_chat", fake_chat)
    result = _llm_extract("PM1 alarm", is_diagnosis=True)
    assert len(calls) == 2
    assert result["_mode"] == "json_object"


def test_transient_llm_failure_emits_no_success_lifecycle(test_scope, monkeypatch):
    monkeypatch.setattr("cortex.extraction.pipeline.services.llm_configured", lambda tier: True)
    monkeypatch.setattr("cortex.extraction.pipeline._llm_extract",
                        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("upstream timeout")))
    eid, _ = append_event(scope=test_scope, modality="conversation",
                          content={"kind": "message", "text": "PM1 alarm"}, context={},
                          caller="test", idempotency_key="transient-llm")
    with pytest.raises(RuntimeError, match="LLM extraction failed"):
        extract_event(eid)
    with session_scope() as conn:
        kinds = conn.execute(text("SELECT kind FROM lifecycle_events WHERE event_id=CAST(:e AS uuid) ORDER BY ts"),
                             {"e": eid}).scalars().all()
    assert kinds == ["captured"]


def test_worker_terminal_config_failure_emits_failed_lifecycle(test_scope):
    from cortex.worker.runner import _handle_job_failure

    eid, _ = append_event(scope=test_scope, modality="conversation",
                          content={"kind": "message", "text": "PM1 alarm"}, context={},
                          caller="test", idempotency_key="worker-config")
    with session_scope() as conn:
        jid = conn.execute(text("""INSERT INTO jobs(job_type,scope,event_id,status,attempts,max_attempts)
                                  VALUES('extract',:s,CAST(:e AS uuid),'running',1,3) RETURNING job_id::text"""),
                           {"s": test_scope, "e": eid}).scalar()
        _handle_job_failure(conn, {"job_id": jid, "scope": test_scope, "event_id": eid},
                            ExtractionConfigurationError("bad key"), 4)
    with session_scope() as conn:
        status = conn.execute(text("SELECT status FROM jobs WHERE job_id=CAST(:j AS uuid)"), {"j": jid}).scalar()
        embed_status = conn.execute(text("SELECT embed_status FROM events WHERE event_id=CAST(:e AS uuid)"), {"e": eid}).scalar()
        failed = conn.execute(text("SELECT payload->>'error_kind' FROM lifecycle_events WHERE event_id=CAST(:e AS uuid) AND kind='failed'"), {"e": eid}).scalar()
    assert (status, embed_status, failed) == ("failed", "failed", "config_error")


def test_worker_transient_failure_requeues_then_dead_letters_at_bound(test_scope):
    from cortex.worker.runner import _handle_job_failure

    with session_scope() as conn:
        jid = conn.execute(text("""INSERT INTO jobs(job_type,scope,status,attempts,max_attempts)
                                  VALUES('extract',:s,'running',1,2) RETURNING job_id::text"""),
                           {"s": test_scope}).scalar()
        _handle_job_failure(conn, {"job_id": jid, "scope": test_scope}, TimeoutError("slow"), 2)
    with session_scope() as conn:
        first = conn.execute(text("SELECT status, result FROM jobs WHERE job_id=CAST(:j AS uuid)"),
                             {"j": jid}).one()
        conn.execute(text("UPDATE jobs SET status='running', attempts=2 WHERE job_id=CAST(:j AS uuid)"),
                     {"j": jid})
        _handle_job_failure(conn, {"job_id": jid, "scope": test_scope}, TimeoutError("slow"), 2)
    with session_scope() as conn:
        final = conn.execute(text("SELECT status, result->>'error_kind' FROM jobs WHERE job_id=CAST(:j AS uuid)"),
                             {"j": jid}).one()
    assert first == ("queued", None)
    assert final == ("failed", "processing_error")


def test_smoke_explicitly_enables_mock_only_for_local_extraction(monkeypatch):
    from cortex import smoke

    seen = []
    monkeypatch.delenv("CORTEX_ALLOW_MOCK_EXTRACTION", raising=False)
    monkeypatch.setattr(smoke, "llm_configured", lambda _tier: False)
    monkeypatch.setattr(smoke, "init_schema", lambda **_kwargs: None)
    monkeypatch.setattr(smoke, "append_event", lambda **_kwargs: ("event", 1))
    monkeypatch.setattr(smoke, "enqueue_job", lambda **_kwargs: "job")

    def fake_extract(_event_id):
        seen.append(os.environ.get("CORTEX_ALLOW_MOCK_EXTRACTION"))
        raise RuntimeError("stop after extraction probe")

    monkeypatch.setattr(smoke, "extract_event", fake_extract)
    with pytest.raises(RuntimeError, match="extraction probe"):
        smoke.run_smoke()
    assert seen == ["true"]
    assert "CORTEX_ALLOW_MOCK_EXTRACTION" not in os.environ
