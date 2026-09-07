# Project Exit Plan — Aggregate Dashboard Link-Up

## Confirmed source baselines supplied by user

- Indices live runtime: `v10.1.53` in `app_postgres_runtime.py`
- Metals: `v1.6.32`
- BCO: `v0.8.8`

The linked cumulative builds in this pack are:

- Indices `v10.1.54`
- Metals `v1.6.33`
- BCO `v0.8.9`
- Aggregate Dashboard `v0.2.0`

No production strategy rule is intentionally changed. The three producer builds add only a read-only `/api/portfolio-summary` adapter plus optional shared-secret authentication.

## Important Indices runtime note

The supplied Indices package contains an older `app.py` and the screenshot-confirmed current live runtime `app_postgres_runtime.py` at `v10.1.53`.
The patch is applied to `app_postgres_runtime.py` only. The supplied v10.1.54 Procfile has also been aligned to `app_postgres_runtime:app` so the package cannot accidentally boot the legacy `app.py`. If Railway already has a custom start command for the same runtime, keep it. Do not revert the service to legacy `app:app`.

## Deployment order

### 1. Indices
Deploy `Project-Exit-Plan-v10.1.54.zip` to the existing Indices service.

Verify:

- `/dashboard` shows `v10.1.54`
- `/health` remains healthy
- `/api/portfolio-summary` returns `strategy: indices` and `source_build: v10.1.54`
- existing live top tiles still match the broker

### 2. Metals
Deploy `Metals-v1.6.33.zip` to the existing Metals service.

Verify:

- `/dashboard` shows `v1.6.33`
- `/health` remains healthy
- `/api/portfolio-summary` returns `strategy: metals` and `source_build: v1.6.33`
- headline values remain XAU LONG live only; XAU SHORT/XAG practice stays excluded

### 3. BCO
Deploy `BCO-live-v0.8.9.zip` to the existing BCO service.

Verify:

- `/dashboard` shows `0.8.9`
- `/health` remains healthy
- `/api/portfolio-summary` returns `strategy: bco`, `source_build: 0.8.9`, and `mode: practice` while still demo/practice
- BCO execution/safety state is unchanged

### 4. Aggregate service
Create a new Railway service/repository from `Aggregate-v0.2.0.zip`.

Set these variables to the BASE URLs of the three services:

- `INDICES_SERVICE_URL=https://...`
- `METALS_SERVICE_URL=https://...`
- `BCO_SERVICE_URL=https://...`
- `AGGREGATE_LIVE_STRATEGIES=indices,metals`

Do not add OANDA credentials to the aggregate service.

### 5. Optional but recommended endpoint protection
Create one random secret and set the same value on all four services:

- `AGGREGATE_SOURCE_SECRET=<same random value>`

The aggregate service sends it in `X-Aggregate-Secret`. If the variable is blank, the summary endpoints remain read-only but publicly reachable to anyone who knows the URL.

### 6. Aggregate verification
Open:

- `/dashboard`
- `/health`
- `/api/aggregate`

Expected top order:

1. Indices — Broker P&L / High Water / Giveback / Open Trades
2. Metals — Broker P&L / High Water / Giveback / Open Trades
3. BCO — Broker P&L / High Water / Giveback / Open Trades

The Live Portfolio row below them should currently combine Indices + Metals only. BCO remains visible but its practice account NAV/P&L must not contaminate live-money totals.

When BCO is later promoted to real live execution, explicitly change:

`AGGREGATE_LIVE_STRATEGIES=indices,metals,bco`
