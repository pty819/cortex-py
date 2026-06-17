# Cortex Frontend

A Vue 3 + Vite frontend for **Cortex**, a knowledge-graph memory system. It talks to
the FastAPI backend at `http://localhost:8000` via a Vite dev proxy, and falls
back to built-in mock data when the backend is offline.

## Stack

- **Vue 3** (`<script setup>` + Composition API + TypeScript)
- **Vite 7** with a `/v1 → http://localhost:8000` dev proxy
- **Pinia** (state) + **Vue Router** (4 routes)
- **Naive UI** (light theme)
- **vis-network** (knowledge-graph visualization)
- **axios** (HTTP) + native **EventSource** (SSE lifecycle stream)

## Quick start

```bash
npm install
npm run dev
```

App: http://localhost:5173

The backend is **not** required to run the dev server. If it is up on
`http://localhost:8000`, all `/v1` requests are proxied to it automatically.

### Build

```bash
npm run build   # type-checks (vue-tsc) + builds to dist/
npm run preview # serves the built bundle
```

## Modes: Live API vs Mock data

The header has a toggle (top-right):

- **Live API** (default) — every request hits the FastAPI backend through the
  `/v1` proxy.
- **Mock data** — uses built-in fixtures so you can click through every page
  with no backend running. The Ingest page will simulate
  `captured → extracted → indexed` lifecycle frames locally.

Toggle to **Mock data** if you see "waiting for backend" and don't want to
start the server.

## Auth (hardcoded for the demo)

Per the API contract, every request carries:

- `Authorization: Bearer dev-key`
- `X-Cortex-Actor: user:alice`

These are set once in `src/api/index.ts`. The scope is taken from the header's
scope selector (defaults to `org:acme/dept:sales/user:alice`) and persisted to
`localStorage`.

> Note: `EventSource` cannot send custom headers. The lifecycle SSE works when
> the backend is lenient on `/v1/lifecycle/stream` or when the proxy injects
> headers. In dev, if SSE fails the UI shows "waiting for backend" rather than
> crashing — toggle Mock data to demo the lifecycle panel.

## Routes

| Path       | Page              | What it does                                                                                  |
| ---------- | ----------------- | --------------------------------------------------------------------------------------------- |
| `/ingest`  | Ingest            | Submit a single experience → POST `/experience`, then a live SSE panel of lifecycle frames.    |
| `/graph`   | Knowledge Graph   | vis-network graph from `/entities` + `/facts`; node side-panel; timeline drawer for a fact.   |
| `/qa`      | Ask               | POST `/answer`; answer text with clickable `[n]` citation chips + collapsible raw pack JSON.  |
| `/browse`  | Browse            | Paginated tables for Facts / Entities / Events (derived) / Beliefs.                           |

## Project layout

```
src/
  api/
    index.ts        # axios client + SSE helper (auth, scope handled here)
    mock.ts         # dev fixtures
  components/
    ScopeSelector.vue
  router/
    index.ts
  stores/
    scope.ts        # shared scope (header dropdown) — persisted
    settings.ts     # live/mock toggle — persisted
  types/
    index.ts        # request/response types matching the /v1 contract
  views/
    IngestView.vue
    GraphView.vue
    QaView.vue
    BrowseView.vue
  App.vue
  main.ts
```

## API contract reference

All endpoints below are under `/v1`. See `src/types/index.ts` for the exact
shapes used by the UI.

- `POST /experience` — ingest one experience (only write)
- `GET  /lifecycle/stream?event_id=<id>` — SSE: `captured | extracted | indexed | failed`
- `GET  /entities?scope=<scope>`
- `GET  /facts?scope=<scope>`
- `GET  /facts/timeline?scope=<scope>&subject=<id>&predicate=<pred>`
- `POST /recall` — hybrid retrieval → StratifiedPack
- `POST /answer` — recall + LLM answer with citations
