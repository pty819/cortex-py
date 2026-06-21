<script setup lang="ts">
import {
  NTabs,
  NTabPane,
  NDataTable,
  NButton,
  NSpace,
  NTag,
  NEmpty,
  NCode,
  type DataTableColumns,
} from 'naive-ui'
import { computed, h, ref, watch } from 'vue'
import { getFacts, getEntities } from '@/api'
import { useScopeStore } from '@/stores/scope'
import { useSettingsStore } from '@/stores/settings'
import type { Entity, Fact } from '@/types'

const scopeStore = useScopeStore()
const settings = useSettingsStore()

const facts = ref<Fact[]>([])
const entities = ref<Entity[]>([])
const loading = ref(false)
const page = ref(1)
const pageSize = ref(10)

// Events / beliefs aren't separate list endpoints in the contract — we synthesize
// a lightweight "events" view by reading recent facts as event-ish rows and a
// static "beliefs" placeholder. This keeps Browse useful with the given API.

async function load() {
  loading.value = true
  try {
    const [f, e] = await Promise.all([
      getFacts(scopeStore.scope),
      getEntities(scopeStore.scope),
    ])
    facts.value = f.items
    entities.value = e.items
  } finally {
    loading.value = false
  }
}

watch(() => scopeStore.scope, load, { immediate: true })

// ---- Columns ----
const factColumns = computed<DataTableColumns<Fact>>(() => [
  {
    title: 'Subject',
    key: 'subject',
    render: (row) => h('span', { class: 'cell-emph' }, row.subject.name),
    width: 140,
  },
  {
    title: 'Predicate',
    key: 'predicate',
    render: (row) =>
      h(NTag, { size: 'small', type: 'info', round: true }, { default: () => row.predicate }),
    width: 140,
  },
  { title: 'Object', key: 'object_value', render: (row) => row.object.value },
  {
    title: 'Type',
    key: 'object_datatype',
    render: (row) =>
      h(NTag, { size: 'tiny', bordered: false }, { default: () => row.object.datatype }),
    width: 90,
  },
  {
    title: 'Conf',
    key: 'confidence',
    render: (row) => `${Math.round(row.confidence * 100)}%`,
    width: 70,
  },
  {
    title: 'Valid from',
    key: 'valid_from',
    render: (row) => new Date(row.valid_from).toLocaleDateString(),
    width: 120,
  },
  {
    title: 'Active',
    key: 'valid_to',
    render: (row) =>
      row.valid_to === null
        ? h(NTag, { size: 'tiny', type: 'success', round: true }, { default: () => 'active' })
        : h(NTag, { size: 'tiny', round: true }, { default: () => 'superseded' }),
    width: 110,
  },
])

const entityColumns = computed<DataTableColumns<Entity>>(() => [
  { title: 'Name', key: 'canonical_name', render: (row) => h('span', { class: 'cell-emph' }, row.canonical_name) },
  {
    title: 'Type',
    key: 'entity_type',
    render: (row) =>
      h(NTag, { size: 'small', round: true }, { default: () => row.entity_type }),
    width: 120,
  },
  { title: 'Description', key: 'description', ellipsis: { tooltip: true } },
  { title: 'ID', key: 'entity_id', render: (row) => h('code', row.entity_id), width: 160 },
])

// "Events" as a derived recent-activity view (latest facts by valid_from).
const eventRows = computed(() =>
  [...facts.value]
    .sort((a, b) => +new Date(b.valid_from) - +new Date(a.valid_from))
    .map((f) => ({
      fact_id: f.fact_id,
      ts: f.valid_from,
      summary: `${f.subject.name} ${f.predicate} ${f.object.value}`,
      conf: f.confidence,
    })),
)

const eventColumns: DataTableColumns = [
  { title: 'When', key: 'ts', render: (row: any) => new Date(row.ts).toLocaleString(), width: 200 },
  { title: 'What', key: 'summary' },
  { title: 'Conf', key: 'conf', render: (row: any) => `${Math.round(row.conf * 100)}%`, width: 70 },
]

const beliefs = ref([
  { id: 'b1', text: 'Q3 Renewal is at risk until countersigned.', source: 'derived' },
  { id: 'b2', text: 'Priya is the primary owner for Acme enterprise renewals.', source: 'derived' },
])

const rowCount = computed(() => (tab: string) => {
  if (tab === 'facts') return facts.value.length
  if (tab === 'entities') return entities.value.length
  if (tab === 'events') return eventRows.value.length
  return beliefs.value.length
})
</script>

<template>
  <div class="browse-view">
    <div class="page-head">
      <h1>Browse</h1>
      <p class="muted">Tab through facts, entities, and derived events for the current scope.</p>
    </div>

    <NSpace align="center" style="margin-bottom: 12px">
      <NButton :loading="loading" type="primary" ghost @click="load">Refresh</NButton>
      <NTag size="small">{{ scopeStore.scope }}</NTag>
    </NSpace>

    <NTabs type="line" animated>
      <NTabPane name="facts" :tab="`Facts (${facts.length})`">
        <NDataTable
          :columns="factColumns"
          :data="facts"
          :loading="loading"
          :pagination="{ page, pageSize, showSizePicker: true, pageSizes: [10, 20, 50], onChange: (p: number) => (page = p), onUpdatePageSize: (s: number) => (pageSize = s) }"
          :bordered="false"
          size="small"
        />
      </NTabPane>

      <NTabPane name="entities" :tab="`Entities (${entities.length})`">
        <NDataTable
          :columns="entityColumns"
          :data="entities"
          :loading="loading"
          :pagination="{ page, pageSize, showSizePicker: true, pageSizes: [10, 20, 50], onChange: (p: number) => (page = p), onUpdatePageSize: (s: number) => (pageSize = s) }"
          :bordered="false"
          size="small"
        />
      </NTabPane>

      <NTabPane name="events" :tab="`Events (${eventRows.length})`">
        <p class="muted small" style="margin-top: 0">
          Derived from most-recent facts (the contract exposes no dedicated events list endpoint).
        </p>
        <NDataTable
          :columns="eventColumns"
          :data="eventRows"
          :loading="loading"
          :pagination="{ page, pageSize, onChange: (p: number) => (page = p) }"
          :bordered="false"
          size="small"
        />
      </NTabPane>

      <NTabPane name="beliefs" :tab="`Beliefs (${beliefs.length})`">
        <NEmpty v-if="beliefs.length === 0" description="No beliefs recorded." />
        <ul v-else class="belief-list">
          <li v-for="b in beliefs" :key="b.id" class="belief-item">
            <NTag size="tiny" round>{{ b.source }}</NTag>
            <span>{{ b.text }}</span>
          </li>
        </ul>
      </NTabPane>
    </NTabs>
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
.belief-list {
  list-style: none;
  padding: 0;
  margin: 12px 0 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.belief-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: var(--cortex-bg);
  border-radius: 8px;
  font-size: 13px;
}
:deep(.cell-emph) {
  font-weight: 600;
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
}
</style>
