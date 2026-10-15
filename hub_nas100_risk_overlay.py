"""Portfolio Hub presentation sync for current live risk policies.

Presentation-only. Producer services remain authoritative for all execution,
sizing, stops and broker controls.

Current live policies reflected here:
- Indices Signal-Age v1 (approved 2026-10-15): 0.050% NAV base/reference;
  signals 1-5 request 2x executable units, 6-10 normal, 11-20 alternate
  eligible signals, 21-30 earned/qualified, 31+ fresh-high credit extension.
  The former 0.075/0.100/count-tier risk map is retired.
- Metals XAU LONG + SHORT: 0.100% NAV normal requested/base risk; post-32
  ~0.050% average incremental exposure through broker-safe alternating overflow
  while minimum size binds. Effective broker risk can exceed requested risk.
- BCO: 0.100% NAV requested/base risk; broker discrete-unit rounding can make
  effective executed risk differ modestly from the requested cash target.

The Hub's producer summary contract exposes one requested/base risk number, not
position-by-position locked stop risk. Therefore portfolio open-risk arithmetic
based on open_count * base risk is labelled explicitly as a nominal proxy.
"""
from __future__ import annotations

import inspect
from typing import Any

from fastapi.responses import Response

import hub_risk_display_overlay as base

app = base.app

POLICY_VERSION = "hub_signal_age_risk_display_v1_2026_10_15"
HUB_VERSION = "0.6.11"

base.gateway.core.APP_VERSION = HUB_VERSION
base.gateway.core.STRATEGY_LANE_NOTES["indices"] = (
    "NAS100 + US500 live · Signal-Age v1: 1-5 2x executable units, 6-10 normal, "
    "11-20 alternate eligible signals, 21-30 earned/qualified, 31+ fresh-high credits · no strategic cap"
)
base.gateway.core.STRATEGY_LANE_NOTES["metals"] = (
    "XAU LONG + SHORT live · adaptive stack: first 20 normal, 21+ needs +2R campaign funding, "
    "post-32 half average exposure (broker-safe alternating overflow while minimum size binds) · harvest 30/80/130R+"
)
base.gateway.core.STRATEGY_LANE_NOTES["bco"] = (
    "BCO LONG live · RED/CRITICAL stacking brake · 48h + ATR2 · 0.100% NAV requested/base risk"
)


