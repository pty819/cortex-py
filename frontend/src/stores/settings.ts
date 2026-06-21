import { defineStore } from 'pinia'
import { ref } from 'vue'

/**
 * Mock 模式已移除——前端永远走 Live API。
 * useMock 固定 false,不可切换。
 */
export const useSettingsStore = defineStore('settings', () => {
  const useMock = ref<boolean>(false)
  function setUseMock(_: boolean) { /* no-op: mock removed */ }
  return { useMock, setUseMock }
})
