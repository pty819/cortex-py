<script setup lang="ts">
import {
  NCard,
  NInput,
  NButton,
  NSpace,
  NSpin,
  NEmpty,
  NAlert,
  NCode,
  NCollapse,
  NCollapseItem,
  NTag,
  NStatistic,
  NDescriptions,
  NDescriptionsItem,
  useMessage,
} from 'naive-ui'
import { computed, ref } from 'vue'
import { postAnswer, postRecall } from '@/api'
import { useScopeStore } from '@/stores/scope'
import type { AnswerResponse, Citation, StratifiedPack, Fact } from '@/types'
import { getFacts } from '@/api'

const scopeStore = useScopeStore()
const message = useMessage()

const query = ref('Who owns the Q3 renewal?')
const loading = ref(false)
const error = ref<string | null>(null)
const answer = ref<AnswerResponse | null>(null)
const pack = ref<StratifiedPack | null>(null)

const activeCitation = ref<string | null>(null)

// Local facts cache to resolve citation fact details inline.
const factCache = ref<Record<string, Fact>>({})

async function ensureFactCache() {
  if (Object.keys(factCache.value).length > 0) return
  try {
    const f = await getFacts(scopeStore.scope)
    factCache.value = Object.fromEntries(f.items.map((x) => [x.fact_id, x]))
  } catch {
    /* non-fatal */
  }
}

async function ask() {
  if (!query.value.trim()) {
    message.warning('Enter a query')
    return
  }
  loading.value = true
  error.value = null
  answer.value = null
  pack.value = null
  activeCitation.value = null
  try {
    await ensureFactCache()
    // Fetch the answer; also fetch the raw pack for the collapsible view.
    const [ans, pk] = await Promise.all([
      postAnswer({ scope: scopeStore.scope, query: query.value }),
      postRecall({ scope: scopeStore.scope, query: query.value, view: 'holistic' }).catch(() => null),
    ])
    answer.value = ans
    pack.value = pk
  } catch (e: any) {
    error.value = e?.message ? `Answer failed: ${e.message}` : String(e)
    message.error('Query failed')
  } finally {
    loading.value = false
  }
}

// Split the answer text into segments so [n] markers become clickable chips.
interface Segment {
  text: string
  citation?: Citation
}
const segments = computed<Segment[]>(() => {
  if (!answer.value) return []
  const out: Segment[] = []
  const re = /(\[\d+\])/g
  const parts = answer.value.answer.split(re)
  for (const p of parts) {
    if (!p) continue
    const c = answer.value!.citations.find((x) => x.marker === p)
    out.push({ text: p, citation: c })
  }
  return out
})

function toggleCitation(c: Citation) {
  activeCitation.value = activeCitation.value === c.marker ? null : c.marker
}

const activeFact = computed<Fact | null>(() => {
  const c = answer.value?.citations.find((x) => x.marker === activeCitation.value)
  if (!c?.fact_id) return null
  return factCache.value[c.fact_id] || null
})

const packJson = computed(() => (pack.value ? JSON.stringify(pack.value, null, 2) : ''))

const examples = [
  'Who owns the Q3 renewal?',
  'What is the status of the Q3 renewal?',
  'Who approves deals at Acme?',
  'What is Acme Corp ARR?',
]
</script>

