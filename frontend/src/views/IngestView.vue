<script setup lang="ts">
import {
  NCard,
  NForm,
  NFormItem,
  NInput,
  NSelect,
  NButton,
  NSpace,
  NTag,
  NAlert,
  NCode,
  NIcon,
  useMessage,
  type SelectOption,
} from 'naive-ui'
import { computed, onUnmounted, ref } from 'vue'
import { postExperience, subscribeLifecycle } from '@/api'
import { mockExperienceResponse } from '@/api/mock'
import { useScopeStore } from '@/stores/scope'
import { useSettingsStore } from '@/stores/settings'
import type { ExperienceResponse, LifecycleFrame, Modality } from '@/types'

const scopeStore = useScopeStore()
const settings = useSettingsStore()
const message = useMessage()

const modality = ref<Modality>('conversation')
const role = ref<'user' | 'assistant' | 'system'>('user')
const text = ref('Priya owns the Q3 renewal; she said it in standup.')
const labels = ref('')

const modalityOptions: SelectOption[] = [
  { label: 'Conversation', value: 'conversation' },
  { label: 'Document', value: 'document' },
  { label: 'Observation', value: 'observation' },
  { label: 'Feedback', value: 'feedback' },
]
const roleOptions: SelectOption[] = [
  { label: 'user', value: 'user' },
  { label: 'assistant', value: 'assistant' },
  { label: 'system', value: 'system' },
]

const submitting = ref(false)
const response = ref<ExperienceResponse | null>(null)
const error = ref<string | null>(null)

// SSE lifecycle state
const frames = ref<LifecycleFrame[]>([])
const sseConnected = ref(false)
const sseWaiting = ref(false)
const terminal = ref(false)
let unsubscribe: (() => void) | null = null

const extractedCount = computed(() => {
  const f = frames.value.find((x) => x.kind === 'extracted')
  return f?.facts_extracted ?? null
})

