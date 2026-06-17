"""Pydantic API schemas(契约,前端按此构建)。3.9 兼容:用 Optional/List。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── experience envelope ────────────────────────────────────────────────────
class Content(BaseModel):
    kind: str = "message"            # message|text|json|blob_ref|triple
    role: Optional[str] = None       # user|assistant|tool|system(message)
    text: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    blob_id: Optional[str] = None


class Context(BaseModel):
    observed_at: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
    intent: Optional[str] = None
    preceded_by: List[str] = Field(default_factory=list)


class ExperienceRequest(BaseModel):
    scope: str
    modality: str = "conversation"
    content: Content
    context: Context = Field(default_factory=Context)
    observed_actor: Optional[str] = None   # 默认 = caller
    subject: Optional[str] = None          # 默认 = observed_actor
    directives: Optional[Dict[str, Any]] = None
    idempotency_key: str


class ExperienceResponse(BaseModel):
    event_id: str
    wal_offset: int
    status: str
    lifecycle_stream: str


# ── layer rows ─────────────────────────────────────────────────────────────
class EntityOut(BaseModel):
    entity_id: str
    canonical_name: str
    entity_type: Optional[str] = None
    description: Optional[str] = None
    merged_into: Optional[str] = None


class Ref(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None


class FactOut(BaseModel):
    fact_id: str
    scope: str
    subject: Ref
    predicate: str
    object: Dict[str, Any]                       # {datatype, value}
    confidence: float = 0.5
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    supports: List[str] = Field(default_factory=list)


class BeliefOut(BaseModel):
    belief_id: str
    about: Ref
    stance: str
    claim: str
    confidence: float
    confidence_interval: Optional[List[float]] = None
    supports: List[str] = Field(default_factory=list)


class EventOut(BaseModel):
    event_id: str
    scope: str
    modality: str
    observed_actor: str
    content: Dict[str, Any]
    observed_at: str
    excluded_from_recall: bool = False


# ── recall / pack ──────────────────────────────────────────────────────────
class RecallRequest(BaseModel):
    scope: str
    query: Optional[str] = None
    view: str = "local"          # local|holistic|descend|structured
    include: Optional[List[str]] = None
    top_k: Optional[int] = None
    as_of: Optional[str] = None
    include_superseded: bool = False
    recorded_during: Optional[Dict[str, str]] = None   # {from, to}
    budgets: Optional[Dict[str, Any]] = None           # {max_tokens, per_layer_limits}
    citation_mode: str = "inline_with_markers"         # none|inline_with_markers|block_at_end|structured_only
    exclude_content: bool = False
    temporal: Optional[Dict[str, Any]] = None   # {natural, reference_date}


class Layers(BaseModel):
    events: List[EventOut] = Field(default_factory=list)
    facts: List[FactOut] = Field(default_factory=list)
    beliefs: List[BeliefOut] = Field(default_factory=list)


class ProvenanceTrail(BaseModel):
    trail: List[Dict[str, Any]] = Field(default_factory=list)
    citations: Dict[str, Dict[str, str]] = Field(default_factory=dict)


class Diagnostics(BaseModel):
    time_ms: Dict[str, float] = Field(default_factory=dict)
    channels: Dict[str, int] = Field(default_factory=dict)


class StratifiedPack(BaseModel):
    pack_id: str
    scope: str
    view: str
    layers: Layers
    context_block: str = ""
    provenance: ProvenanceTrail = Field(default_factory=ProvenanceTrail)
    diagnostics: Diagnostics = Field(default_factory=Diagnostics)


# ── answer ─────────────────────────────────────────────────────────────────
class AnswerRequest(BaseModel):
    scope: str
    query: str
    use_pack_id: Optional[str] = None


class Citation(BaseModel):
    marker: str
    layer: str
    id: str


class AnswerResponse(BaseModel):
    answer: str
    citations: List[Citation] = Field(default_factory=list)
    model_used: str
    pack_id: str


# ── timeline ───────────────────────────────────────────────────────────────
class TimelineVersion(BaseModel):
    fact_id: str
    object_value: Optional[str]
    valid_from: Optional[str]
    valid_to: Optional[str]
    confidence: float


class TimelineResponse(BaseModel):
    subject: str
    predicate: str
    versions: List[TimelineVersion]


# ── forget ─────────────────────────────────────────────────────────────────
class ForgetRequest(BaseModel):
    scope: str
    layers: List[str] = Field(default_factory=lambda: ["facts", "beliefs"])
    predicate: Optional[str] = None
    about_entity: Optional[str] = None
    cascade: str = "derived_only"    # derived_only|redact_events
    confirm_all: bool = False


class ForgetResponse(BaseModel):
    deleted: Dict[str, int]
    audit_id: str


# ── Stage 6: bulk / import / export ─────────────────────────────────────────
class BulkItem(BaseModel):
    """experience/bulk 的单项(无 scope;scope 来自 bulk 请求体)。"""
    modality: str = "conversation"
    content: Content
    context: Context = Field(default_factory=Context)
    observed_actor: Optional[str] = None
    subject: Optional[str] = None
    directives: Optional[Dict[str, Any]] = None
    idempotency_key: str


class BulkExperienceRequest(BaseModel):
    scope: str
    items: List[BulkItem]
    ordering: str = "strict_temporal"   # strict_temporal | batch_throughput
    directives: Optional[Dict[str, Any]] = None


class ImportResponse(BaseModel):
    import_id: str
    source: str
    accepted: int
    failed: int
    lifecycle_stream: str


class ImportStatus(BaseModel):
    import_id: str
    source: str
    status: str
    accepted: int
    failed: int
    total: int


class ImportJsonlRequest(BaseModel):
    scope: str
    scope_template: Optional[str] = None   # 支持 {field} 占位,从每行 JSON 取
    lines: str                              # JSONL 文本


class ImportMem0Item(BaseModel):
    memory: str
    timestamp: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class ImportMem0Request(BaseModel):
    scope: str
    scope_template: Optional[str] = None
    memories: List[ImportMem0Item]


class ZepFact(BaseModel):
    subject: str
    predicate: str
    object: str
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    confidence: float = 0.8


class ImportZepRequest(BaseModel):
    scope: str
    facts: List[ZepFact]


class LettaBlock(BaseModel):
    label: Optional[str] = None
    text: str


class ImportLettaRequest(BaseModel):
    scope: str
    scope_template: Optional[str] = None
    blocks: List[LettaBlock]


class OpenAIMemoryItem(BaseModel):
    id: Optional[str] = None
    content: str


class ImportOpenAIRequest(BaseModel):
    scope: str
    scope_template: Optional[str] = None
    memories: List[OpenAIMemoryItem]


class ExportRequest(BaseModel):
    scope: str
    format: str = "jsonl"


class ExportResponse(BaseModel):
    export_id: str
    format: str
    bytes: int
    data: str   # JSONL 文本(小批量内联;大批量应走异步下载,这里同步返回)


# ── Stage 7: erasures / episodes / vocab CRUD / temporal / admin ────────────
class ErasureSelector(BaseModel):
    memory_ids: Optional[List[str]] = None
    about_entity: Optional[str] = None
    predicate: Optional[str] = None


class ErasurePreviewRequest(BaseModel):
    scope: str
    selector: ErasureSelector = Field(default_factory=ErasureSelector)


class ErasureExecuteRequest(BaseModel):
    scope: str
    selector: Optional[ErasureSelector] = None
    from_preview_id: Optional[str] = None


class VocabValueIn(BaseModel):
    canonical: str
    aliases: List[str] = Field(default_factory=list)


class VocabCreateRequest(BaseModel):
    scope: str
    name: str
    kind: str = "closed"   # closed | open
    values: List[VocabValueIn] = Field(default_factory=list)


class VocabReplaceRequest(BaseModel):
    scope: str
    kind: Optional[str] = None
    values: List[VocabValueIn] = Field(default_factory=list)


class TemporalPhraseRequest(BaseModel):
    name: str
    expression: str    # dur..dur e.g. -P7D..P0D
    anchor: Optional[str] = None


class MaintenanceRequest(BaseModel):
    action: str        # methylation | consolidation
    scope: str
    older_than_days: Optional[int] = 30


class IngestDocumentRequest(BaseModel):
    """长文档切块入库(机械结构等有标题章节的文档)。"""
    scope: str
    text: str
    intent: str = "structure"     # structure | diagnosis | general
    min_chars: int = 200
    max_chars: int = 2000
