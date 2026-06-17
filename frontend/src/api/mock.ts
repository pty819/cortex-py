import type {
  Entity,
  Fact,
  ExperienceResponse,
  StratifiedPack,
  AnswerResponse,
  TimelineResponse,
} from '@/types'

// Mock data for local dev when the FastAPI backend is not running.
// Lets you click around every page without a live server.

export const mockEntities: Entity[] = [
  { entity_id: 'e_acme', canonical_name: 'Acme Corp', entity_type: 'org', description: 'Software vendor headquartered in Portland.' },
  { entity_id: 'e_priya', canonical_name: 'Priya Rao', entity_type: 'person', description: 'Account executive covering enterprise renewals.' },
  { entity_id: 'e_q3', canonical_name: 'Q3 Renewal', entity_type: 'deal', description: 'Upcoming renewal for Acme Corp seat bundle.' },
  { entity_id: 'e_jordan', canonical_name: 'Jordan Lee', entity_type: 'person', description: 'Procurement lead at Acme.' },
  { entity_id: 'e_slack', canonical_name: 'Slack Channel', entity_type: 'channel', description: '#acme-renewal shared with customer.' },
]

export const mockFacts: Fact[] = [
  {
    fact_id: 'f_1',
    subject: { id: 'e_priya', name: 'Priya Rao' },
    predicate: 'owns',
    object: { datatype: 'entity', value: 'Q3 Renewal' },
    confidence: 0.88,
    valid_from: '2026-05-01T00:00:00Z',
    valid_to: null,
  },
  {
    fact_id: 'f_2',
    subject: { id: 'e_priya', name: 'Priya Rao' },
    predicate: 'works_at',
    object: { datatype: 'entity', value: 'Acme Corp' },
    confidence: 0.97,
    valid_from: '2026-01-15T00:00:00Z',
    valid_to: null,
  },
  {
    fact_id: 'f_3',
    subject: { id: 'e_q3', name: 'Q3 Renewal' },
    predicate: 'status',
    object: { datatype: 'literal', value: 'negotiating' },
    confidence: 0.82,
    valid_from: '2026-06-10T00:00:00Z',
    valid_to: null,
  },
  {
    fact_id: 'f_4',
    subject: { id: 'e_q3', name: 'Q3 Renewal' },
    predicate: 'status',
    object: { datatype: 'literal', value: 'signed' },
    confidence: 0.95,
    valid_from: '2026-06-17T00:00:00Z',
    valid_to: null,
  },
  {
    fact_id: 'f_5',
    subject: { id: 'e_jordan', name: 'Jordan Lee' },
    predicate: 'works_at',
    object: { datatype: 'entity', value: 'Acme Corp' },
    confidence: 0.91,
    valid_from: '2026-02-01T00:00:00Z',
    valid_to: null,
  },
  {
    fact_id: 'f_6',
    subject: { id: 'e_jordan', name: 'Jordan Lee' },
    predicate: 'approves',
    object: { datatype: 'entity', value: 'Q3 Renewal' },
    confidence: 0.76,
    valid_from: '2026-06-12T00:00:00Z',
    valid_to: null,
  },
  {
    fact_id: 'f_7',
    subject: { id: 'e_acme', name: 'Acme Corp' },
    predicate: 'arr',
    object: { datatype: 'literal', value: '$1.2M' },
    confidence: 0.8,
    valid_from: '2026-04-01T00:00:00Z',
    valid_to: null,
  },
]

export const mockExperienceResponse: ExperienceResponse = {
  event_id: 'evt-mock-0001',
  wal_offset: 1,
  status: 'captured',
  lifecycle_stream: '/v1/lifecycle/stream?event_id=evt-mock-0001',
}

export const mockPack: StratifiedPack = {
  pack_id: 'pack-mock-0001',
  layers: {
    events: [
      { event_id: 'evt-mock-0001', summary: 'Priya mentioned she owns the Q3 renewal.' },
    ],
    facts: mockFacts.slice(0, 4),
    beliefs: [
      { belief: 'Q3 Renewal is at risk until countersigned.' },
    ],
  },
  context_block:
    'Priya Rao owns the Q3 Renewal [1]. The deal moved from "negotiating" to "signed" on 2026-06-17 [2]. Jordan Lee (procurement at Acme) approved it [3].',
  provenance: {
    trail: [
      { step: 'fetch', kept: 12 },
      { step: 'rerank', kept: 5 },
      { step: 'assemble', kept: 3 },
    ],
    citations: {
      '[1]': { layer: 'fact', id: 'f_1' },
      '[2]': { layer: 'fact', id: 'f_4' },
      '[3]': { layer: 'fact', id: 'f_6' },
    },
  },
  diagnostics: { time_ms: { fetch: 36, rerank: 22, assemble: 8 } },
}

export const mockAnswer: AnswerResponse = {
  answer: 'Priya Rao owns the Q3 Renewal [1]. It was signed on 2026-06-17 [2], approved by Jordan Lee [3].',
  citations: [
    { marker: '[1]', layer: 'fact', fact_id: 'f_1' },
    { marker: '[2]', layer: 'fact', fact_id: 'f_4' },
    { marker: '[3]', layer: 'fact', fact_id: 'f_6' },
  ],
  model_used: 'Minimax-M3',
  pack_id: 'pack-mock-0001',
}

export const mockTimeline: TimelineResponse = {
  versions: [
    { fact_id: 'f_3', object_value: 'negotiating', valid_from: '2026-06-10T00:00:00Z', valid_to: '2026-06-17T00:00:00Z', confidence: 0.82 },
    { fact_id: 'f_4', object_value: 'signed', valid_from: '2026-06-17T00:00:00Z', valid_to: null, confidence: 0.95 },
  ],
}
