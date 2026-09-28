# Project Exit Plan - Portfolio Hub v0.5.0

A read-only portfolio cockpit above Indices, Metals and BCO.

## What v0.5.0 shows
- Portfolio NAV, open P&L, realised today/week/month/all-time, month total and open-risk estimate.
- Hub-observed NAV high-water and drawdown.
- Compact live strategy cards for Indices, Metals and BCO.
- Live-lane descriptions, approved risk/trade, current exit manager and basket state.
- A single condensed health bar when everything is healthy.
- A Needs Attention section that only expands when something actually needs review.
- Recent material activity: basket open/flat/reduction, realised changes, HWM resets, manager/mode changes and feed recovery/failure.
- Compact Research & Challengers summary from the read-only analysis gateway.
- Monthly Risk Review / Risk Control panel. Authenticated manual changes apply to NEW trades only and never resize existing positions.
- Deep accounting, exposure and system-health detail remains available in collapsed sections.

## Safety / authority
- Portfolio Hub is read-only for trading/management actions except the explicit authenticated manual new-trade risk control.
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
NAV drawdown is Hub-observed telemetry and starts with the current Hub process. It is not a substitute for producer accounting. Realised accounting still comes from the producer contracts.

## v0.4.1 maintenance
- Preserve unavailable/null accounting fields as unavailable in the dashboard instead of rendering them as £0.00.

## v0.5.0
- Removes the Portfolio performance chart.
- Adds authenticated manual risk-per-new-trade controls for Indices, XAU LONG live, and BCO.
- Control secrets are prompted at use time and are not stored by the Hub.
