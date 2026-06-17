import { defineStore } from 'pinia'
import { ref } from 'vue'
import { DEFAULT_SCOPE, EXAMPLE_SCOPES } from '@/api'

const STORAGE_KEY = 'cortex.scope'

/**
 * Global scope selector state, shared by the header dropdown and every
 * request-bearing view. Persists the last-used scope to localStorage.
 */
export const useScopeStore = defineStore('scope', () => {
  const stored = typeof localStorage !== 'undefined' ? localStorage.getItem(STORAGE_KEY) : null
  const scope = ref<string>(stored ?? DEFAULT_SCOPE)

  function setScope(value: string) {
    scope.value = value || DEFAULT_SCOPE
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(STORAGE_KEY, value)
    }
  }

  const presets = EXAMPLE_SCOPES

  return { scope, setScope, presets }
})
