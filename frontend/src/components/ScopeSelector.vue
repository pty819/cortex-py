<script setup lang="ts">
import { NSelect, NButton, NSpace, type SelectOption } from 'naive-ui'
import { computed } from 'vue'
import { useScopeStore } from '@/stores/scope'

const store = useScopeStore()

const options = computed<SelectOption[]>(() => [
  ...store.presets.map((s) => ({ label: s, value: s })),
])

// Allow the dropdown to act as a quick-picker that fills the editable field,
// while still letting the user type a custom scope.
function onChange(value: string | null) {
  if (value != null) store.setScope(value)
}

function reset() {
  store.setScope(store.presets[0] ?? 'org:fab-a/etch:E301')
}
</script>

<template>
  <div class="scope-selector">
    <span class="scope-label">Scope</span>
    <NSelect
      :value="store.scope"
      :options="options"
      filterable
      tag
      size="small"
      placeholder="org:fab-a/etch:E301"
      style="min-width: 320px"
      @update:value="onChange"
    />
    <NButton size="small" quaternary @click="reset">Reset</NButton>
  </div>
</template>

<style scoped>
.scope-selector {
  display: flex;
  align-items: center;
  gap: 8px;
}
.scope-label {
  font-size: 12px;
  color: var(--cortex-muted);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
</style>
