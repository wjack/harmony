# Harmony web

The SPA: Vite, React 19, TypeScript (strict), Tailwind v4, TanStack Query, React Router,
Zustand. FastAPI serves the production build from `web/dist` on the same origin as `/api`.

```sh
make dev          # api, worker and this dev server (http://localhost:5173, proxies /api)
make web-ci       # frozen install, typecheck, lint, test, build
make gen-api      # regenerate src/api/schema.d.ts from FastAPI's OpenAPI document
```

`pnpm -C web dev` proxies `/api` to `http://127.0.0.1:8000`; set `HARMONY_API_ORIGIN` to
point it elsewhere. pnpm only resolves versions published at least seven days ago
(`pnpm-workspace.yaml`); dependencies are pinned exactly.

## Layout

- `src/api/`: the typed client. `schema.d.ts` is generated, never edited.
  `pending.ts` holds hand-written contracts for routes not yet in the generated schema;
  a generated path always wins, so drift shows up as a type error. Delete pending entries
  once `make gen-api` covers them.
- `src/realtime/dispatcher.ts`: one EventSource per tab, opened at
  `/api/events?after=<cursor>` from `GET /api/session`'s `X-Harmony-Cursor`. It applies
  versioned entity snapshots to the query cache, sends demo notices and stream deltas to
  subscribers, resumes after drops without gaps, and on an account switch closes the
  stream, clears the cache and reopens from a fresh cursor. The visitor's other tabs
  follow the switch in place through a `BroadcastChannel` (ARCHITECTURE.md §8).
- `src/app/`: providers, query hooks and routes.
- `src/shell/`, `src/chat/`, `src/cards/`, `src/requests/`, `src/network/`,
  `src/customize/` and `src/demo/`: the screens, one folder for each area of the app.
