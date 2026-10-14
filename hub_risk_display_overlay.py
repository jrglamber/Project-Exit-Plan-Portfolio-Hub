"""Portfolio Hub approved Metals risk display overlay.

Shows the user-approved 0.100% NAV target for Metals while keeping live-money
risk application fail-closed at the Hub boundary. Existing positions are never
resized here.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import analysis_gateway as gateway

# Presentation/planning target only. The live Metals producer remains the
# authority for any actual new-trade cash-risk setting.
gateway.core.FIXED_RISK_PCT_OF_NAV["metals"] = 0.10

app = FastAPI(title="Project Exit Plan — Portfolio Hub Risk Overlay")

@app.post("/api/risk-control/metals")
async def metals_risk_apply_blocked(request: Request):
    return JSONResponse(
        status_code=409,
        content={
            "status": "manual_action_required",
            "strategy": "metals",
            "fixed_risk_pct_of_nav": 0.10,
            "detail": "0.100% is the approved Metals target shown by the Hub, but the live Metals producer is still configured at 0.075%. Application is blocked here to prevent the Hub displaying 0.100% while actually applying 0.075%. Existing positions are unchanged.",
        },
    )

app.mount("/", gateway.app)
