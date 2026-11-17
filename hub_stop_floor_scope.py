"""Read-only stop-floor coverage guard for LIVE Metals broker positions.

The legacy Metals /api/portfolio-risk endpoint can be XAU-only while the
LIVE positions bridge proves additional XAG SHORT exposure. A numeric
portfolio stop-floor must not be labelled complete in that situation.
This module makes NO OANDA calls and changes NO positions or risk settings.
"""
from __future__ import annotations

from math import isfinite


def _count(value):
    try:
        n=float(value)
        return int(n) if isfinite(n) and n >= 0 and int(n)==n else None
    except (TypeError, ValueError):
        return None


def guard_stop_floor_coverage(snapshot):
    sources=snapshot.get("sources") or {}
    metals=(sources.get("metals") or {}).get("data")
    if not isinstance(metals,dict) or str(metals.get("mode") or "").lower() != "live":
        return snapshot
    basket=metals.get("basket") or {}
    expected=_count(basket.get("open_trades"))
    counted=_count(metals.get("live_risk_open_trade_count"))
    verified=basket.get("live_positions_status")=="BROKER_VERIFIED"
    status="COMPLETE" if verified and expected is not None and counted==expected else "INCOMPLETE"
    p=snapshot.setdefault("portfolio",{})
    p["live_stop_floor_scope_status"]=status
    p["live_stop_floor_scope_complete"]=status=="COMPLETE"
    p["live_stop_floor_metals_position_count"]=expected
    p["live_stop_floor_metals_risk_count"]=counted
    if status=="COMPLETE":
        p["live_stop_floor_metals_excluded_count"]=0
        return snapshot

    missing=max(0,expected-counted) if expected is not None and counted is not None else None
    p["live_stop_floor_metals_excluded_count"]=missing
    reason=(f"Metals stop-floor coverage incomplete: {counted if counted is not None else '?'}"
            f"/{expected if expected is not None else '?'} LIVE Metals broker positions covered")
    p["live_stop_floor_coverage_warning"]=reason

    # Retain the original quantified subset for audit, but NEVER present it as
    # the all-positions loss-at-stops outcome.
    p["live_stop_floor_known_partial_gbp"]=p.get("live_stop_floor_pnl_gbp")
    p["live_stop_floor_pnl_gbp"]=None
    p["live_stop_floor_complete"]=False
    p["live_risk_complete"]=False
    metals["live_stop_floor_known_partial_gbp"]=metals.get("live_stop_floor_pnl_gbp")
    metals["live_stop_floor_pnl_gbp"]=None
    metals["live_stop_floor_complete"]=False
    metals["live_stop_floor_coverage_status"]="INCOMPLETE"
    metals["live_stop_floor_coverage_warning"]=reason
    warnings=p.setdefault("warnings",[])
    if reason not in warnings:
        warnings.append(reason)
    attention=snapshot.setdefault("attention",{}).setdefault("items",[])
    if not any(item.get("strategy")=="metals" and item.get("message")==reason
               for item in attention if isinstance(item,dict)):
        attention.append({"strategy":"metals","severity":"warn","message":reason})
    return snapshot
