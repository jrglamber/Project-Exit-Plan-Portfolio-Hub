# Project Exit Plan - Portfolio Hub v0.4.0

A read-only portfolio cockpit above Indices, Metals and BCO.

## What v0.4.0 shows
- Portfolio NAV, open P&L, realised today/week/month/all-time, month total and open-risk estimate.
- Hub-observed NAV high-water and drawdown.
- Compact live strategy cards for Indices, Metals and BCO.
- Live-lane descriptions, approved risk/trade, current exit manager and basket state.
- A single condensed health bar when everything is healthy.
- A Needs Attention section that only expands when something actually needs review.
- Recent material activity: basket open/flat/reduction, realised changes, HWM resets, manager/mode changes and feed recovery/failure.
- Hub-captured hourly realised-P&L performance history for the current month.
- Compact Research & Challengers summary from the read-only analysis gateway.
- Monthly Risk Review panel. Approval remains manual-only and never resizes existing positions.
- Deep accounting, exposure and system-health detail remains available in collapsed sections.

## Safety / authority
- Portfolio Hub is read-only.
- No OANDA credentials.
- No trade placement, close, harvest, stop, sizing or strategy authority.
- Producer build numbers are informational. Schema compatibility is enforced through the shared portfolio-summary contract.

## Railway
Deploy this repository as the Portfolio Hub service.

Set:
- `INDICES_SERVICE_URL`
- `METALS_SERVICE_URL`
- `BCO_SERVICE_URL`

Each producer exposes `GET /api/portfolio-summary`.
The analysis wrapper additionally consumes the producers' read-only `/analysis/*` endpoints.

Current default live-money scope is:

`AGGREGATE_LIVE_STRATEGIES=indices,metals,bco`

If a producer is temporarily practice/demo, it remains visible in its own strategy card but is excluded from live totals unless it reports `mode=live`.

## Endpoints
- `/dashboard`
- `/api/aggregate`
- `/api/research-summary`
- `/api/analysis`
- `/health`

## Notes on history
The performance chart and NAV drawdown are Hub-observed telemetry. They start collecting when the Hub process starts and are not treated as a substitute for producer accounting. Realised accounting still comes from the producer contracts.
