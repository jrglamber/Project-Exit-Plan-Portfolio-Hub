# Project Exit Plan - Portfolio Hub deployment checklist

## Scope

Portfolio Hub is a read-only cockpit above the live Indices, Metals and BCO producers.

Producer build numbers are informational only. Compatibility is enforced through the shared `/api/portfolio-summary` schema contract, so normal producer version bumps do not require a Hub code change.

## Required service variables

- `INDICES_SERVICE_URL=https://...`
- `METALS_SERVICE_URL=https://...`
- `BCO_SERVICE_URL=https://...`
- `AGGREGATE_LIVE_STRATEGIES=indices,metals,bco`

Optional:

- `AGGREGATE_SOURCE_SECRET=<shared read-only summary secret>`
- lane-note overrides from `env.example`
- signal-health and NAV-drift thresholds from `env.example`

Do not add OANDA credentials to Portfolio Hub.

## Post-deploy verification

Open:

- `/dashboard`
- `/health`
- `/api/aggregate`
- `/api/research-summary`

Verify:

1. Dashboard reports Portfolio Hub `v0.6.0`.
2. Indices, Metals and BCO producer cards all load and report the expected modes.
3. Live Portfolio includes only producers that are both in the configured live scope and currently report `mode=live`.
4. NAV is taken from the freshest live producer snapshot and is not summed across shared-account producers.
5. Accounting totals are live-strategy only.
6. Needs Attention is empty when sources, signals, schema and broker/database/worker health are normal.
7. Research & Challengers is read-only and cannot influence execution.
8. Fixed-percent risk control requires the producer control secret, recalculates from fresh live NAV (Indices 0.05%, XAU LONG 0.075%, BCO 0.10%), changes NEW trades only, honours producer minimum-position sizing, and never resizes existing positions.
9. NAV drawdown is labelled as Hub-observed telemetry and begins with the current Hub process.

## Safety

Portfolio Hub must not place orders, close trades, move stops, harvest, reset HWM or alter strategy rules. The sole write exception is the authenticated manual risk-per-new-trade proxy explicitly requested by the user.
