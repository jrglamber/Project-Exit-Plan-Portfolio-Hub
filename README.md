# Project Exit Plan — Aggregate Dashboard v0.2.0

A fourth, read-only Railway service above Indices, Metals and BCO.

## v0.2.0
- Three live strategy blocks at the top.
- Basket P&L, High Water, Giveback, Open Trades.
- Status/mode/freshness for each source.
- Compact portfolio strip immediately below.
- Last-known-good cache on temporary source failure.
- Stale/unavailable data is never silently changed to zero.
- Shared broker NAV is never summed across strategy services.
- No OANDA credentials and no trade write controls.

## Railway
Deploy this folder/repository as a new service.

Set:
- `INDICES_SERVICE_URL`
- `METALS_SERVICE_URL`
- `BCO_SERVICE_URL`

Each source will expose `GET /api/portfolio-summary`.

Do not put OANDA credentials in the aggregate service.

## Endpoints
- `/dashboard`
- `/api/aggregate`
- `/health`

See `portfolio_summary_contract.example.json` and `PRODUCER_ENDPOINT_RULES.md`.

## Locked linked producer builds — 06 Sep 2026

The aggregate service expects these current producer builds:

- Indices: `v10.1.54`
- Metals: `v1.6.33`
- BCO: `0.8.9`

If a connected `/api/portfolio-summary` reports a different `source_build`,
the dashboard shows **BUILD MISMATCH** rather than silently accepting it.

These expected values can be overridden with:
`INDICES_EXPECTED_BUILD`, `METALS_EXPECTED_BUILD`, `BCO_EXPECTED_BUILD`.

## v0.2.0 linked producer builds

- Indices `v10.1.54` — cumulative on `v10.1.53`
- Metals `v1.6.33` — cumulative on `v1.6.32`
- BCO `0.8.9` — cumulative on `0.8.8`

Combined live Portfolio NAV/P&L/risk defaults to `indices,metals`. BCO is still
shown in its own top block but is excluded from combined live-money totals while
its producer reports PRACTICE/DEMO.

When BCO is explicitly promoted to live, set:

`AGGREGATE_LIVE_STRATEGIES=indices,metals,bco`
