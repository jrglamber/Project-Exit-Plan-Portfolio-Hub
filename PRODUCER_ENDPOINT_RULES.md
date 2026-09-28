# Producer endpoint rules

The Portfolio Hub producer summary/research endpoints are display/read-only.

1. Reuse the exact existing functions/state powering each producer's live headline values.
2. Do not trigger reconciliation, broker repair, harvesting, HWM reset, maintenance or orders from a GET endpoint.
3. Return `null` when a broker-owned value cannot be established; never invent zero.
4. Keep P&L, HWM and giveback on the same strategy-owned broker basis.
5. Preserve the HWM timestamp precision already captured by the strategy.
6. `nav_gbp` is shared account NAV and must never be summed across producer services.
7. `risk_per_trade_gbp` means the currently approved risk for subsequent new trades.
8. Existing producer accounting rules remain authoritative.
9. Producer `source_build` is informational. Schema compatibility, not exact build equality, controls Hub compatibility.
10. Research/analysis endpoints exposed through the Hub remain read-only and have zero execution authority.

## Portfolio-summary contract

Each producer must expose the shared schema version expected by Portfolio Hub at `/api/portfolio-summary`.

Normal producer releases can move independently as long as that schema remains compatible. Do not pin the Hub to historical producer build numbers.

## Manual risk-control exception
Each live producer may expose `GET/POST /control/risk-per-trade`. POST must require the producer control secret, an explicit confirmation token, persist the change, and apply it to NEW trades only. Existing positions must never be resized by this control.
