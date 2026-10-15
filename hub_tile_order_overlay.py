"""Portfolio Hub v0.6.16 — exact portfolio headline row layout.

Presentation-only overlay:
- Row 1: Portfolio NAV, one-tile width, centred.
- Row 2: Open P&L | If all stops hit.
- Row 3: Today realised | Week realised.
- Row 4: Month realised | All-time realised.
- Observed drawdown remains removed.

No producer, strategy, execution, sizing, stop, or accounting logic is changed.
"""
from __future__ import annotations

import inspect
import re
from typing import Any

from fastapi.responses import Response

import hub_stop_floor_overlay as base

app = base.app
core = base.core
HUB_VERSION = "0.6.16"
POLICY_VERSION = "hub_portfolio_tile_rows_v3_2026_10_15"
core.APP_VERSION = HUB_VERSION


def _sync_html(text: str) -> str:
    out = str(text)
    out = out.replace("v0.6.15 &middot; portfolio cockpit", "v0.6.16 &middot; portfolio cockpit")
    out = out.replace("v0.6.15 · portfolio cockpit", "v0.6.16 · portfolio cockpit")
    out = out.replace("v0.6.14 &middot; portfolio cockpit", "v0.6.16 &middot; portfolio cockpit")
    out = out.replace("v0.6.14 · portfolio cockpit", "v0.6.16 · portfolio cockpit")
    out = out.replace("v0.6.13 &middot; portfolio cockpit", "v0.6.16 &middot; portfolio cockpit")
    out = out.replace("v0.6.13 · portfolio cockpit", "v0.6.16 · portfolio cockpit")

    stop_floor_card = (
        "card((p.live_stop_floor_complete?'If all stops hit':"
        "(p.live_risk_endpoint_complete?'Known stop-floor P&L':'If all stops hit'))," 
        "('<span class=\"'+cls(p.live_stop_floor_pnl_gbp)+'\">'+money(p.live_stop_floor_pnl_gbp)+'</span>'),"
        "(p.live_risk_endpoint_complete?((p.live_risk_unquantified_trade_count||0)>0?"
        "('⚠ '+p.live_risk_unquantified_trade_count+' trade(s) unquantified · open P&L now '+money(p.unrealised_pnl_gbp)):"
        "('Open P&L now '+money(p.unrealised_pnl_gbp)+' · '+(p.live_risk_total_trade_count||0)+' current broker stop(s) · before gaps/slippage')):"
        "('Telemetry unavailable: '+((p.live_risk_endpoint_failures||[]).join(', ')||'producer error'))))"
    )

    desired = (
        "document.getElementById('headline').innerHTML=["
        "card('Portfolio NAV',money(p.nav_gbp),'Freshest '+(NAMES[p.nav_source]||p.nav_source||'source'))," 
        "card('Open P&L','<span class=\"'+cls(p.unrealised_pnl_gbp)+'\">'+money(p.unrealised_pnl_gbp)+'</span>','Live strategies only'),"
        + stop_floor_card + ","
        "card('Today realised','<span class=\"'+cls(p.realised_today_gbp)+'\">'+money(p.realised_today_gbp)+'</span>','Live strategies only'),"
        "card('Week realised','<span class=\"'+cls(p.realised_week_gbp)+'\">'+money(p.realised_week_gbp)+'</span>','Live strategies only'),"
        "card('Month realised','<span class=\"'+cls(p.realised_month_gbp)+'\">'+money(p.realised_month_gbp)+'</span>','Live strategies only'),"
        "card('All-time realised','<span class=\"'+cls(p.realised_all_time_gbp)+'\">'+money(p.realised_all_time_gbp)+'</span>','Live strategies only')"
        "].join('');"
    )

    pattern = r"document\.getElementById\('headline'\)\.innerHTML=\[.*?\]\.join\(''\);"
    out, count = re.subn(pattern, desired, out, count=1)
    if count != 1:
        raise RuntimeError(f"Portfolio headline tile assignment replacement count={count}")

    # Exact row geometry requested by the user. Keep the portfolio headline a
    # two-column grid at every viewport width. NAV occupies its own first row,
    # but stays the width of one normal tile and is centred horizontally.
    layout_css = (
        "\n<style id=\"portfolio-headline-row-layout\">"
        "#headline.headline{grid-template-columns:repeat(2,minmax(0,1fr))!important;}"
        "#headline.headline>.card:first-child{grid-column:1/-1;width:calc((100% - 7px)/2);justify-self:center;}"
        "</style>\n"
    )
    if "portfolio-headline-row-layout" not in out:
        out = out.replace("</head>", layout_css + "</head>", 1)
    return out


def _install_root_wrapper() -> dict:
    target = None
    for route in list(getattr(app, "routes", [])):
        if str(getattr(route, "path", "")) == "/" and "GET" in set(getattr(route, "methods", []) or []):
            target = route
            break
    if target is None:
        raise RuntimeError("Portfolio Hub root route not found")

    original = getattr(getattr(target, "dependant", None), "call", None) or getattr(target, "endpoint", None)
    if not callable(original):
        raise RuntimeError("Portfolio Hub root endpoint not callable")

    async def reordered_root(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Response):
            raw = bytes(getattr(result, "body", b"") or b"").decode("utf-8", errors="replace")
            headers = {
                k: v for k, v in result.headers.items()
                if k.lower() not in {"content-length", "content-encoding"}
            }
            return Response(
                content=_sync_html(raw).encode("utf-8"),
                status_code=int(getattr(result, "status_code", 200) or 200),
                headers=headers,
                media_type=None,
            )
        return _sync_html(str(result))

    target.endpoint = reordered_root
    if getattr(target, "dependant", None) is not None:
        target.dependant.call = reordered_root
    return {"ok": True, "version": POLICY_VERSION}


INSTALL = _install_root_wrapper()
print(
    f"HUB_TILE_ORDER installed={INSTALL.get('ok')} version={POLICY_VERSION} hub_version={HUB_VERSION}",
    flush=True,
)
