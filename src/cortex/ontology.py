"""诊断谓词本体的单一真相源。Prompt、closed vocab、图准入和 backfill 共用。"""

STRUCTURAL_PREDICATES = {
    "part_of", "has_component", "installed_on", "located_in", "monitored_by",
    "controlled_by", "regulates", "configured_as", "depends_on",
}

CAUSAL_PREDICATES = {
    "caused_by", "led_to", "cascades_to", "affects", "triggers", "contributes_to",
    "correlates_with", "suggests", "symptom_of", "has_symptom",
}

DIAGNOSTIC_PREDICATES = {
    "detected_by", "investigates", "investigated_by", "checked", "found", "normal",
    "ruled_out", "no_correlation", "supports", "contradicts", "refines_to",
    "alternative_to", "confirmed_by", "repaired_by", "observed_by", "references",
    "preceded_by", "drifts_from", "measured_as", "deviates_from", "feedback_to",
}

OPPOSING_PREDICATES = {"ruled_out"}
RELATIONAL_EXCLUSION_PREDICATES = {"no_correlation", "contradicts"}
GRAPH_EXCLUDED_PREDICATES = OPPOSING_PREDICATES | RELATIONAL_EXCLUSION_PREDICATES

STATE_PREDICATES = {"has_status", "deal_stage"}
DIAGNOSIS_PREDICATE_NAMES = (
    STRUCTURAL_PREDICATES | CAUSAL_PREDICATES | DIAGNOSTIC_PREDICATES | STATE_PREDICATES
)

PREDICATE_CARDINALITY = {
    predicate: ("single" if predicate in STATE_PREDICATES else "multi")
    for predicate in DIAGNOSIS_PREDICATE_NAMES
}


def graph_eligible(predicate: str, polarity: str, assertion_status: str) -> bool:
    if polarity != "positive" or predicate in GRAPH_EXCLUDED_PREDICATES:
        return False
    if predicate in CAUSAL_PREDICATES:
        return assertion_status == "confirmed"
    return assertion_status in {"observed", "confirmed"}
