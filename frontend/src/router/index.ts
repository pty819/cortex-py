import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/ingest' },
  {
    path: '/ingest',
    name: 'ingest',
    component: () => import('@/views/IngestView.vue'),
    meta: { title: 'Ingest' },
  },
  {
    path: '/graph',
    name: 'graph',
    component: () => import('@/views/GraphView.vue'),
    meta: { title: 'Knowledge Graph' },
  },
  {
    path: '/qa',
    name: 'qa',
    component: () => import('@/views/QaView.vue'),
    meta: { title: 'Ask' },
  },
  {
    path: '/browse',
    name: 'browse',
    component: () => import('@/views/BrowseView.vue'),
    meta: { title: 'Browse' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  const title = (to.meta.title as string) || 'Cortex'
  document.title = `${title} · Cortex`
})

export default router
