"""Portfolio Hub Metals risk/policy presentation overlay.

Approved 2026-10-14 live-XAU policy:
- base risk target 0.100% NAV for normal new XAU entries;
- after campaign trade #32 the target is ~0.050% NAV average incremental exposure;
- while OANDA's XAU minimum size prevents a true half-size ticket, the producer
  implements that target by taking every other otherwise-qualified overflow
  opportunity at the normal valid broker size;
- XAU LONG and XAU SHORT are both live lanes;
- adaptive stacking: first 20 normal, 21+ requires >= +2R campaign funding,
  no strategic hard cap;
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
OVERFLOW_AVERAGE_RISK_PCT = 0.05
POLICY_VERSION = "hub_metals_adaptive_risk_0p100_v5_2026_10_14"

# Presentation/planning policy. The producer is still authoritative for the
# persisted GBP risk amount applied to subsequent live broker entries.
gateway.core.FIXED_RISK_PCT_OF_NAV["metals"] = BASE_RISK_PCT
gateway.core.STRATEGY_LANE_NOTES["metals"] = (
    "XAU LONG + SHORT live · adaptive stack: first 20 normal, 21+ needs +2R campaign funding, "
    "post-32 half average exposure (broker-safe alternating overflow while minimum size binds) · "
    "harvest 30/80/130R+"
)
# Visible Hub version only; producer contract/schema is unchanged.
gateway.core.APP_VERSION = "0.6.8"

app = FastAPI(title="Project Exit Plan — Portfolio Hub Risk Overlay")


@app.on_event("startup")
async def start_wrapped_gateway() -> None:
    """Forward lifecycle into the mounted analysis gateway and core Hub.

    A mounted FastAPI sub-application does not reliably receive its own startup
    lifecycle from the outer app. Without this explicit forwarding, neither the
    core portfolio-summary poller nor the analysis/research poller starts, which
    leaves the aggregate cache empty even though all producers are healthy.
    """
    await gateway.app.router.startup()


@app.on_event("shutdown")
async def stop_wrapped_gateway() -> None:
    await gateway.app.router.shutdown()


def _core_root_endpoint() -> Any:
    for route in list(getattr(gateway.core.app, "routes", [])):
        if str(getattr(route, "path", "")) == "/" and "GET" in set(getattr(route, "methods", []) or []):
            endpoint = getattr(route, "endpoint", None)
            if callable(endpoint):
                return endpoint
    raise RuntimeError("Portfolio Hub core dashboard route not found")


_CORE_ROOT = _core_root_endpoint()


def _f(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


@app.get("/api/metals-risk-live-details")
def metals_risk_live_details() -> dict:
    """Read-only producer sizing snapshot for accurate Hub risk presentation."""
    try:
        raw = dict(gateway._risk_control_fetch("metals", "GET") or {})
        if str(raw.get("status") or "").lower() == "error":
            return {
                "status": "error",
                "detail": str(raw.get("detail") or raw.get("error") or "Metals risk-control producer returned an error"),
                "http_status": raw.get("http_status"),
            }
        sp = dict(raw.get("current_sizing_preview") or raw.get("sizing_preview") or {})
        nav = _f(raw.get("nav_gbp"))
        effective = _f(sp.get("estimated_risk_gbp"))
        return {
            "status": "ok",
            "fixed_risk_pct_of_nav": _f(raw.get("fixed_risk_pct_of_nav")),
            "nav_gbp": nav,
            "current_requested_risk_gbp": _f(raw.get("current_risk_per_trade_gbp")),
            "calculated_target_gbp": _f(raw.get("calculated_risk_per_trade_gbp")),
            "raw_formula_target_gbp": _f(raw.get("raw_calculated_risk_gbp")),
            "broker_units_preview": _f(sp.get("units")),
            "minimum_trade_size": _f(sp.get("minimum_trade_size")),
            "estimated_effective_risk_gbp": effective,
            "estimated_effective_risk_pct_of_nav": (effective / nav * 100.0) if effective is not None and nav else None,
            "estimated_risk_overage_pct": _f(sp.get("estimated_risk_overage_pct")),
            "base_executable": sp.get("executable"),
            "blocking_reasons": list(sp.get("blocking_reasons") or []),
            "overflow_average_risk_pct_of_nav": OVERFLOW_AVERAGE_RISK_PCT,
            "overflow_execution_mode": "EVERY_OTHER_QUALIFIED_OVERFLOW_AT_NORMAL_VALID_SIZE_WHILE_MINIMUM_BINDS",
            "existing_positions_resized": False,
        }
    except Exception as exc:
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Render the existing dashboard with Metals policy and live sizing brought current."""
    result = _CORE_ROOT()
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, Response):
        raw = bytes(getattr(result, "body", b"") or b"").decode("utf-8", errors="replace")
        status_code = int(getattr(result, "status_code", 200) or 200)
    else:
        raw = str(result)
        status_code = 200

    raw = raw.replace("v0.6.1 &middot; portfolio cockpit", "v0.6.8 &middot; portfolio cockpit", 1)
    raw = raw.replace("XAU LONG live", "XAU LONG + SHORT live")
    raw = raw.replace(
        "Agreed fixed percentages · button recalculates from fresh live NAV · NEW trades only",
        "Agreed base-risk percentages · Metals base target 0.100%; post-32 target is ~0.050% average incremental exposure · button recalculates from fresh live NAV · NEW trades only",
    )
    raw = raw.replace(
        "If broker minimum size implies more cash risk than the target, the producer keeps the minimum-size execution rule and records the effective risk.",
        "If broker minimum size implies more cash risk than the target, the producer keeps the minimum-size execution rule and records the effective risk. Metals post-32 half exposure is broker-safe: while a true half-size ticket is below minimum, every other otherwise-qualified overflow opportunity is taken at the normal valid size.",
    )
    raw = raw.replace(
        "s.ok&&!s.stale&&s.contract_match!==false?'OK':s.contract_match===false?'SCHEMA MISMATCH':s.stale?'STALE':'DEGRADED'",
        "!s.data?'UNAVAILABLE':s.ok&&!s.stale&&s.contract_match!==false?'OK':s.contract_match===false?'SCHEMA MISMATCH':s.stale?'STALE':'DEGRADED'",
    )

    risk_marker = '<div class="section-title">Monthly Risk Review / Fixed % Risk</div><div id="riskReview" class="panel"></div>'
    risk_extra = risk_marker + '<div id="metalsRiskLiveDetail" class="panel"><div class="small">Loading live Metals sizing preview...</div></div>'
    if risk_marker in raw:
        raw = raw.replace(risk_marker, risk_extra, 1)

    call_marker = 'load();loadResearch();setInterval(load,20000);setInterval(loadResearch,60000);'
    live_js = r'''function renderRisk(risk){var remembered={};ORDER.forEach(function(k){var old=document.getElementById('risk-secret-'+k);if(old&&old.value)remembered[k]=old.value});var rows=(risk||{}).strategies||[],due=when((risk||{}).review_due_at_utc);document.getElementById('riskReview').innerHTML='<div class="small">Agreed base-risk percentages · NEW trades only · existing positions are never resized · enter the producer control secret only when applying a change · the Hub does not store the secret · next formal review due '+due+'</div>'+(rows.length?rows.map(function(r){var lane=r.strategy==='metals'?'XAU LONG + SHORT live':(r.strategy==='indices'?'NAS100 + US500 live':'BCO live');var fixed=num(r.fixed_risk_pct_of_nav),target=num(r.calculated_target_gbp),different=(num(r.risk_per_trade_gbp)!==null&&target!==null&&Math.abs(num(r.risk_per_trade_gbp)-target)>0.005);return '<div class="risk-row"><strong>'+(r.label||NAMES[r.strategy])+'</strong><div>'+lane+'<div class="small">Fixed risk <strong>'+((fixed===null)?'-':fixed.toFixed(3)+'%')+'</strong> of NAV · Current '+money(r.risk_per_trade_gbp)+' / trade · target now <strong>'+money(target)+'</strong>'+(different?' · <span class="amber">NOT YET APPLIED</span>':' · <span class="green">CURRENT</span>')+'</div><div class="small">NAV '+money(r.nav_gbp)+' · broker minimum-size rules remain authoritative where applicable.</div><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:7px"><input id="risk-secret-'+r.strategy+'" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Control secret" style="min-width:150px;flex:1;background:#0b0f14;color:#f4f6f8;border:1px solid #394653;border-radius:7px;padding:7px 8px"><button id="risk-apply-'+r.strategy+'" data-strategy="'+r.strategy+'" onclick="applyRisk(this.dataset.strategy,'+(fixed===null?'null':fixed)+')" style="cursor:pointer;background:#10263b;color:#d8ecff;border:1px solid #1f4e73;border-radius:7px;padding:7px 10px;font-weight:800">Apply '+((fixed===null)?'fixed %':fixed.toFixed(3)+'%')+'</button></div><div id="risk-result-'+r.strategy+'" class="small"></div></div><span class="pill '+(different?'amber':'green')+'">'+(different?'PENDING':'SET')+'</span></div>'}).join(''):'<div class="small">No live strategy risk rows available.</div>');Object.keys(remembered).forEach(function(k){var el=document.getElementById('risk-secret-'+k);if(el)el.value=remembered[k]})}
async function applyRisk(strategy,fixedPct){var box=document.getElementById('risk-result-'+strategy),input=document.getElementById('risk-secret-'+strategy),btn=document.getElementById('risk-apply-'+strategy);if(fixedPct===null||!Number.isFinite(Number(fixedPct))){if(box)box.textContent='Fixed risk profile unavailable.';return}var secret=input?input.value:'';if(!secret){if(box)box.innerHTML='<span class="amber">Enter the control secret first.</span>';if(input)input.focus();return}if(input)input.value='';if(btn)btn.disabled=true;if(box)box.textContent='Reading fresh NAV and applying fixed percentage...';try{var resp=await fetch('/api/risk-control/'+encodeURIComponent(strategy),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:'APPLY_FIXED_RISK_PCT',control_secret:secret})});secret='';var data=await resp.json();if(!resp.ok||data.status!=='ok')throw new Error(data.detail||data.error||('HTTP '+resp.status));var minText='';var sp=data.sizing_preview||{};if(sp.used_minimum_trade_size||sp.minimum_units_used)minText=' · broker minimum size in use';if(sp.effective_risk_gbp!==undefined&&sp.effective_risk_gbp!==null)minText+=' · effective preview '+money(sp.effective_risk_gbp);if(box)box.innerHTML='<span class="green">Applied '+Number(data.fixed_risk_pct_of_nav||fixedPct).toFixed(3)+'% of NAV '+money(data.nav_gbp)+' → target '+money(data.current_risk_per_trade_gbp)+' / new trade'+minText+'. Existing positions unchanged.</span>';window.setTimeout(function(){load();loadMetalsRiskLiveDetail()},2500)}catch(e){secret='';if(box)box.innerHTML='<span class="red">Risk update failed: '+String(e.message||e)+'</span>'}finally{if(btn)btn.disabled=false}}
async function loadMetalsRiskLiveDetail(){var box=document.getElementById('metalsRiskLiveDetail');if(!box)return;try{var r=await fetch('/api/metals-risk-live-details',{cache:'no-store'}),x=await r.json();if(x.status!=='ok')throw new Error(x.detail||'unavailable');var ep=(x.estimated_effective_risk_pct_of_nav===null||x.estimated_effective_risk_pct_of_nav===undefined)?'-':Number(x.estimated_effective_risk_pct_of_nav).toFixed(3)+'%';var ov=(x.estimated_risk_overage_pct===null||x.estimated_risk_overage_pct===undefined)?'-':Number(x.estimated_risk_overage_pct).toFixed(1)+'%';var min=(x.minimum_trade_size===null||x.minimum_trade_size===undefined)?'-':String(x.minimum_trade_size);var units=(x.broker_units_preview===null||x.broker_units_preview===undefined)?'-':String(x.broker_units_preview);var pending=(num(x.current_requested_risk_gbp)!==null&&num(x.calculated_target_gbp)!==null&&Math.abs(num(x.current_requested_risk_gbp)-num(x.calculated_target_gbp))>0.005);box.innerHTML='<strong>Metals effective sizing</strong><div class="small">Base policy <strong>0.100% NAV</strong> · current requested cash risk '+money(x.current_requested_risk_gbp)+' · fresh target '+money(x.calculated_target_gbp)+(pending?' · <span class="amber">base-risk update not yet applied</span>':' · <span class="green">base-risk target applied</span>')+' · broker preview '+units+' units (minimum '+min+')</div><div class="small">Estimated effective risk at current price/stop <strong>'+money(x.estimated_effective_risk_gbp)+' / '+ep+' NAV</strong> · target overage '+ov+'. This can exceed 0.100% while the broker minimum size binds.</div><div class="small">After campaign trade #32: ~<strong>0.050% NAV average incremental exposure</strong> via every-other qualified overflow entry at valid size while true half-size is below the broker minimum. Existing positions are never resized.</div>';}catch(e){box.innerHTML='<div class="small">Live Metals sizing preview unavailable: '+String(e.message||e)+'</div>'}};
load();loadResearch();loadMetalsRiskLiveDetail();setInterval(load,20000);setInterval(loadResearch,60000);setInterval(loadMetalsRiskLiveDetail,20000);'''
    if call_marker in raw:
        raw = raw.replace(call_marker, live_js, 1)

    return HTMLResponse(content=raw, status_code=status_code)


@app.get("/api/hub-risk-policy")
def hub_risk_policy() -> dict:
    return {
        "status": "ok",
        "version": POLICY_VERSION,
        "metals": {
            "live_lanes": ["XAU_LONG", "XAU_SHORT"],
            "base_risk_pct_of_nav": BASE_RISK_PCT,
            "overflow_after_trade_32_average_risk_pct_of_nav": OVERFLOW_AVERAGE_RISK_PCT,
            "overflow_execution_mode_while_broker_minimum_binds": "EVERY_OTHER_QUALIFIED_OVERFLOW_AT_NORMAL_VALID_SIZE",
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
# delegated unchanged to the analysis gateway.
app.mount("/", gateway.app)
