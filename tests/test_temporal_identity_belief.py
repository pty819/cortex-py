import json
from sqlalchemy import text

from cortex.core import append_event
from cortex.db import session_scope
from cortex.extraction.pipeline import canonical_identity_context, context_key, extract_event
from cortex.maintenance import seed_diagnosis_vocab
from cortex.retrieval.pipeline import _temporal_clause


def _write(scope, key, value, at):
    eid, _ = append_event(
        scope=scope, modality="imported",
        content={"kind": "triple", "triple": {
            "subject": {"name": "PM1", "type": "equipment"}, "predicate": "has_status",
            "object": {"name": value},
        }},
        context={"observed_at": at, "intent": "diagnosis", "fab": " FAB 1 ",
                 "tool": "PM1", "module": " Etch "},
        caller="test", idempotency_key=key,
    )
    return extract_event(eid)


def test_late_arrival_versions_predecessor_interval(test_scope):
    seed_diagnosis_vocab(test_scope)
    _write(test_scope, "late-a", "alarm", "2026-01-03T00:00:00Z")
    _write(test_scope, "late-b", "idle", "2026-01-01T00:00:00Z")
    _write(test_scope, "late-c", "running", "2026-01-02T00:00:00Z")
    with session_scope() as conn:
        rows = conn.execute(text("""SELECT o.canonical_name, f.valid_from::date::text, f.valid_to::date::text
                                  FROM facts f JOIN entities o ON o.entity_id=f.object_entity_id
                                  WHERE f.scope=:s AND f.predicate='has_status' AND f.recorded_to IS NULL
                                  ORDER BY f.valid_from"""), {"s": test_scope}).all()
    assert rows == [("idle", "2026-01-01", "2026-01-02"),
                    ("running", "2026-01-02", "2026-01-03"),
                    ("alarm", "2026-01-03", None)]


def test_equal_valid_time_creates_recorded_correction(test_scope):
    seed_diagnosis_vocab(test_scope)
    _write(test_scope, "corr-a", "idle", "2026-01-01T00:00:00Z")
    _write(test_scope, "corr-b", "running", "2026-01-01T00:00:00Z")
    with session_scope() as conn:
        current = conn.execute(text("""SELECT o.canonical_name FROM facts f JOIN entities o ON o.entity_id=f.object_entity_id
                                      WHERE f.scope=:s AND f.predicate='has_status' AND f.recorded_to IS NULL"""), {"s": test_scope}).all()
        historic = conn.execute(text("SELECT count(*) FROM facts WHERE scope=:s AND predicate='has_status' AND recorded_to IS NOT NULL"), {"s": test_scope}).scalar()
    assert current == [("running",)]
    assert historic == 1


def test_context_key_golden_vectors():
    raw = {"fab": " FAB 1 ", "equipment": "ignored", "tool": "ＰＭ１", "module": "  Etch   Module ",
           "chamber": "", "recipe_rev": " R2 "}
    canonical = canonical_identity_context(raw)
    assert canonical == {"fab": "fab 1", "equipment": "pm1", "module": "etch module", "recipe_revision": "r2"}
    assert context_key(raw) == json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_contextual_entity_resolution_keeps_same_alias_separate(test_scope):
    seed_diagnosis_vocab(test_scope)
    _write(test_scope, "ctx-a", "idle", "2026-01-01T00:00:00Z")
    eid, _ = append_event(
        scope=test_scope, modality="imported",
        content={"kind": "triple", "triple": {
            "subject": {"name": "PM1", "type": "equipment"}, "predicate": "has_status",
            "object": {"name": "idle"},
        }},
        context={"observed_at": "2026-01-02T00:00:00Z", "intent": "diagnosis",
                 "fab": "FAB 2", "tool": "PM1"},
        caller="test", idempotency_key="ctx-b",
    )
    extract_event(eid)
    with session_scope() as conn:
        n = conn.execute(text("SELECT count(*) FROM entities WHERE scope=:s AND canonical_name='PM1'"), {"s": test_scope}).scalar()
    assert n == 2


