"""Portfolio Hub v0.6.13 — net stop-floor P&L presentation.

Presentation/observability only. The producer broker telemetry remains authoritative.
For every quantified open trade, the producer already reads the current OANDA
stop (emergency or ratcheted), actual entry and actual units. This overlay nets:

    locked profit at current stops - capital still at risk to current stops

into one portfolio/strategy figure: 'If all stops hit'.

This is a stop-price floor, not a guarantee against market gaps/slippage.
"""
from __future__ import annotations

import inspect
from typing import Any, Dict

from fastapi.responses import Response

import hub_live_risk_overlay as base

app = base.app
core = base.core
HUB_VERSION = "0.6.13"
POLICY_VERSION = "hub_net_stop_floor_v1_2026_10_15"
core.APP_VERSION = HUB_VERSION

_ORIGINAL_AGGREGATE = core.aggregate_snapshot


def _f(value: Any):
    try:
        return None if value is None or value == "" else float(value)
    except Exception:
        return None


def aggregate_snapshot() -> Dict[str, Any]:
    snap = _ORIGINAL_AGGREGATE()

    # Per-strategy net stop-floor P&L. If the producer endpoint is unavailable,
    # leave the figure unknown rather than imply a partial/zero floor.
    for key in ("indices", "metals", "bco"):
        source = ((snap.get("sources") or {}).get(key) or {})
        data = source.get("data") if isinstance(source, dict) else None
        if not isinstance(data, dict):
            continue
        risk_ok = bool(data.get("live_risk_ok"))
        risk = _f(data.get("live_capital_risk_gbp"))
        locked = _f(data.get("live_locked_profit_floor_gbp"))
        unquantified = data.get("live_risk_unquantified_trade_count")
        if risk_ok and risk is not None and locked is not None:
            data["live_stop_floor_pnl_gbp"] = round(locked - risk, 2)
        else:
            data["live_stop_floor_pnl_gbp"] = None
        data["live_stop_floor_complete"] = bool(risk_ok and int(unquantified or 0) == 0)

    portfolio = snap.setdefault("portfolio", {})
    risk = _f(portfolio.get("live_capital_risk_gbp"))
    locked = _f(portfolio.get("live_locked_profit_floor_gbp"))
    endpoints_complete = bool(portfolio.get("live_risk_endpoint_complete"))
    if endpoints_complete and risk is not None and locked is not None:
        portfolio["live_stop_floor_pnl_gbp"] = round(locked - risk, 2)
    else:
        portfolio["live_stop_floor_pnl_gbp"] = None
    portfolio["live_stop_floor_complete"] = bool(portfolio.get("live_risk_complete"))
    portfolio["live_stop_floor_method"] = (
        "sum of each open trade's P&L at its current broker stop: "
        "actual entry → current emergency/ratcheted stop × actual units, converted to GBP"
    )
    portfolio["live_stop_floor_caveat"] = "subject to market gaps/slippage and broker execution"
    return snap


core.aggregate_snapshot = aggregate_snapshot


def _sync_html(text: str) -> str:
    out = str(text)
    out = out.replace("v0.6.12 &middot; portfolio cockpit", "v0.6.13 &middot; portfolio cockpit")
    out = out.replace("v0.6.12 · portfolio cockpit", "v0.6.13 · portfolio cockpit")

    # Replace the v0.6.12 headline risk card with the more intuitive single net
    # stop-out result. Open P&L is kept in the subtext for immediate context.
    old_card = "card((p.live_risk_complete?'Live capital at risk':(p.live_risk_endpoint_complete?'Known capital at risk':'Live capital at risk')),money(p.live_capital_risk_gbp),(p.live_risk_endpoint_complete?((p.live_risk_unquantified_trade_count||0)>0?('⚠ '+p.live_risk_unquantified_trade_count+' trade(s) unquantified · '+p.live_risk_quantified_trade_count+'/'+p.live_risk_total_trade_count+' quantified'):((p.live_risk_total_trade_count||0)+' broker trade(s) stop-backed · locked profit floor '+money(p.live_locked_profit_floor_gbp))):('Telemetry unavailable: '+((p.live_risk_endpoint_failures||[]).join(', ')||'producer error'))))"
    new_card = "card((p.live_stop_floor_complete?'If all stops hit':(p.live_risk_endpoint_complete?'Known stop-floor P&L':'If all stops hit')),('<span class=\"'+cls(p.live_stop_floor_pnl_gbp)+'\">'+money(p.live_stop_floor_pnl_gbp)+'</span>'),(p.live_risk_endpoint_complete?((p.live_risk_unquantified_trade_count||0)>0?('⚠ '+p.live_risk_unquantified_trade_count+' trade(s) unquantified · open P&L now '+money(p.unrealised_pnl_gbp)):('Open P&L now '+money(p.unrealised_pnl_gbp)+' · '+(p.live_risk_total_trade_count||0)+' current broker stop(s) · before gaps/slippage')):('Telemetry unavailable: '+((p.live_risk_endpoint_failures||[]).join(', ')||'producer error'))))"
    out = out.replace(old_card, new_card)

    # Per-strategy risk-card subtext also uses the net stop-floor rather than
    # showing only the downside component.
    out = out.replace(
        "live capital risk '+money(d.live_capital_risk_gbp)+' · signals 1–5 can execute 2x units'",
        "if all stops hit '+money(d.live_stop_floor_pnl_gbp)+' · signals 1–5 can execute 2x units'",
    )
    out = out.replace(
        "live capital risk '+money(d.live_capital_risk_gbp)+' · broker minimum can raise effective risk'",
        "if all stops hit '+money(d.live_stop_floor_pnl_gbp)+' · broker minimum can raise effective risk'",
    )
    out = out.replace(
        "live capital risk '+money(d.live_capital_risk_gbp)+' · discrete units can alter effective risk'",
        "if all stops hit '+money(d.live_stop_floor_pnl_gbp)+' · discrete units can alter effective risk'",
    )
    out = out.replace(
        " / live capital risk '+money(p.live_capital_risk_gbp)",
        " / stop-floor P&L '+money(p.live_stop_floor_pnl_gbp)",
    )
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

    async def synced_root(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Response):
            raw = bytes(getattr(result, "body", b"") or b"").decode("utf-8", errors="replace")
            headers = {k: v for k, v in result.headers.items() if k.lower() not in {"content-length", "content-encoding"}}
            return Response(
                content=_sync_html(raw).encode("utf-8"),
                status_code=int(getattr(result, "status_code", 200) or 200),
                headers=headers,
                media_type=None,
            )
        return _sync_html(str(result))

    target.endpoint = synced_root
    if getattr(target, "dependant", None) is not None:
        target.dependant.call = synced_root
    return {"ok": True, "version": POLICY_VERSION}


INSTALL = _install_root_wrapper()
print(
    f"HUB_STOP_FLOOR installed={INSTALL.get('ok')} version={POLICY_VERSION} hub_version={HUB_VERSION}",
    flush=True,
)
