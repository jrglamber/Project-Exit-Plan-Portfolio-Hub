"""Portfolio Hub presentation sync for NAS100 broker-granularity risk policy.

The producer remains authoritative. This wrapper only corrects the Hub's live
risk presentation after NAS100's former 0.050% tier was floored to 0.075%.
US500 keeps the original 0.075% / 0.100% / 0.050% stack map.
"""
from __future__ import annotations

import inspect
from typing import Any

from fastapi.responses import Response

import hub_risk_display_overlay as base

app = base.app

POLICY_VERSION = "hub_nas100_risk_floor_v1_2026_10_14"
HUB_VERSION = "0.6.10"

base.gateway.core.APP_VERSION = HUB_VERSION
base.gateway.core.STRATEGY_LANE_NOTES["indices"] = (
    "NAS100 live · stack risk 1-10 0.075%, 11-20 0.100%, earned 21+ 0.075% "
    "(0.050% tier floored for broker granularity) · "
    "US500 live · 1-10 0.075%, 11-20 0.100%, earned 21+ 0.050%"
)


def _sync_html(text: str) -> str:
    out = str(text)
    out = out.replace("v0.6.9 &middot; portfolio cockpit", "v0.6.10 &middot; portfolio cockpit")
    out = out.replace("v0.6.9 · portfolio cockpit", "v0.6.10 · portfolio cockpit")

    out = out.replace(
        "NAS100 + US500 live · tiered risk by per-asset stack: 1-10 0.075%, 11-20 0.100%, earned 21+ 0.050%",
        "NAS100 live · 1-10 0.075%, 11-20 0.100%, earned 21+ 0.075% (broker-granularity floor) · US500 live · 1-10 0.075%, 11-20 0.100%, earned 21+ 0.050%",
    )
    out = out.replace(
        "Live risk policy · Indices uses a 0.050% base refreshed from NAV then applies 0.075% / 0.100% / 0.050% stack tiers · Metals base target 0.100% with post-32 ~0.050% average incremental exposure · NEW trades only",
        "Live risk policy · Indices uses a 0.050% base refreshed from NAV; NAS100 applies 0.075% / 0.100% / 0.075% by stack cohort while US500 remains 0.075% / 0.100% / 0.050% · Metals base target 0.100% with post-32 ~0.050% average incremental exposure · NEW trades only",
    )

    # Replace the generated Indices risk-row wording while preserving all cash
    # calculations and authenticated apply controls from the underlying Hub.
    out = out.replace(
        "NAS100 + US500 live · <strong>tiered risk per asset</strong>",
        "NAS100 + US500 live · <strong>asset-specific tiered risk</strong>",
    )
    out = out.replace(
        "Live tiers from fresh NAV: <strong>positions 1–10 0.075% ≈ '+money(fresh075)+'</strong> · <strong>11–20 0.100% ≈ '+money(fresh100)+'</strong> · <strong>earned 21+ 0.050% ≈ '+money(target)+'</strong>.",
        "NAS100 fresh tiers: <strong>1–10 0.075% ≈ '+money(fresh075)+'</strong> · <strong>11–20 0.100% ≈ '+money(fresh100)+'</strong> · <strong>earned 21+ 0.075% ≈ '+money(fresh075)+'</strong>. US500 fresh tiers: <strong>1–10 0.075% ≈ '+money(fresh075)+'</strong> · <strong>11–20 0.100% ≈ '+money(fresh100)+'</strong> · <strong>earned 21+ 0.050% ≈ '+money(target)+'</strong>.",
    )
    out = out.replace(
        "Current cash equivalents before any base refresh: '+money(current075)+' / '+money(current100)+' / '+money(current)+'. Candidate position number is counted separately for NAS100 and US500 using active positions + pending opens; uncertain numbering fails safe to the 0.050% base.",
        "Current cash equivalents: NAS100 '+money(current075)+' / '+money(current100)+' / '+money(current075)+' by stack cohort; US500 '+money(current075)+' / '+money(current100)+' / '+money(current)+'. Candidate position number is counted separately per asset using active positions + pending opens; uncertain numbering still fails safe to the 0.050% base path.",
    )
    out = out.replace(
        "Tier engine remains 1–10: 0.075% ≈ '+money(base===null?null:base*1.5)+' · 11–20: 0.100% ≈ '+money(base===null?null:base*2.0)+' · earned 21+: 0.050% ≈ '+money(base)+'. Existing positions unchanged.",
        "Tier engine remains NAS100 1–10: 0.075% ≈ '+money(base===null?null:base*1.5)+' · 11–20: 0.100% ≈ '+money(base===null?null:base*2.0)+' · earned 21+: 0.075% ≈ '+money(base===null?null:base*1.5)+'; US500 remains 0.075% / 0.100% / 0.050%. Existing positions unchanged.",
    )
    return out


def _install_root_wrapper() -> dict[str, Any]:
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

    async def synced_root(*args: Any, **kwargs: Any):
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

    synced_root._pep_nas100_risk_sync = True
    target.endpoint = synced_root
    if getattr(target, "dependant", None) is not None:
        target.dependant.call = synced_root
    return {"ok": True, "version": POLICY_VERSION}


INSTALL = _install_root_wrapper()
print(
    f"HUB_NAS100_RISK_SYNC installed={INSTALL.get('ok')} version={POLICY_VERSION} hub_version={HUB_VERSION}",
    flush=True,
)
