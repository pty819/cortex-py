import { defineStore } from 'pinia'
import { ref } from 'vue'

/**
 * Dev-mode toggle: when the FastAPI backend is unreachable, fall back to mock
 * data so the UI is still explorable. Persisted in localStorage.
 */
const STORAGE_KEY = 'cortex.useMock'

export const useSettingsStore = defineStore('settings', () => {
  const stored =
    typeof localStorage !== 'undefined' ? localStorage.getItem(STORAGE_KEY) : null
  const useMock = ref<boolean>(stored === null ? false : stored === 'true')

  function setUseMock(value: boolean) {
    useMock.value = value
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(STORAGE_KEY, String(value))
    }
  }

  return { useMock, setUseMock }
})
