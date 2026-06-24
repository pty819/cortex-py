<script setup lang="ts">
import {
  NCard,
  NButton,
  NSpace,
  NSelect,
  NEmpty,
  NSpin,
  NTag,
  NAlert,
  NTimeline,
  NTimelineItem,
  NModal,
  NDrawer,
  NDrawerContent,
  NDescriptions,
  NDescriptionsItem,
  NStatistic,
  useMessage,
  type SelectOption,
} from 'naive-ui'
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { Network, type Options as NetworkOptions } from 'vis-network'
import { DataSet } from 'vis-data'
import { getEntities, getFacts, getTimeline } from '@/api'
import { useScopeStore } from '@/stores/scope'

import type { Entity, Fact, TimelineResponse } from '@/types'

const scopeStore = useScopeStore()

const message = useMessage()

const entities = ref<Entity[]>([])
const facts = ref<Fact[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

// Selected node -> side panel
const selectedId = ref<string | null>(null)
const selectedEntity = computed(() =>
  entities.value.find((e) => e.entity_id === selectedId.value) || null,
)
// 同时包含该节点作为主语(outgoing)和宾语(incoming)的 facts。过去只筛
// f.subject.id,导致点一个只作为宾语出现的节点时侧边栏看不到任何边。
// 每条结果带方向标记,模板据此区分显示。
interface SelectedFact {
  fact: Fact
  direction: 'out' | 'in'
}
const selectedFacts = computed<SelectedFact[]>(() => {
  if (!selectedId.value) return []
  const out: SelectedFact[] = []
  facts.value.forEach((f) => {
    if (f.subject.id === selectedId.value) {
      out.push({ fact: f, direction: 'out' })
    }
    // incoming:节点作为实体宾语(literal 宾语没有 id,不算)
    if (f.object.datatype === 'entity' && f.object.id === selectedId.value) {
      out.push({ fact: f, direction: 'in' })
    }
  })
  return out
})

// Timeline drawer state
const drawerOpen = ref(false)
const timelinePredicate = ref<string>('')
const timelineLoading = ref(false)
const timelineData = ref<TimelineResponse | null>(null)

// Distinct predicates for the selected node — include both outgoing (as
// subject) and incoming (as object entity) so the timeline picker covers all
// edges touching this node.
const predicateOptions = computed<SelectOption[]>(() => {
  const set = new Set<string>()
  facts.value.forEach((f) => {
    if (!selectedId.value) return
    if (f.subject.id === selectedId.value) set.add(f.predicate)
    if (f.object.datatype === 'entity' && f.object.id === selectedId.value) set.add(f.predicate)
  })
  return [...set].map((p) => ({ label: p, value: p }))
})

// ---- vis-network ----
const containerEl = ref<HTMLElement | null>(null)
const network = shallowRef<Network | null>(null)
const nodes = new DataSet<any>([])
const edges = new DataSet<any>([])

// Color palette by entity type
const TYPE_COLORS: Record<string, string> = {
  org: '#3b82f6',
  person: '#10b981',
  deal: '#f59e0b',
  channel: '#8b5cf6',
  project: '#ec4899',
  default: '#6b7280',
}
const DEFAULT_NODE_COLOR = '#6b7280'
function colorFor(type: string) {
  return TYPE_COLORS[type] ?? DEFAULT_NODE_COLOR
}

function buildGraph() {
  nodes.clear()
  edges.clear()

  const nodeIds = new Set<string>()

  // Entity nodes
  entities.value.forEach((e) => {
    nodes.add({
      id: e.entity_id,
      label: e.canonical_name,
      group: e.entity_type,
      color: {
        background: colorFor(e.entity_type),
        border: shade(colorFor(e.entity_type), -20),
        highlight: { background: colorFor(e.entity_type) },
      },
      font: { color: '#1f2937', strokeWidth: 4, strokeColor: '#ffffff', size: 14, face: 'system-ui, sans-serif' },
      title: `${e.canonical_name} (${e.entity_type})\n${e.description || ''}`,
      shape: 'dot',
      size: 18,
    })
    nodeIds.add(e.entity_id)
  })

  // Edges from facts
  facts.value.forEach((f, idx) => {
    const subjId = f.subject.id
    // Entity-object edges connect to an existing entity node; literal-object edges
    // carry the value as the edge label.
    if (f.object.datatype === 'entity') {
      // find target entity by canonical_name match (object.value is a name in the contract example)
      const target = entities.value.find((e) => e.canonical_name === f.object.value)
      const targetId = target?.entity_id
      if (targetId && nodeIds.has(targetId)) {
        edges.add({
          id: f.fact_id || `edge_${idx}`,
          from: subjId,
          to: targetId,
          label: f.predicate,
          arrows: 'to',
          title: `${f.subject.name} —${f.predicate}→ ${f.object.value}\nconf: ${f.confidence}`,
          color: { color: '#94a3b8', highlight: '#3b82f6' },
          font: { size: 11, color: '#475569', strokeWidth: 0, face: 'inherit' },
          smooth: { type: 'continuous' },
        })
      } else {
        // Entity object not in node set: add a ghost node
        const ghostId = `ghost_${f.object.value}`
        if (!nodeIds.has(ghostId)) {
          nodes.add({
            id: ghostId,
            label: f.object.value,
            color: { background: '#cbd5e1', border: '#94a3b8' },
            font: { color: '#334155' },
            shape: 'dot',
            size: 14,
            title: `referenced entity: ${f.object.value}`,
          })
          nodeIds.add(ghostId)
        }
        edges.add({
          id: f.fact_id || `edge_${idx}`,
          from: subjId,
          to: ghostId,
          label: f.predicate,
          arrows: 'to',
          color: { color: '#cbd5e1' },
          font: { size: 11, color: '#64748b' },
        })
      }
    } else {
      // literal object -> show as a self-loop-ish edge label on the subject
      edges.add({
        id: `lit_${f.fact_id || idx}`,
        from: subjId,
        to: subjId,
        label: `${f.predicate}: ${f.object.value}`,
        arrows: '',
        dashes: true,
        color: { color: '#a78bfa' },
        font: { size: 11, color: '#6d28d9' },
        smooth: { type: 'curvedCW', roundness: 0.4 },
      })
    }
  })
}

function shade(hex: string, percent: number) {
  const num = parseInt(hex.replace('#', ''), 16)
  const r = (num >> 16) + Math.round((percent / 100) * 255)
  const g = ((num >> 8) & 0x00ff) + Math.round((percent / 100) * 255)
  const b = (num & 0x0000ff) + Math.round((percent / 100) * 255)
  const clamp = (v: number) => Math.max(0, Math.min(255, v))
  return `#${(0x1000000 + clamp(r) * 0x10000 + clamp(g) * 0x100 + clamp(b)).toString(16).slice(1)}`
}

const networkOptions: NetworkOptions = {
  physics: {
    stabilization: { iterations: 120 },
    barnesHut: { gravitationalConstant: -8000, springLength: 130 },
  },
  interaction: { hover: true, tooltipDelay: 120 },
  nodes: { borderWidth: 2 },
  edges: { width: 1.5 },
}

async function loadGraph() {
  loading.value = true
  error.value = null
  try {
    const [ent, fct] = await Promise.all([
      getEntities(scopeStore.scope),
      getFacts(scopeStore.scope),
    ])
    entities.value = ent.items
    facts.value = fct.items
    buildGraph()
    await nextTick()
    renderNetwork()
  } catch (e: any) {
    error.value = e?.message ? `Failed to load graph: ${e.message}` : String(e)
    message.error('Graph load failed')
  } finally {
    loading.value = false
  }
}

function renderNetwork() {
  if (!containerEl.value) return
  if (network.value) {
    network.value.setData({ nodes: nodes as any, edges: edges as any })
    return
  }
  network.value = new Network(
    containerEl.value,
    { nodes: nodes as any, edges: edges as any },
    networkOptions,
  )
  network.value.on('click', (params: any) => {
    if (params.nodes.length > 0) {
      selectedId.value = params.nodes[0]
    } else {
      selectedId.value = null
    }
  })
}

async function openTimeline() {
  if (!selectedId.value || !timelinePredicate.value) {
    message.warning('Pick a predicate first')
    return
  }
  drawerOpen.value = true
  timelineLoading.value = true
  timelineData.value = null
  try {
    timelineData.value = await getTimeline(
      scopeStore.scope,
      selectedId.value,
      timelinePredicate.value,
    )
  } catch (e: any) {
    message.error(`Timeline failed: ${e?.message || e}`)
  } finally {
    timelineLoading.value = false
  }
}

function fitGraph() {
  network.value?.fit({ animation: true })
}

onBeforeUnmount(() => {
  network.value?.destroy()
  network.value = null
})

// Reload when scope changes
watch(
  () => scopeStore.scope,
  () => {
    loadGraph()
  },
)
</script>

<template>
  <div class="graph-view">
    <div class="page-head">
      <h1>Knowledge Graph</h1>
      <p class="muted">
        Entities (nodes, colored by type) and facts (directed edges). Literal facts render as dashed
        labels on their subject.
      </p>
    </div>

    <NSpace align="center" style="margin-bottom: 12px">
      <NButton type="primary" :loading="loading" @click="loadGraph">Load graph</NButton>
      <NButton quaternary @click="fitGraph">Fit to view</NButton>
      <NTag v-if="entities.length" size="small">{{ entities.length }} entities</NTag>
      <NTag v-if="facts.length" size="small">{{ facts.length }} facts</NTag>
    </NSpace>

    <NAlert v-if="error" type="error" :show-icon="true" style="margin-bottom: 12px">{{ error }}</NAlert>

    <div class="graph-layout">
      <NCard size="small" class="graph-card">
        <div class="graph-canvas-wrap">
          <NSpin v-if="loading" class="overlay-spin" />
          <div ref="containerEl" class="graph-canvas"></div>
          <NEmpty
            v-if="!loading && entities.length === 0"
            description="No entities yet — ingest an experience first, or load the graph."
            class="overlay-empty"
          />
        </div>
        <div class="legend">
          <span v-for="(color, type) in TYPE_COLORS" :key="type" class="legend-item">
            <span class="dot" :style="{ background: color }"></span>{{ type }}
          </span>
        </div>
      </NCard>

      <NCard size="small" class="side-panel" :title="selectedEntity ? selectedEntity.canonical_name : 'Entity'">
        <template #header-extra>
          <NTag v-if="selectedEntity" size="small" round>{{ selectedEntity.entity_type }}</NTag>
        </template>
        <div v-if="!selectedEntity" class="muted">
          <NEmpty description="Click a node to see its facts and explore timelines." />
        </div>
        <div v-else>
          <NDescriptions v-if="selectedEntity.description" :column="1" size="small" label-placement="left" bordered>
            <NDescriptionsItem label="Description">{{ selectedEntity.description }}</NDescriptionsItem>
            <NDescriptionsItem label="ID"><code>{{ selectedEntity.entity_id }}</code></NDescriptionsItem>
          </NDescriptions>

          <div class="section-title">Facts ({{ selectedFacts.length }})</div>
          <div v-if="selectedFacts.length === 0" class="muted small">No facts reference this entity.</div>
          <ul v-else class="fact-list">
            <li v-for="sf in selectedFacts" :key="sf.fact.fact_id + '-' + sf.direction" class="fact-item">
              <NTag
                size="tiny"
                :type="sf.direction === 'out' ? 'success' : 'warning'"
                round
                :bordered="false"
                class="dir-tag"
                :title="sf.direction === 'out' ? 'outgoing: this node is the subject' : 'incoming: this node is the object'"
              >
                {{ sf.direction === 'out' ? '→ out' : '← in' }}
              </NTag>
              <template v-if="sf.direction === 'out'">
                <span class="pred">{{ sf.fact.predicate }}</span>
                <span class="arrow">→</span>
                <span class="obj">{{ sf.fact.object.value }}</span>
              </template>
              <template v-else>
                <!-- incoming:展示另一端的主语 → 本节点 -->
                <span class="obj">{{ sf.fact.subject.name }}</span>
                <span class="arrow">→</span>
                <span class="pred">{{ sf.fact.predicate }}</span>
              </template>
              <NTag size="tiny" :type="sf.fact.object.datatype === 'entity' ? 'info' : 'default'" round>
                {{ sf.fact.object.datatype }}
              </NTag>
              <span class="conf">{{ Math.round(sf.fact.confidence * 100) }}%</span>
            </li>
          </ul>

          <div class="section-title">Timeline</div>
          <p class="muted small">Pick a predicate to see how its value changed over time.</p>
          <NSpace>
            <NSelect
              v-model:value="timelinePredicate"
              :options="predicateOptions"
              size="small"
              placeholder="predicate"
              style="min-width: 160px"
            />
            <NButton size="small" type="primary" ghost @click="openTimeline" :disabled="!timelinePredicate">
              Show timeline
            </NButton>
          </NSpace>
        </div>
      </NCard>
    </div>

    <NDrawer v-model:show="drawerOpen" :width="420" placement="right">
      <NDrawerContent :title="`Timeline · ${timelinePredicate}`" closable>
        <NSpin v-if="timelineLoading" />
        <div v-else-if="timelineData">
          <p class="muted small">Supersession chain (oldest → current). A null <code>valid_to</code> is the active value.</p>
          <NTimeline size="large">
            <NTimelineItem
              v-for="(v, idx) in timelineData.versions"
              :key="v.fact_id"
              :type="v.valid_to === null ? 'success' : 'default'"
              :title="v.object_value"
              :content="`${v.valid_to === null ? 'active' : 'superseded'} · conf ${(v.confidence ?? 0) * 100}%`"
              :time="`${new Date(v.valid_from).toLocaleString()}${v.valid_to ? ' → ' + new Date(v.valid_to).toLocaleString() : ' → now'}`"
            />
          </NTimeline>
        </div>
        <NEmpty v-else description="No versions" />
      </NDrawerContent>
    </NDrawer>
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
.small {
  font-size: 12px;
}
.page-head {
  margin-bottom: 16px;
}
.graph-layout {
  display: grid;
  grid-template-columns: 1fr 360px;
  gap: 16px;
  align-items: start;
}
.graph-card {
  padding: 0;
}
.graph-canvas-wrap {
  position: relative;
  height: 600px;
  background: #fafbfc;
  border-radius: 8px;
  overflow: hidden;
}
.graph-canvas {
  width: 100%;
  height: 100%;
}
.overlay-spin {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 2;
}
.overlay-empty {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
}
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  padding: 10px 4px 0;
  font-size: 12px;
  color: var(--cortex-muted);
}
.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}
.side-panel {
  min-height: 400px;
}
.section-title {
  font-weight: 600;
  margin: 16px 0 8px;
  font-size: 13px;
  color: var(--cortex-text);
}
.fact-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.fact-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  background: var(--cortex-bg);
  border-radius: 6px;
  font-size: 12px;
}
.pred {
  font-family: ui-monospace, monospace;
  color: var(--cortex-primary);
  font-weight: 600;
}
.arrow {
  color: var(--cortex-muted);
}
.obj {
  flex: 1;
}
.conf {
  font-family: ui-monospace, monospace;
  font-size: 11px;
  color: var(--cortex-muted);
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
@media (max-width: 980px) {
  .graph-layout {
    grid-template-columns: 1fr;
  }
  .graph-canvas-wrap {
    height: 420px;
  }
}
</style>