def test_vector_similarity_cannot_merge_incompatible_equipment_context(test_scope, monkeypatch):
    seed_diagnosis_vocab(test_scope)
    monkeypatch.setattr("cortex.extraction.pipeline.services.embed_one", lambda text, **kwargs: [0.01] * 1024)
    for idx, (name, fab) in enumerate((("PM-1", "FAB 1"), ("PM1", "FAB 2"))):
        eid, _ = append_event(scope=test_scope, modality="imported", content={"kind": "triple", "triple": {
            "subject": {"name": name, "type": "equipment"}, "predicate": "has_status",
            "object": {"name": "idle"}}},
            context={"observed_at": f"2026-01-0{idx + 1}T00:00:00Z", "intent": "diagnosis",
                     "fab": fab, "tool": name}, caller="test", idempotency_key=f"vector-context-{idx}")
        extract_event(eid)
    with session_scope() as conn:
        rows = conn.execute(text("""SELECT canonical_name, context_key FROM entities
                                  WHERE scope=:s AND entity_type='equipment' ORDER BY canonical_name"""),
                            {"s": test_scope}).all()
    assert len(rows) == 2
    assert rows[0].context_key != rows[1].context_key


def test_untyped_legacy_entity_is_not_reused_for_typed_equipment(test_scope, monkeypatch):
    seed_diagnosis_vocab(test_scope)
    monkeypatch.setattr("cortex.extraction.pipeline.services.embed_one", lambda _text, **kwargs: [0.04] * 1024)
    for idx, etype in enumerate((None, "equipment")):
        eid, _ = append_event(scope=test_scope, modality="imported", content={"kind": "triple", "triple": {
            "subject": {"name": "PM-LEGACY", "type": etype}, "predicate": "has_status",
            "object": {"name": "idle"}}},
            context={"observed_at": f"2026-01-0{idx + 1}T00:00:00Z", "intent": "diagnosis",
                     "fab": "FAB 1", "tool": "PM-LEGACY"}, caller="test",
            idempotency_key=f"legacy-type-{idx}")
        extract_event(eid)
    with session_scope() as conn:
        types = conn.execute(text("SELECT entity_type FROM entities WHERE scope=:s AND canonical_name='PM-LEGACY' ORDER BY entity_type NULLS FIRST"),
                             {"s": test_scope}).scalars().all()
    assert types == [None, "equipment"]


def test_vector_lookup_filters_context_before_candidate_limit(test_scope, monkeypatch):
    seed_diagnosis_vocab(test_scope)
    monkeypatch.setattr("cortex.extraction.pipeline.services.embed_one", lambda _text, **kwargs: [0.06] * 1024)
    for idx in range(6):
        eid, _ = append_event(scope=test_scope, modality="imported", content={"kind": "triple", "triple": {
            "subject": {"name": f"Pump-{idx}", "type": "component"}, "predicate": "has_status",
            "object": {"name": "idle"}}},
            context={"observed_at": "2026-01-01T00:00:00Z", "intent": "diagnosis",
                     "fab": "FAB 2", "tool": "PM2", "chamber": f"C{idx}"},
            caller="test", idempotency_key=f"context-distractor-{idx}")
        extract_event(eid)
    for idx, name in enumerate(("VacuumPump", "Vac Pump")):
        eid, _ = append_event(scope=test_scope, modality="imported", content={"kind": "triple", "triple": {
            "subject": {"name": name, "type": "component"}, "predicate": "has_status",
            "object": {"name": "idle"}}},
            context={"observed_at": f"2026-01-0{idx + 2}T00:00:00Z", "intent": "diagnosis",
                     "fab": "FAB 1", "tool": "PM1", "chamber": "C1"},
            caller="test", idempotency_key=f"context-compatible-{idx}")
        extract_event(eid)
    with session_scope() as conn:
        compatible = conn.execute(text("""SELECT count(*) FROM entities WHERE scope=:s
                                         AND entity_type='component' AND context_key LIKE '%fab 1%'"""),
                                  {"s": test_scope}).scalar()
    assert compatible == 1


