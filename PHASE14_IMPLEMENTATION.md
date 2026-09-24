# Phase 14 — Frontend Dashboard

Phase 14 turns the existing React/Vite UI into an operational dashboard over the backend capabilities already built in Phases 1–13.

## Screens

- **Runs** — recent gate decisions and run details.
- **Repositories** — repository registration and approval policy controls.
- **Try the gate** — local/demo gate execution without a CI provider.
- **API keys** — issue and revoke CI authentication keys.
- **System** — live backend health, queue/worker state, durable jobs and provider capabilities.

## System screen

The new System screen polls the backend and shows:

- service/database status
- queue state, worker count and pending work
- tracing status
- durable job lifecycle counts
- recent durable jobs and retry attempts
- source and CI/CD provider capabilities

The frontend only displays capability metadata. Provider credentials remain server-side.

## Build

```bash
cd frontend
npm install
npm run build
```
