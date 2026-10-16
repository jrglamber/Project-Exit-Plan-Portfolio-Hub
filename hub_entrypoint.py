"""Portfolio Hub v0.6.20 production entrypoint.

Keeps the presentation overlay chain intact and defensively guarantees the
Metals live Open Trades bridge is registered on the exact FastAPI app exported
to Uvicorn. This avoids route loss through overlay/import ordering.
"""
from __future__ import annotations

from typing import Any, Dict

import hub_metals_candidate_support_overlay as overlay

app = overlay.app
core = overlay.core
HUB_VERSION = "0.6.20"
POLICY_VERSION = "hub_entrypoint_route_guard_v1_2026_10_16"
core.APP_VERSION = HUB_VERSION


def _metals_live_trade_tile_guard() -> Dict[str, Any]:
    return overlay.metals_live_trade_tile()


# Register the bridge on the final exported app, not merely an intermediate
# overlay.  The guard is idempotent so local imports/tests cannot duplicate it.
_route_present = any(
    str(getattr(route, "path", "")) == "/api/metals-live-trade-tile"
    and "GET" in set(getattr(route, "methods", []) or [])
    for route in list(getattr(app, "routes", []))
)
if not _route_present:
    app.add_api_route(
        "/api/metals-live-trade-tile",
        _metals_live_trade_tile_guard,
        methods=["GET"],
        name="metals_live_trade_tile_guard",
    )

_final_route_present = any(
    str(getattr(route, "path", "")) == "/api/metals-live-trade-tile"
    and "GET" in set(getattr(route, "methods", []) or [])
    for route in list(getattr(app, "routes", []))
)
if not _final_route_present:
    raise RuntimeError("Portfolio Hub failed to register /api/metals-live-trade-tile")

print(
    f"HUB_ENTRYPOINT installed=True version={POLICY_VERSION} "
    f"hub_version={HUB_VERSION} metals_live_trade_tile=True",
    flush=True,
)