function newIdempotencyKey() {
  return `ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

async function submit() {
  if (!text.value.trim()) {
    message.warning('Message text is required')
    return
  }
  error.value = null
  frames.value = []
  terminal.value = false
  sseWaiting.value = false
  response.value = null
  submitting.value = true

  const payload = {
    scope: scopeStore.scope,
    modality: modality.value,
    content: {
      kind: 'message',
      role: modality.value === 'conversation' ? role.value : undefined,
      text: text.value,
    },
    context: {
      observed_at: new Date().toISOString(),
      labels: labels.value
        .split(',')
        .map((l) => l.trim())
        .filter(Boolean),
    },
    idempotency_key: newIdempotencyKey(),
  }

  try {
    if (settings.useMock) {
      // Simulate latency + lifecycle frames for demo.
      await new Promise((r) => setTimeout(r, 350))
      response.value = { ...mockExperienceResponse, lifecycle_stream: `/v1/lifecycle/stream?event_id=${mockExperienceResponse.event_id}` }
      message.success('Captured (mock)')
      runMockLifecycle()
    } else {
      const res = await postExperience(payload)
      response.value = res
      message.success('Captured')
      openLifecycle(res.event_id)
    }
  } catch (e: any) {
    error.value = e?.message ? `POST /experience failed: ${e.message}` : String(e)
    message.error('Ingest failed — see error below')
  } finally {
    submitting.value = false
  }
}

function openLifecycle(eventId: string) {
  unsubscribe?.()
  sseConnected.value = false
  sseWaiting.value = true

  // Give the backend a moment to begin streaming; show "waiting" if no frame yet.
  const waitTimer = window.setTimeout(() => {
    if (frames.value.length === 0 && !terminal.value) {
      sseWaiting.value = true
    }
  }, 1500)

  unsubscribe = subscribeLifecycle(
    eventId,
    (frame) => {
      sseConnected.value = true
      sseWaiting.value = false
      frames.value.push(frame)
      if (frame.kind === 'indexed' || frame.kind === 'failed') {
        terminal.value = true
      }
    },
    () => {
      sseConnected.value = false
      sseWaiting.value = true
      window.clearTimeout(waitTimer)
    },
  )
}

function runMockLifecycle() {
  sseConnected.value = true
  sseWaiting.value = false
  const seq: LifecycleFrame[] = [
    { kind: 'captured', event_id: mockExperienceResponse.event_id, ts: new Date().toISOString() },
  ]
  let i = 0
  const push = () => {
    const next = seq[i]
    if (!next) return
    frames.value.push(next)
    i++
    if (i < seq.length) setTimeout(push, 500)
    else {
      setTimeout(() => {
        frames.value.push({
          kind: 'extracted',
          event_id: mockExperienceResponse.event_id,
          facts_extracted: 2,
          ts: new Date().toISOString(),
        })
      }, 600)
      setTimeout(() => {
        frames.value.push({
          kind: 'indexed',
          event_id: mockExperienceResponse.event_id,
          facts_extracted: 2,
          ts: new Date().toISOString(),
        })
        terminal.value = true
      }, 1300)
    }
  }
  push()
}

onUnmounted(() => {
  unsubscribe?.()
})

function kindTagType(kind: string) {
  switch (kind) {
    case 'captured':
      return 'default'
    case 'extracted':
      return 'info'
    case 'indexed':
      return 'success'
    case 'failed':
      return 'error'
    default:
      return 'default'
  }
}

const responseJson = computed(() => (response.value ? JSON.stringify(response.value, null, 2) : ''))
</script>

<template>
  <div class="ingest-view">
    <div class="page-head">
      <h1>Ingest</h1>
      <p class="muted">
        Capture a single experience. The only write in Cortex is
        <code>POST /v1/experience</code> — everything else derives from it.
      </p>
    </div>

    <div class="grid">
      <NCard title="New experience" size="small" class="form-card">
        <NForm label-placement="top" require-mark-placement="right-hanging">
          <NFormItem label="Scope">
            <NInput :value="scopeStore.scope" @update:value="scopeStore.setScope" />
          </NFormItem>

          <NSpace>
            <NFormItem label="Modality" style="width: 180px">
              <NSelect v-model:value="modality" :options="modalityOptions" />
            </NFormItem>
            <NFormItem v-if="modality === 'conversation'" label="Role" style="width: 160px">
              <NSelect v-model:value="role" :options="roleOptions" />
            </NFormItem>
          </NSpace>

          <NFormItem label="Message text">
            <NInput
              v-model:value="text"
              type="textarea"
              placeholder="What was said / observed?"
              :autosize="{ minRows: 4, maxRows: 10 }"
            />
          </NFormItem>

          <NFormItem label="Labels (comma separated, optional)">
            <NInput v-model:value="labels" placeholder="renewal, q3, priya" />
          </NFormItem>

          <NSpace justify="end">
            <NButton type="primary" :loading="submitting" @click="submit">
              Submit experience
            </NButton>
          </NSpace>
        </NForm>
      </NCard>

      <div class="right-col">
        <NCard title="Lifecycle stream" size="small">
          <template #header-extra>
            <NTag v-if="sseConnected" type="success" size="small" round>live</NTag>
            <NTag v-else-if="sseWaiting && !settings.useMock" type="warning" size="small" round>
              waiting for backend
            </NTag>
            <NTag v-else-if="settings.useMock" type="warning" size="small" round>mock</NTag>
          </template>

          <NAlert
            v-if="sseWaiting && !settings.useMock && frames.length === 0"
            type="warning"
            :show-icon="true"
            style="margin-bottom: 12px"
          >
            Waiting for backend — start the FastAPI server on :8000 to see real lifecycle frames.
            (Toggle "Live API → Mock data" in the header to demo without the backend.)
          </NAlert>

          <div v-if="frames.length === 0" class="empty muted">
            Lifecycle frames will appear here as the event moves through
            <code>captured → extracted → indexed</code>.
          </div>

          <ul v-else class="frame-list">
            <li v-for="(f, idx) in frames" :key="idx" class="frame-item">
              <NTag :type="kindTagType(f.kind)" size="small" round>{{ f.kind }}</NTag>
              <span v-if="f.kind === 'extracted'" class="frame-extra">
                <strong>{{ f.facts_extracted }}</strong> facts extracted
              </span>
              <span v-if="f.kind === 'indexed'" class="frame-extra">indexed into graph</span>
              <span v-if="f.ts" class="frame-ts">{{ new Date(f.ts).toLocaleTimeString() }}</span>
            </li>
          </ul>
        </NCard>

        <NCard v-if="extractedCount !== null" title="Extraction result" size="small">
          <p>
            Extracted <strong>{{ extractedCount }}</strong> fact(s) from this experience. They will
            appear in the <RouterLink to="/graph">Knowledge Graph</RouterLink> once indexed.
          </p>
        </NCard>

        <NCard v-if="error" title="Error" size="small">
          <NAlert type="error" :show-icon="true">{{ error }}</NAlert>
        </NCard>

        <NCard v-if="response" title="Response" size="small">
          <div class="resp-meta">
            <NTag type="success" size="small" round>{{ response.status }}</NTag>
            <span class="muted">wal_offset: {{ response.wal_offset }}</span>
          </div>
          <NCode :code="responseJson" language="json" word-wrap />
        </NCard>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page-head h1 {
  margin: 0 0 4px;
  font-size: 24px;
}
.muted {
  color: var(--cortex-muted);
}
.page-head {
  margin-bottom: 20px;
}
.grid {
  display: grid;
  grid-template-columns: 1.1fr 1fr;
  gap: 20px;
  align-items: start;
}
.right-col {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.empty {
  font-size: 13px;
  line-height: 1.6;
}
.frame-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.frame-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  background: var(--cortex-bg);
  border-radius: 8px;
  font-size: 13px;
}
.frame-extra {
  color: var(--cortex-text);
}
.frame-ts {
  margin-left: auto;
  font-family: ui-monospace, monospace;
  font-size: 11px;
  color: var(--cortex-muted);
}
.resp-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  font-size: 12px;
}
@media (max-width: 980px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: rgba(0, 0, 0, 0.04);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 0.9em;
}
</style>
