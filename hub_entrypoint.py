"""Portfolio Hub v0.6.22 production entrypoint.

Keeps the presentation overlay chain intact, guarantees the Metals live Open
Trades bridge is registered with highest route precedence on the exact FastAPI
app exported to Uvicorn, and prevents heavyweight research harvesting from
competing with live cockpit telemetry.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict

import analysis_gateway as _analysis_gateway
import hub_metals_candidate_support_overlay as overlay

app = overlay.app
core = overlay.core
HUB_VERSION = "0.6.22"
POLICY_VERSION = "hub_live_telemetry_load_guard_v1_2026_10_19"
core.APP_VERSION = HUB_VERSION


# ---------------------------------------------------------------------------
# Live-telemetry load guard
# ---------------------------------------------------------------------------
# analysis_gateway historically emitted a very large research pack on startup,
# every ~15 minutes, and whenever a producer version changed. That sweep makes
# dozens of bounded-but-expensive Indices/Metals research requests (including
# 250-row historical slices and causal studies). It is useful for ad-hoc research
# but must not compete with the Portfolio Hub's live /api/portfolio-summary poll.
#
# Keep the lightweight /analysis/status freshness worker automatic. All heavy
# discovery/research endpoints remain available unchanged and can still be called
# explicitly when analysis is required. An env escape hatch preserves the old
# behaviour for diagnostics without another code change.
_PERIODIC_HEAVY_ANALYSIS_ENABLED = (
    os.getenv("HUB_PERIODIC_HEAVY_ANALYSIS_ENABLED", "false").strip().lower()
    == "true"
)

if not _PERIODIC_HEAVY_ANALYSIS_ENABLED:
    def _live_safe_analysis_worker() -> None:
        while True:
            try:
                _analysis_gateway._refresh_sources()
            except Exception as exc:
                print(
                    "HUB_ANALYSIS_LOAD_GUARD lightweight_refresh_error="
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
            time.sleep(_analysis_gateway.ANALYSIS_POLL_SECONDS)

    # start_analysis_gateway() resolves this module global when its FastAPI
    # startup hook executes, so replace the target before application startup.
    _analysis_gateway._analysis_worker = _live_safe_analysis_worker



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

# Starlette resolves routes in order. The endpoint existed in the overlay yet
# production still returned 404, so make this exact route authoritative by
# moving it ahead of any broader route/mount that may otherwise match first.
_route = next(
    (route for route in list(getattr(app, "routes", [])) if _is_metals_tile_route(route)),
    None,
)
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
    print(
        f"HUB_METALS_LIVE_TILE_PROBE ok=False probe_exception={type(exc).__name__}: {exc}",
        flush=True,
    )

print(
    f"HUB_ANALYSIS_LOAD_GUARD installed={not _PERIODIC_HEAVY_ANALYSIS_ENABLED} "
    f"periodic_heavy_analysis_enabled={_PERIODIC_HEAVY_ANALYSIS_ENABLED} "
    f"light_poll_seconds={_analysis_gateway.ANALYSIS_POLL_SECONDS} "
    "heavy_research_endpoints_preserved=True execution_authority=False",
    flush=True,
)
print(
    f"HUB_ENTRYPOINT installed=True version={POLICY_VERSION} "
    f"hub_version={HUB_VERSION} metals_live_trade_tile=True route_index=0",
    flush=True,
)
