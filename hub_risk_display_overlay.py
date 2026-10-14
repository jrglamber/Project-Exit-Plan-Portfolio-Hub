"""Portfolio Hub Metals risk/policy presentation overlay.

Approved 2026-10-14 live-XAU policy:
- base risk target 0.100% NAV for normal new XAU entries;
- adaptive entries after campaign trade #32 are half-size (0.050% NAV target);
- XAU LONG and XAU SHORT are both live lanes;
- adaptive stacking: first 20 normal, 21+ requires >= +2R campaign funding,
  no strategic hard cap, half-size after #32;
- live-XAU harvest ladder: 30R, 80R, 130R, then +50R, banking 20/20/25%+;
- existing positions are never resized by the monthly risk button.

The authenticated POST remains owned by the producer through analysis_gateway;
this overlay does not bypass that control boundary.
"""
from __future__ import annotations

import inspect
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

import analysis_gateway as gateway

BASE_RISK_PCT = 0.10
OVERFLOW_RISK_PCT = 0.05
POLICY_VERSION = "hub_metals_adaptive_risk_0p100_v1_2026_10_14"

# Presentation/planning policy. The producer is still authoritative for the
# persisted GBP risk amount applied to subsequent live broker entries.
gateway.core.FIXED_RISK_PCT_OF_NAV["metals"] = BASE_RISK_PCT
gateway.core.STRATEGY_LANE_NOTES["metals"] = (
    "XAU LONG + SHORT live · adaptive stack: first 20 normal, 21+ needs +2R campaign funding, "
    "half-size after #32 · harvest 30/80/130R+"
)
# Visible Hub version only; producer contract/schema is unchanged.
gateway.core.APP_VERSION = "0.6.4"

app = FastAPI(title="Project Exit Plan — Portfolio Hub Risk Overlay")


def _core_root_endpoint() -> Any:
    for route in list(getattr(gateway.core.app, "routes", [])):
        if str(getattr(route, "path", "")) == "/" and "GET" in set(getattr(route, "methods", []) or []):
            endpoint = getattr(route, "endpoint", None)
            if callable(endpoint):
                return endpoint
    raise RuntimeError("Portfolio Hub core dashboard route not found")


_CORE_ROOT = _core_root_endpoint()


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Render the existing dashboard with Metals policy wording brought current."""
    result = _CORE_ROOT()
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, Response):
        raw = bytes(getattr(result, "body", b"") or b"").decode("utf-8", errors="replace")
        status_code = int(getattr(result, "status_code", 200) or 200)
    else:
        raw = str(result)
        status_code = 200

    # Keep this transformation deliberately narrow: no portfolio calculations
    # are changed here; only stale Metals wording in the static dashboard JS.
    raw = raw.replace("XAU LONG live", "XAU LONG + SHORT live")
    raw = raw.replace(
        "Agreed fixed percentages · button recalculates from fresh live NAV · NEW trades only",
        "Agreed base-risk percentages · Metals uses 0.100% normally and 0.050% after campaign trade #32 · button recalculates from fresh live NAV · NEW trades only",
    )
    raw = raw.replace(
        "If broker minimum size implies more cash risk than the target, the producer keeps the minimum-size execution rule and records the effective risk.",
        "If broker minimum size implies more cash risk than the target, the producer keeps the minimum-size execution rule and records the effective risk. Metals adaptive overflow entries after #32 are half the displayed base target.",
    )
    return HTMLResponse(content=raw, status_code=status_code)


@app.get("/api/hub-risk-policy")
def hub_risk_policy() -> dict:
    return {
        "status": "ok",
        "version": POLICY_VERSION,
        "metals": {
            "live_lanes": ["XAU_LONG", "XAU_SHORT"],
            "base_risk_pct_of_nav": BASE_RISK_PCT,
            "overflow_after_trade_32_pct_of_nav": OVERFLOW_RISK_PCT,
            "normal_allowance": 20,
            "overflow_min_campaign_r": 2.0,
            "strategic_hard_cap": None,
            "harvest_levels_r_example": [30, 80, 130, 180, 230],
            "harvest_fractions": [0.20, 0.20, 0.25],
            "existing_positions_resized_by_apply": False,
            "producer_is_execution_authority": True,
        },
    }

# Everything else, including authenticated /api/risk-control/{strategy}, is
# delegated unchanged to the analysis gateway. This removes the temporary
# Metals-only 409 block now that the producer target is being promoted to 0.100%.
app.mount("/", gateway.app)