def test_belief_revision_closes_previous_recorded_version(test_scope):
    seed_diagnosis_vocab(test_scope)
    _write(test_scope, "belief-a", "idle", "2026-01-01T00:00:00Z")
    _write(test_scope, "belief-b", "running", "2026-01-02T00:00:00Z")
    with session_scope() as conn:
        current = conn.execute(text("SELECT claim, stance FROM beliefs WHERE scope=:s AND recorded_to IS NULL"),
                               {"s": test_scope}).all()
        history = conn.execute(text("SELECT count(*) FROM beliefs WHERE scope=:s AND recorded_to IS NOT NULL"),
                               {"s": test_scope}).scalar()
    assert len(current) == 1
    assert "1 supporting" in current[0].claim
    assert current[0].stance == "likely_true"
    assert history == 1


def test_negative_single_value_assertion_does_not_supersede_positive_state(test_scope):
    seed_diagnosis_vocab(test_scope)
    _write(test_scope, "state-positive", "running", "2026-01-01T00:00:00Z")
    eid, _ = append_event(scope=test_scope, modality="imported", content={"kind": "triple", "triple": {
        "subject": {"name": "PM1", "type": "equipment"}, "predicate": "has_status",
        "object": {"name": "down"}, "negation": True}},
        context={"observed_at": "2026-01-02T00:00:00Z", "intent": "diagnosis",
                 "fab": "FAB 1", "tool": "PM1", "module": "Etch"},
        caller="test", idempotency_key="state-negative")
    extract_event(eid)
    with session_scope() as conn:
        rows = conn.execute(text("""SELECT o.canonical_name, f.polarity, f.valid_to::text
                                  FROM facts f JOIN entities o ON o.entity_id=f.object_entity_id
                                  WHERE f.scope=:s AND f.predicate='has_status' AND f.recorded_to IS NULL
                                  ORDER BY f.polarity DESC"""), {"s": test_scope}).all()
    assert {row[0] for row in rows} == {"running", "down"}
    assert next(row for row in rows if row[0] == "running")[2] is None


def test_later_positive_state_does_not_close_negative_evidence(test_scope):
    seed_diagnosis_vocab(test_scope)
    eid, _ = append_event(scope=test_scope, modality="imported", content={"kind": "triple", "triple": {
        "subject": {"name": "PM1", "type": "equipment"}, "predicate": "has_status",
        "object": {"name": "down"}, "negation": True}},
        context={"observed_at": "2026-01-01T00:00:00Z", "intent": "diagnosis",
                 "fab": "FAB 1", "tool": "PM1"}, caller="test", idempotency_key="negative-first")
    extract_event(eid)
    _write(test_scope, "positive-later", "running", "2026-01-02T00:00:00Z")
    with session_scope() as conn:
        rows = conn.execute(text("""SELECT polarity, valid_to::text, recorded_to::text FROM facts
                                  WHERE scope=:s AND predicate='has_status' ORDER BY polarity"""),
                            {"s": test_scope}).all()
    negative = next(row for row in rows if row.polarity == "negative")
    assert negative.valid_to is None and negative.recorded_to is None


