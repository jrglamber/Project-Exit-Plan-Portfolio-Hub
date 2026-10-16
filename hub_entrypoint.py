"""Portfolio Hub v0.6.21 production entrypoint.

Keeps the presentation overlay chain intact and guarantees the Metals live Open
Trades bridge is registered with highest route precedence on the exact FastAPI
app exported to Uvicorn. A non-fatal startup probe also verifies the Hub can
reach the Metals producer contract.
"""
from __future__ import annotations

from typing import Any, Dict

import hub_metals_candidate_support_overlay as overlay

app = overlay.app
core = overlay.core
HUB_VERSION = "0.6.21"
POLICY_VERSION = "hub_entrypoint_route_guard_v2_2026_10_16"
core.APP_VERSION = HUB_VERSION


def _metals_live_trade_tile_guard() -> Dict[str, Any]:
    return overlay.metals_live_trade_tile()


def _is_metals_tile_route(route: Any) -> bool:
    return (
        str(getattr(route, "path", "")) == "/api/metals-live-trade-tile"
        and "GET" in set(getattr(route, "methods", []) or [])
    )


# Register on the final exported app if needed.
_routes = list(getattr(app, "routes", []))
if not any(_is_metals_tile_route(route) for route in _routes):
    app.add_api_route(
        "/api/metals-live-trade-tile",
        _metals_live_trade_tile_guard,
        methods=["GET"],
        name="metals_live_trade_tile_guard",
    )

# Starlette resolves routes in order.  The endpoint existed in the overlay yet
# production still returned 404, so make this exact route authoritative by
# moving it ahead of any broader route/mount that may otherwise match first.
_route = next((route for route in list(getattr(app, "routes", [])) if _is_metals_tile_route(route)), None)
if _route is None:
    raise RuntimeError("Portfolio Hub failed to register /api/metals-live-trade-tile")
app.routes.remove(_route)
app.routes.insert(0, _route)

# Non-fatal contract probe: proves the deployed Hub can reach the fixed Metals
# producer endpoint without making Hub availability depend on Metals uptime.
try:
    _probe = overlay.metals_live_trade_tile()
    print(
        "HUB_METALS_LIVE_TILE_PROBE "
        f"ok={bool(_probe.get('ok'))} "
        f"open_count={_probe.get('open_count')} "
        f"long_count={_probe.get('long_count')} "
        f"short_count={_probe.get('short_count')} "
        f"last_opened_at_utc={_probe.get('last_opened_at_utc')} "
        f"error={_probe.get('error')}",
        flush=True,
    )
except Exception as exc:
    print(f"HUB_METALS_LIVE_TILE_PROBE ok=False probe_exception={type(exc).__name__}: {exc}", flush=True)

print(
    f"HUB_ENTRYPOINT installed=True version={POLICY_VERSION} "
    f"hub_version={HUB_VERSION} metals_live_trade_tile=True route_index=0",
    flush=True,
)
