<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
import { NConfigProvider, NMessageProvider, NDialogProvider, darkTheme, zhCN, dateEnUS } from 'naive-ui'
import { computed } from 'vue'
import ScopeSelector from '@/components/ScopeSelector.vue'
import { useSettingsStore } from '@/stores/settings'

const settings = useSettingsStore()

// Light theme per the spec. We keep darkTheme imported so it's easy to switch later.
void darkTheme
void zhCN
const theme = computed(() => null)
const dateLocale = dateEnUS

const navItems = [
  { to: '/ingest', label: 'Ingest' },
  { to: '/graph', label: 'Knowledge Graph' },
  { to: '/qa', label: 'Ask' },
  { to: '/browse', label: 'Browse' },
]
</script>

<template>
  <NConfigProvider :theme="theme" :date-locale="dateLocale">
    <NMessageProvider>
      <NDialogProvider>
        <div class="app-shell">
          <header class="app-header">
            <div class="header-left">
              <RouterLink to="/" class="brand">
                <span class="brand-icon">🧠</span>
                <span class="brand-text">Cortex</span>
              </RouterLink>
              <nav class="nav">
                <RouterLink
                  v-for="item in navItems"
                  :key="item.to"
                  :to="item.to"
                  class="nav-link"
                  active-class="nav-link-active"
                >
                  {{ item.label }}
                </RouterLink>
              </nav>
            </div>
            <div class="header-right">
              <ScopeSelector />
              <NTag
                :type="settings.useMock ? 'warning' : 'success'"
                size="small"
                round
                checkable
                :checked="settings.useMock"
                @update:checked="settings.setUseMock($event)"
                title="Toggle mock data when the backend is offline"
              >
                {{ settings.useMock ? 'Mock data' : 'Live API' }}
              </NTag>
            </div>
          </header>

          <main class="app-main">
            <RouterView />
          </main>

          <footer class="app-footer">
            <span>Cortex · knowledge-graph memory system</span>
            <span class="footer-hint">Backend: http://localhost:8000 · proxy /v1</span>
          </footer>
        </div>
      </NDialogProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>

<style>
:root {
  --cortex-bg: #f6f8fb;
  --cortex-surface: #ffffff;
  --cortex-border: #e5e9f0;
  --cortex-text: #1f2933;
  --cortex-muted: #6b7280;
  --cortex-primary: #3b82f6;
  --cortex-accent: #8b5cf6;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial,
    sans-serif, 'Apple Color Emoji', 'Segoe UI Emoji';
  color: var(--cortex-text);
  background: var(--cortex-bg);
}

* {
  box-sizing: border-box;
}

html,
body,
#app {
  margin: 0;
  padding: 0;
  height: 100%;
}

.app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 0 24px;
  height: 60px;
  background: var(--cortex-surface);
  border-bottom: 1px solid var(--cortex-border);
  position: sticky;
  top: 0;
  z-index: 10;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 24px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  text-decoration: none;
  color: var(--cortex-text);
  font-weight: 700;
  font-size: 18px;
}

.brand-icon {
  font-size: 22px;
}

.nav {
  display: flex;
  gap: 4px;
}

.nav-link {
  text-decoration: none;
  color: var(--cortex-muted);
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 500;
  transition: background 0.15s, color 0.15s;
}

.nav-link:hover {
  background: var(--cortex-bg);
  color: var(--cortex-text);
}

.nav-link-active {
  background: #eff6ff;
  color: var(--cortex-primary);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.app-main {
  flex: 1;
  padding: 24px;
  max-width: 1400px;
  width: 100%;
  margin: 0 auto;
}

.app-footer {
  padding: 12px 24px;
  border-top: 1px solid var(--cortex-border);
  background: var(--cortex-surface);
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  color: var(--cortex-muted);
}

.footer-hint {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

@media (max-width: 720px) {
  .header-left {
    gap: 12px;
  }
  .nav {
    gap: 0;
  }
  .nav-link {
    padding: 8px 6px;
    font-size: 13px;
  }
  .brand-text {
    display: none;
  }
  .app-main {
    padding: 16px;
  }
}
</style>