def test_identifier_prefix_prevents_same_number_entity_merge(test_scope, monkeypatch):
    seed_diagnosis_vocab(test_scope)
    monkeypatch.setattr("cortex.extraction.pipeline.services.embed_one", lambda _text, **kwargs: [0.08] * 1024)
    for idx, identifier in enumerate(("P-02", "T-02", "MFC-1", "VALVE-1")):
        eid, _ = append_event(scope=test_scope, modality="imported", content={"kind": "triple", "triple": {
            "subject": {"name": identifier, "type": "sensor"}, "predicate": "has_status",
            "object": {"name": "normal"}}},
            context={"observed_at": "2026-01-01T00:00:00Z", "intent": "diagnosis",
                     "fab": "FAB 1", "tool": "PM1", "chamber": "C1"}, caller="test",
            idempotency_key=f"identifier-family-{idx}")
        extract_event(eid)
    with session_scope() as conn:
        names = conn.execute(text("""SELECT canonical_name FROM entities WHERE scope=:s
                                   AND entity_type='sensor' ORDER BY canonical_name"""),
                             {"s": test_scope}).scalars().all()
    assert names == ["MFC-1", "P-02", "T-02", "VALVE-1"]


def test_late_arrival_does_not_backdate_current_belief(test_scope):
    seed_diagnosis_vocab(test_scope)
    _write(test_scope, "belief-tail", "running", "2026-02-03T00:00:00Z")
    _write(test_scope, "belief-late", "idle", "2026-02-01T00:00:00Z")
    with session_scope() as conn:
        valid_from = conn.execute(text("SELECT valid_from::date::text FROM beliefs WHERE scope=:s AND recorded_to IS NULL"),
                                  {"s": test_scope}).scalar()
    assert valid_from == "2026-02-03"


def test_recorded_as_of_selects_correction_visible_at_that_time(test_scope):
    seed_diagnosis_vocab(test_scope)
    _write(test_scope, "recorded-a", "idle", "2026-01-01T00:00:00Z")
    _write(test_scope, "recorded-b", "running", "2026-01-01T00:00:00Z")
    with session_scope() as conn:
        conn.execute(text("""UPDATE facts SET recorded_from='2026-03-01T00:00:00Z',
                             recorded_to='2026-03-02T00:00:00Z'
                             WHERE scope=:s AND predicate='has_status' AND recorded_to IS NOT NULL"""), {"s": test_scope})
        conn.execute(text("""UPDATE facts SET recorded_from='2026-03-02T00:00:00Z'
                             WHERE scope=:s AND predicate='has_status' AND recorded_to IS NULL"""), {"s": test_scope})
        clause = _temporal_clause("set", True)
        before = conn.execute(text(f"""SELECT o.canonical_name FROM facts f JOIN entities o ON o.entity_id=f.object_entity_id
                                      WHERE f.scope=:s AND f.{clause}"""),
                              {"s": test_scope, "ao": "2026-03-01T12:00:00Z"}).scalars().all()
        after = conn.execute(text(f"""SELECT o.canonical_name FROM facts f JOIN entities o ON o.entity_id=f.object_entity_id
                                     WHERE f.scope=:s AND f.{clause}"""),
                             {"s": test_scope, "ao": "2026-03-03T00:00:00Z"}).scalars().all()
    assert before == ["idle"]
    assert after == ["running"]


def test_contradictory_belief_formula_and_counts_are_deterministic(test_scope):
    seed_diagnosis_vocab(test_scope)
    _write(test_scope, "belief-support", "running", "2026-01-01T00:00:00Z")
    eid, _ = append_event(scope=test_scope, modality="imported", content={"kind": "triple", "triple": {
        "subject": {"name": "PM1", "type": "equipment"}, "predicate": "has_status",
        "object": {"name": "running"}, "negation": True, "confidence": 0.9}},
        context={"observed_at": "2026-01-02T00:00:00Z", "intent": "diagnosis",
                 "fab": "FAB 1", "tool": "PM1", "module": "Etch"},
        caller="test", idempotency_key="belief-oppose")
    extract_event(eid)
    with session_scope() as conn:
        belief = conn.execute(text("SELECT claim, stance, confidence FROM beliefs WHERE scope=:s AND recorded_to IS NULL"),
                              {"s": test_scope}).one()
    assert "1 supporting, 1 opposing, and 0 hypothesized" in belief.claim
    assert belief.stance == "uncertain"
    assert float(belief.confidence) == 0.1
