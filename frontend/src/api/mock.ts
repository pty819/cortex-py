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
  { entity_id: 'e_et', canonical_name: 'E-301', entity_type: 'equipment', description: '刻蚀机台 E-301。' },
  { entity_id: 'e_gs', canonical_name: '气体输送系统', entity_type: 'subsystem', description: '负责向腔体输送工艺气体。' },
  { entity_id: 'e_mfc1', canonical_name: '质量流量控制器MFC-1', entity_type: 'component', description: '控制 CF4 气体流量的质量流量控制器。' },
  { entity_id: 'e_p02', canonical_name: '压力传感器P-02', entity_type: 'sensor', description: '安装在腔体上的高精度压力监测仪表。' },
  { entity_id: 'e_pid', canonical_name: '腔体压力PID', entity_type: 'controller', description: '腔体压力闭环控制单元。' },
  { entity_id: 'e_press', canonical_name: '腔体压力异常', entity_type: 'fault', description: '工艺腔体内压力偏离设定值的异常状态。' },
  { entity_id: 'e_fluc', canonical_name: '压力读数波动', entity_type: 'symptom', description: 'P-02 读数周期性波动约 ±0.5mTorr。' },
  { entity_id: 'e_hyp', canonical_name: '怀疑MFC校准漂移', entity_type: 'hypothesis', description: '假设 MFC-1 流量校准发生漂移。' },
]

export const mockFacts: Fact[] = [
  {
    fact_id: 'f_1',
    subject: { id: 'e_mfc1', name: '质量流量控制器MFC-1' },
    predicate: 'part_of',
    object: { datatype: 'entity', value: '气体输送系统', id: 'e_gs' },
    confidence: 0.97,
    valid_from: '2026-01-01T00:00:00Z',
    valid_to: null,
  },
  {
    fact_id: 'f_2',
    subject: { id: 'e_gs', name: '气体输送系统' },
    predicate: 'part_of',
    object: { datatype: 'entity', value: 'E-301', id: 'e_et' },
    confidence: 0.97,
    valid_from: '2026-01-01T00:00:00Z',
    valid_to: null,
  },
  {
    fact_id: 'f_3',
    subject: { id: 'e_press', name: '腔体压力异常' },
    predicate: 'monitored_by',
    object: { datatype: 'entity', value: '压力传感器P-02', id: 'e_p02' },
    confidence: 0.92,
    valid_from: '2026-06-18T00:00:00Z',
    valid_to: null,
  },
  {
    fact_id: 'f_4',
    subject: { id: 'e_press', name: '腔体压力异常' },
    predicate: 'has_symptom',
    object: { datatype: 'entity', value: '压力读数波动', id: 'e_fluc' },
    confidence: 0.88,
    valid_from: '2026-06-18T00:00:00Z',
    valid_to: null,
  },
  {
    fact_id: 'f_5',
    subject: { id: 'e_press', name: '腔体压力异常' },
    predicate: 'caused_by',
    object: { datatype: 'entity', value: '质量流量控制器MFC-1', id: 'e_mfc1' },
    confidence: 0.85,
    valid_from: '2026-06-19T00:00:00Z',
    valid_to: null,
  },
  {
    fact_id: 'f_6',
    subject: { id: 'e_hyp', name: '怀疑MFC校准漂移' },
    predicate: 'investigates',
    object: { datatype: 'entity', value: '质量流量控制器MFC-1', id: 'e_mfc1' },
    confidence: 0.76,
    valid_from: '2026-06-19T00:00:00Z',
    valid_to: null,
  },
  {
    fact_id: 'f_7',
    subject: { id: 'e_press', name: '腔体压力异常' },
    predicate: 'controlled_by',
    object: { datatype: 'entity', value: '腔体压力PID', id: 'e_pid' },
    confidence: 0.9,
    valid_from: '2026-01-01T00:00:00Z',
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
      { event_id: 'evt-mock-0001', summary: '工程师记录:腔体压力在工艺中周期性波动。' },
    ],
    facts: mockFacts.slice(0, 4),
    beliefs: [
      { belief: '腔体压力异常很可能由 MFC-1 校准漂移导致。' },
    ],
  },
  context_block:
    '腔体压力异常由压力传感器P-02监测 [1],表现为压力读数波动 [2],疑似由质量流量控制器MFC-1引起 [3]。怀疑MFC校准漂移这一假设正在排查MFC-1 [4]。',
  provenance: {
    trail: [
      { step: 'fetch', kept: { vector: 8, bm25: 4 } },
      { step: 'rerank', kept: 5 },
      { step: 'assemble', kept: 3 },
    ],
    citations: {
      '[1]': { layer: 'fact', id: 'f_3' },
      '[2]': { layer: 'fact', id: 'f_4' },
      '[3]': { layer: 'fact', id: 'f_5' },
      '[4]': { layer: 'fact', id: 'f_6' },
    },
  },
  diagnostics: { time_ms: { fetch: 36, rerank: 22, assemble: 8 } },
}

export const mockAnswer: AnswerResponse = {
  answer: '腔体压力异常由压力传感器P-02监测 [1],其征兆是压力读数波动 [2],疑似根因为质量流量控制器MFC-1 [3]。正在排查MFC-1校准漂移的假设 [4]。',
  citations: [
    { marker: '[1]', layer: 'fact', id: 'f_3' },
    { marker: '[2]', layer: 'fact', id: 'f_4' },
    { marker: '[3]', layer: 'fact', id: 'f_5' },
    { marker: '[4]', layer: 'fact', id: 'f_6' },
  ],
  model_used: 'Minimax-M3',
  pack_id: 'pack-mock-0001',
}

export const mockTimeline: TimelineResponse = {
  versions: [
    { fact_id: 'f_4', object_value: '±0.5mTorr 周期波动', valid_from: '2026-06-18T00:00:00Z', valid_to: null, confidence: 0.88 },
  ],
}
