import { defineStore } from 'pinia'
import { ref } from 'vue'
import { DEFAULT_SCOPE, EXAMPLE_SCOPES, fetchScopes } from '@/api'

const STORAGE_KEY = 'cortex.scope'

/**
 * Global scope selector state. Presets are fetched dynamically from the DB
 * via GET /v1/scopes/list (falls back to hardcoded if API unreachable).
 */
export const useScopeStore = defineStore('scope', () => {
  const stored = typeof localStorage !== 'undefined' ? localStorage.getItem(STORAGE_KEY) : null
  const scope = ref<string>(stored ?? DEFAULT_SCOPE)
  const presets = ref<string[]>(EXAMPLE_SCOPES)

  // 启动时从 DB 动态拉 scope 列表
  fetchScopes().then((s) => { presets.value = s })

  function setScope(value: string) {
    scope.value = value || DEFAULT_SCOPE
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(STORAGE_KEY, value)
    }
  }

  async function refreshPresets() {
    presets.value = await fetchScopes()
  }

  return { scope, setScope, presets, refreshPresets }
})