def _sync_html(text: str) -> str:
    out = str(text)
    out = out.replace("v0.6.9 &middot; portfolio cockpit", "v0.6.11 &middot; portfolio cockpit")
    out = out.replace("v0.6.9 · portfolio cockpit", "v0.6.11 · portfolio cockpit")
    out = out.replace("v0.6.10 &middot; portfolio cockpit", "v0.6.11 &middot; portfolio cockpit")
    out = out.replace("v0.6.10 · portfolio cockpit", "v0.6.11 · portfolio cockpit")

    # Strategy lane text: retire the Oct-14 count-tier description.
    out = out.replace(
        "NAS100 + US500 live · tiered risk by per-asset stack: 1-10 0.075%, 11-20 0.100%, earned 21+ 0.050%",
        "NAS100 + US500 live · Signal-Age v1: 1-5 2x executable units, 6-10 normal, 11-20 alternate eligible signals, 21-30 earned/qualified, 31+ fresh-high credits · no strategic cap",
    )
    out = out.replace(
        "NAS100 live · 1-10 0.075%, 11-20 0.100%, earned 21+ 0.075% (broker-granularity floor) · US500 live · 1-10 0.075%, 11-20 0.100%, earned 21+ 0.050%",
        "NAS100 + US500 live · Signal-Age v1: 1-5 2x executable units, 6-10 normal, 11-20 alternate eligible signals, 21-30 earned/qualified, 31+ fresh-high credits · no strategic cap",
    )

    out = out.replace(
        "Live risk policy · Indices uses a 0.050% base refreshed from NAV then applies 0.075% / 0.100% / 0.050% stack tiers · Metals base target 0.100% with post-32 ~0.050% average incremental exposure · NEW trades only",
        "Live risk policy · Indices uses a 0.050% base/reference refreshed from NAV; Signal-Age v1 scales executable units 2x for signals 1-5 and governs later admission/frequency · Metals 0.100% requested/base with post-32 ~0.050% average incremental exposure · BCO 0.100% requested/base · NEW trades only",
    )

    # Main per-strategy card: a producer's single risk_per_trade_gbp is a
    # base/requested figure, not necessarily the effective risk of every trade.
    old_card = "card('Approved risk / trade',money(d.risk_per_trade_gbp),riskPct+' of NAV')"
    new_card = "card(key==='indices'?'Base risk reference':'Base risk request',money(d.risk_per_trade_gbp),(key==='indices'?(riskPct+' current NAV · policy target 0.050% · signals 1–5 can execute 2x units'):(key==='metals'?(riskPct+' current NAV · target 0.100% · broker minimum can raise effective risk'):(riskPct+' current NAV · target 0.100% · discrete units can alter effective risk'))))"
    out = out.replace(old_card, new_card)

    # The aggregate arithmetic is only a nominal proxy under variable sizing.
    out = out.replace(
        "card('Open risk estimate',money(p.open_risk_estimate_gbp),'Open trades x approved risk')",
        "card('Nominal risk proxy',money(p.open_risk_estimate_gbp),'Open trades × base/requested risk · not actual locked stop risk')",
    )
    out = out.replace(" / open risk '+money(p.open_risk_estimate_gbp)", " / nominal risk proxy '+money(p.open_risk_estimate_gbp)")
    out = out.replace("+' open / risk '+money(d.risk_per_trade_gbp)+' / basket '", "+' open / base-request '+money(d.risk_per_trade_gbp)+' / basket '")

    # Monthly risk review: base refresh remains valid, but the old count-tier
    # wording is no longer the live Indices policy.
    out = out.replace(
        "Risk refreshes affect NEW trades only; existing positions are never resized. Indices refreshes the 0.050% base cash amount used by its live tier engine. The Hub does not store the producer control secret.",
        "Risk refreshes affect NEW trades only; existing positions are never resized. Indices refreshes the 0.050% base cash reference used by Signal-Age v1; the signal-age engine then scales/filters opportunities without changing that stored base. The Hub does not store the producer control secret.",
    )
    out = out.replace(
        "NAS100 + US500 live · <strong>tiered risk per asset</strong>",
        "NAS100 + US500 live · <strong>Signal-Age v1</strong>",
    )
    out = out.replace(
        "Live tiers from fresh NAV: <strong>positions 1–10 0.075% ≈ '+money(fresh075)+'</strong> · <strong>11–20 0.100% ≈ '+money(fresh100)+'</strong> · <strong>earned 21+ 0.050% ≈ '+money(target)+'</strong>.",
        "Base/reference from fresh NAV: <strong>0.050% ≈ '+money(target)+'</strong>. Signal-Age execution: <strong>1–5 = 2x executable units</strong> (rough nominal base equivalent '+money(target===null?null:target*2.0)+'); <strong>6–10 = 1x</strong>; <strong>11–20 = alternate eligible signals</strong>; <strong>21–30 = earned/qualified</strong>; <strong>31+ = fresh-high credits</strong>.",
    )
    out = out.replace(
        "Current cash equivalents before any base refresh: '+money(current075)+' / '+money(current100)+' / '+money(current)+'. Candidate position number is counted separately for NAS100 and US500 using active positions + pending opens; uncertain numbering fails safe to the 0.050% base.",
        "Current stored base/reference: '+money(current)+' ('+(nav&&current!==null?((current/nav)*100).toFixed(3):'-')+'% of current NAV). Signal-Age counts candidates separately for NAS100 and US500. Early 2x sizing scales executable units and remains subject to broker/per-trade risk guardrails.",
    )
    out = out.replace(
        "Base refreshed from NAV '+money(liveNav)+' → '+money(base)+'. Tier engine remains 1–10: 0.075% ≈ '+money(base===null?null:base*1.5)+' · 11–20: 0.100% ≈ '+money(base===null?null:base*2.0)+' · earned 21+: 0.050% ≈ '+money(base)+'. Existing positions unchanged.",
        "Base refreshed from NAV '+money(liveNav)+' → '+money(base)+'. Signal-Age v1 remains: 1–5 2x executable units; 6–10 1x; 11–20 alternate eligible; 21–30 earned/qualified; 31+ fresh-high credits. Existing positions unchanged.",
    )

    # Make base/requested-vs-effective semantics explicit for Metals/BCO in the
    # monthly review without altering producer-owned apply controls.
    out = out.replace(
        "Base risk <strong>'+((fixed===null)?'-':fixed.toFixed(3)+'%')+'</strong> of NAV · Current '+money(current)+' / trade · target now <strong>'+money(target)+'</strong>",
        "Requested/base risk <strong>'+((fixed===null)?'-':fixed.toFixed(3)+'%')+'</strong> of NAV · current cash request '+money(current)+' · target now <strong>'+money(target)+'</strong>",
    )
    out = out.replace(
        "NAV '+money(nav)+' · broker minimum-size rules remain authoritative where applicable.",
        "NAV '+money(nav)+' · effective broker risk may differ from the cash request because minimum/discrete unit rules remain authoritative.",
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

    synced_root._pep_live_risk_sync = True
    target.endpoint = synced_root
    if getattr(target, "dependant", None) is not None:
        target.dependant.call = synced_root
    return {"ok": True, "version": POLICY_VERSION}


INSTALL = _install_root_wrapper()
print(
    f"HUB_RISK_DISPLAY_SYNC installed={INSTALL.get('ok')} version={POLICY_VERSION} hub_version={HUB_VERSION}",
    flush=True,
)
