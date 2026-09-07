# Producer endpoint rules

The endpoint added to Indices, Metals and BCO is display/read-only.

1. Reuse the exact existing functions/state powering that service's live top tiles.
2. Do not trigger reconciliation, broker repair, harvesting, HWM reset, maintenance or orders from GET.
3. Return `null` when a broker-owned value cannot be established; never invent zero.
4. Keep P&L/HWM/giveback on the same strategy-owned broker basis.
5. Preserve the exact HWM timestamp precision already captured by the strategy.
6. `nav_gbp` is shared account NAV and is not added together by the aggregate service.
7. `risk_per_trade_gbp` means the currently approved risk for subsequent new trades.
8. Existing accounting rules remain authoritative.
9. Stage 1 adds no control/write path.

## Current codebases to patch

Do not patch an older branch/build. The endpoint work must be applied to:

- Indices `v10.1.54` (cumulative on `v10.1.53`)
- Metals `v1.6.33` (cumulative on `v1.6.32`)
- BCO `0.8.9` (cumulative on `0.8.8`)

The endpoint's `source_build` field must report that exact running build.
