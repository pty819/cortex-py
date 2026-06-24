import axios from 'axios'
import type {
  ExperienceRequest,
  ExperienceResponse,
  EntitiesResponse,
  FactsResponse,
  TimelineResponse,
  RecallRequest,
  StratifiedPack,
  AnswerRequest,
  AnswerResponse,
  LifecycleFrame,
  BeliefsResponse,
} from '@/types'

// Hardcoded demo credentials per the task contract.
const AUTH_TOKEN = 'dev-key'
const ACTOR = 'user:alice'

// 后端本体论是半导体设备故障诊断(prompts.py PROJECT_CONTEXT);旧的
// org:acme/dept:sales 是 CRM 模板残留,与 demo 数据 scope 不符。
const EXAMPLE_SCOPES = [
  'org:fab-a/etch:E301',
]

// 动态从 DB 拉 scope 列表(失败时回退到预设)
export async function fetchScopes(): Promise<string[]> {
  try {
    const r = await http.get<{ items: { scope_path: string }[] }>('/scopes/list')
    const scopes = r.data.items.map((i) => i.scope_path)
    return scopes.length > 0 ? scopes : EXAMPLE_SCOPES
  } catch {
    return EXAMPLE_SCOPES
  }
}

export const DEFAULT_SCOPE: string =
  EXAMPLE_SCOPES[0] ?? 'org:fab-a/etch:E301'
export { EXAMPLE_SCOPES }

const http = axios.create({
  baseURL: '/v1',
  headers: {
    Authorization: `Bearer ${AUTH_TOKEN}`,
    'X-Cortex-Actor': ACTOR,
    'Content-Type': 'application/json',
  },
})

// ---------- Ingest ----------

export function postExperience(payload: ExperienceRequest): Promise<ExperienceResponse> {
  return http.post<ExperienceResponse>('/experience', payload).then((r) => r.data)
}

// ---------- Lifecycle SSE ----------

/**
 * Subscribe to the lifecycle stream for a captured event.
 * Returns an unsubscribe function. Calls `onFrame` for each lifecycle event.
 * On connection failure, calls `onError` once (caller decides whether to retry).
 *
 * EventSource can't send custom headers, so the proxy passes the URL through
 * to the backend. For the dev demo we rely on the backend being lenient on the
 * SSE endpoint, or on the proxy injecting headers (see note in README).
 */
export function subscribeLifecycle(
  eventId: string,
  onFrame: (frame: LifecycleFrame) => void,
  onError: (err: Event) => void,
): () => void {
  // Build an absolute path so EventSource works with the Vite dev proxy too.
  const url = `/v1/lifecycle/stream?event_id=${encodeURIComponent(eventId)}`
  let es: EventSource | null = null
  try {
    es = new EventSource(url, { withCredentials: false })
  } catch (e) {
    onError(e as unknown as Event)
    return () => {}
  }

  es.addEventListener('lifecycle', (ev) => {
    try {
      const data = JSON.parse((ev as MessageEvent).data) as LifecycleFrame
      onFrame(data)
      if (data.kind === 'indexed' || data.kind === 'failed') {
        es?.close()
      }
    } catch {
      /* ignore malformed frame */
    }
  })

  es.onerror = (err) => {
    onError(err)
    es?.close()
  }

  return () => es?.close()
}

// ---------- Graph ----------

export function getEntities(scope: string): Promise<EntitiesResponse> {
  return http.get<EntitiesResponse>('/entities', { params: { scope } }).then((r) => r.data)
}

export function getFacts(scope: string): Promise<FactsResponse> {
  return http.get<FactsResponse>('/facts', { params: { scope } }).then((r) => r.data)
}

export function getTimeline(
  scope: string,
  subject: string,
  predicate: string,
): Promise<TimelineResponse> {
  return http
    .get<TimelineResponse>('/facts/timeline', { params: { scope, subject, predicate } })
    .then((r) => r.data)
}

export function getBeliefs(scope: string): Promise<BeliefsResponse> {
  return http
    .get<BeliefsResponse>('/beliefs', { params: { scope } })
    .then((r) => r.data)
}

// ---------- Recall / Answer ----------

export function postRecall(payload: RecallRequest): Promise<StratifiedPack> {
  return http.post<StratifiedPack>('/recall', payload).then((r) => r.data)
}

export function postAnswer(payload: AnswerRequest): Promise<AnswerResponse> {
  return http.post<AnswerResponse>('/answer', payload).then((r) => r.data)
}