<template>
  <div class="qa-view">
    <div class="page-head">
      <h1>Ask</h1>
      <p class="muted">Hybrid recall + LLM synthesis. Answers carry inline [n] citations you can expand.</p>
    </div>

    <NCard size="small" class="ask-card">
      <NSpace vertical :size="12">
        <NInput
          v-model:value="query"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 5 }"
          placeholder="Ask about the knowledge graph…"
          @keydown.enter.exact.prevent="ask"
        />
        <NSpace justify="space-between" align="center">
          <NSpace :size="6">
            <NButton
              v-for="ex in examples"
              :key="ex"
              size="tiny"
              quaternary
              @click="query = ex"
            >
              {{ ex }}
            </NButton>
          </NSpace>
          <NButton type="primary" :loading="loading" @click="ask">Ask</NButton>
        </NSpace>
      </NSpace>
    </NCard>

    <NAlert v-if="error" type="error" :show-icon="true" style="margin-top: 16px">{{ error }}</NAlert>

    <NSpin v-if="loading" style="margin-top: 24px; display: flex; justify-content: center" />

    <div v-if="answer" class="results">
      <NCard size="small" title="Answer">
        <template #header-extra>
          <NTag size="small" round type="info">{{ answer.model_used }}</NTag>
        </template>
        <p class="answer-text">
          <template v-for="(seg, i) in segments" :key="i">
            <span v-if="seg.citation" class="chip" @click="toggleCitation(seg.citation)">{{ seg.text }}</span>
            <span v-else>{{ seg.text }}</span>
          </template>
        </p>

        <div v-if="activeCitation && activeFact" class="citation-detail">
          <div class="cd-head">
            <NTag size="small" round type="success">{{ activeCitation }}</NTag>
            <span class="cd-fact">
              <strong>{{ activeFact.subject.name }}</strong>
              <span class="cd-arrow">—{{ activeFact.predicate }}→</span>
              <strong>{{ activeFact.object.value }}</strong>
            </span>
            <NTag size="tiny" round>{{ activeFact.object.datatype }}</NTag>
            <span class="cd-conf">{{ Math.round(activeFact.confidence * 100) }}% confidence</span>
          </div>
          <div class="cd-meta muted">
            valid from {{ new Date(activeFact.valid_from).toLocaleString() }}
            <span v-if="activeFact.valid_to"> → {{ new Date(activeFact.valid_to).toLocaleString() }}</span>
            <span v-else> → now</span>
          </div>
        </div>
      </NCard>

      <NCard size="small" title="Raw pack" style="margin-top: 16px">
        <NCollapse v-if="pack" :default-expanded-names="[]" arrow-placement="right">
          <NCollapseItem title="Layers (events / facts / beliefs)" name="layers">
            <NDescriptions :column="3" size="small" bordered>
              <NDescriptionsItem label="Events">
                {{ pack.layers.events.length }}
              </NDescriptionsItem>
              <NDescriptionsItem label="Facts">
                {{ pack.layers.facts.length }}
              </NDescriptionsItem>
              <NDescriptionsItem label="Beliefs">
                {{ pack.layers.beliefs.length }}
              </NDescriptionsItem>
            </NDescriptions>
          </NCollapseItem>
          <NCollapseItem title="Provenance trail" name="provenance">
            <NSpace :size="20">
              <NStatistic
                v-for="step in pack.provenance.trail"
                :key="step.step"
                :label="step.step"
                :value="step.kept"
              />
            </NSpace>
          </NCollapseItem>
          <NCollapseItem title="Diagnostics" name="diagnostics">
            <NSpace :size="20">
              <NStatistic
                v-for="(ms, name) in pack.diagnostics.time_ms"
                :key="name"
                :label="`${name} (ms)`"
                :value="ms"
              />
            </NSpace>
          </NCollapseItem>
          <NCollapseItem title="Full JSON" name="json">
            <NCode :code="packJson" language="json" word-wrap />
          </NCollapseItem>
        </NCollapse>
        <NEmpty v-else description="Pack not available (recall failed or returned null)." />
      </NCard>
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
  margin-bottom: 16px;
}
.answer-text {
  font-size: 16px;
  line-height: 1.7;
  margin: 0;
}
.chip {
  display: inline-block;
  background: #eff6ff;
  color: var(--cortex-primary);
  border: 1px solid #bfdbfe;
  border-radius: 999px;
  padding: 0 8px;
  margin: 0 2px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  transition: background 0.15s;
}
.chip:hover {
  background: #dbeafe;
}
.citation-detail {
  margin-top: 16px;
  padding: 12px 14px;
  background: var(--cortex-bg);
  border-radius: 8px;
  border-left: 3px solid var(--cortex-primary);
}
.cd-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 13px;
}
.cd-fact {
  font-size: 14px;
}
.cd-arrow {
  color: var(--cortex-muted);
  margin: 0 4px;
}
.cd-conf {
  font-family: ui-monospace, monospace;
  font-size: 12px;
  color: var(--cortex-muted);
}
.cd-meta {
  margin-top: 6px;
  font-size: 12px;
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
</style>
