// Shared API types matching the Cortex /v1 contract.

export type Modality = 'conversation' | 'document' | 'observation' | 'feedback'

export type ObjectType = 'entity' | 'literal'

export interface ActorRef {
  id: string
  name: string
}

export interface FactObject {
  datatype: ObjectType
  value: string
  // entity 对象的 entity_id;literal 对象没有此字段。后端 /v1/facts 仅在
  // object 为实体时返回,供前端解析 incoming 边(该节点作为宾语)。
  id?: string
}

export interface ExperienceRequest {
  scope: string
  modality: Modality
  content: {
    kind: string
    role?: string
    text: string
  }
  context: {
    observed_at: string
    labels?: string[]
  }
  idempotency_key: string
}

export interface ExperienceResponse {
  event_id: string
  wal_offset: number
  status: string
  lifecycle_stream: string
}

export type LifecycleKind = 'captured' | 'extracted' | 'indexed' | 'failed'

export interface LifecycleFrame {
  kind: LifecycleKind
  event_id: string
  facts_extracted?: number
  ts?: string
  message?: string
}

export interface Entity {
  entity_id: string
  canonical_name: string
  entity_type: string
  description?: string
}

export interface EntitiesResponse {
  items: Entity[]
}

export interface Fact {
  fact_id: string
  subject: ActorRef
  predicate: string
  object: FactObject
  confidence: number
  valid_from: string
  valid_to: string | null
}

export interface FactsResponse {
  items: Fact[]
}

export interface TimelineVersion {
  fact_id: string
  object_value: string
  valid_from: string
  valid_to: string | null
  confidence?: number
}

export interface TimelineResponse {
  versions: TimelineVersion[]
}

export interface RecallRequest {
  scope: string
  query: string
  view?: string
}

export interface AnswerRequest {
  scope: string
  query: string
}

export interface Citation {
  marker: string
  layer: string
  fact_id?: string
}

export interface AnswerResponse {
  answer: string
  citations: Citation[]
  model_used: string
  pack_id: string
}

export interface TrailStep {
  step: string
  kept: number
}

export interface Provenance {
  trail: TrailStep[]
  citations: Record<string, { layer: string; id: string }>
}

export interface Diagnostics {
  time_ms?: Record<string, number>
}

export interface StratifiedPack {
  pack_id: string
  layers: {
    events: unknown[]
    facts: unknown[]
    beliefs: unknown[]
  }
  context_block: string
  provenance: Provenance
  diagnostics: Diagnostics
}
