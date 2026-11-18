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
    quantified=_count(metals.get("live_risk_quantified_trade_count"))
    verified=(basket.get("live_positions_status")=="BROKER_VERIFIED"
              and metals.get("live_risk_ok") is True)
    proof=metals.get("live_metals_positions") or {}
    position_ids=proof.get("broker_trade_ids")
    stop_ids=metals.get("live_risk_broker_trade_ids")
    exact_ids=(isinstance(position_ids,list) and isinstance(stop_ids,list)
               and len(position_ids)==len(stop_ids)
               and len(set(map(str,position_ids)))==len(position_ids)
               and len(set(map(str,stop_ids)))==len(stop_ids)
               and all(str(x).strip() for x in position_ids+stop_ids)
               and set(map(str,position_ids))==set(map(str,stop_ids)))
    by=metals.get("live_risk_by_instrument") or {}
    xau=by.get("XAU_USD") or {}
    xag=by.get("XAG_USD") or {}
    xau_expected=_count(basket.get("live_xau_open_count"))
    xag_expected=_count(basket.get("live_xag_short_open_count"))
    instrument_coverage=(
        xau_expected is not None and xag_expected is not None
        and xau_expected + xag_expected == expected
        and _count(xau.get("open_trade_count", 0 if xau_expected==0 else None)) == xau_expected
        and _count(xag.get("open_trade_count", 0 if xag_expected==0 else None)) == xag_expected
        and _count(xag.get("short_trade_count", 0 if xag_expected==0 else None)) == xag_expected
        and _count(xag.get("long_trade_count", 0)) == 0
        and _count(xau.get("quantified_trade_count", 0 if xau_expected==0 else None)) == xau_expected
        and _count(xag.get("quantified_trade_count", 0 if xag_expected==0 else None)) == xag_expected
    )
    # Counts from independent broker reads can temporarily disagree as trades
    # open/close. More quantified trades than observed positions CANNOT mean
    # "missing stops"; the snapshots are not comparable and must fail closed.
    snapshot_mismatch = bool(verified and expected is not None
                             and counted is not None
                             and (counted > expected
                                  or (quantified is not None and quantified > expected)
                                  or (counted == expected and not exact_ids)))
    status=("COMPLETE" if (verified and expected is not None
            and counted == expected and quantified == expected
            and exact_ids and instrument_coverage)
            else ("SNAPSHOT_MISMATCH" if snapshot_mismatch else "INCOMPLETE"))
    p=snapshot.setdefault("portfolio",{})
    p["live_stop_floor_scope_status"]=status
    p["live_stop_floor_scope_complete"]=status=="COMPLETE"
    p["live_stop_floor_metals_position_count"]=expected
    p["live_stop_floor_metals_risk_count"]=counted
    p["live_stop_floor_metals_quantified_count"]=quantified
    p["live_stop_floor_metals_exact_trade_ids"]=exact_ids
    p["live_stop_floor_metals_instrument_coverage"]=instrument_coverage
    if status=="COMPLETE":
        p["live_stop_floor_metals_excluded_count"]=0
        return snapshot

    missing=(None if snapshot_mismatch else
             max(0,expected-quantified) if expected is not None and quantified is not None else None)
    p["live_stop_floor_metals_excluded_count"]=missing
    if snapshot_mismatch:
        reason=(f"Metals broker position/risk snapshots disagree: "
                f"{expected if expected is not None else '?'} verified positions vs "
                f"{counted if counted is not None else '?'} risk-feed trades "
                f"({quantified if quantified is not None else '?'} quantified). "
                "Stop-floor withheld until fresh snapshots agree; "
                "this is NOT proof of missing broker stop orders.")
    else:
        reason=(f"Metals stop-floor coverage incomplete: "
                f"{min(quantified, expected) if quantified is not None and expected is not None else '?'}"
                f"/{expected if expected is not None else '?'} LIVE Metals broker positions stop-verified"
                + ("; broker trade IDs mismatch" if not exact_ids else "")
                + ("; instrument scope mismatch" if not instrument_coverage else ""))
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
    metals["live_stop_floor_coverage_status"]=status
    metals["live_stop_floor_coverage_warning"]=reason
    warnings=p.setdefault("warnings",[])
    if reason not in warnings:
        warnings.append(reason)
    attention=snapshot.setdefault("attention",{}).setdefault("items",[])
    if not any(item.get("strategy")=="metals" and item.get("message")==reason
               for item in attention if isinstance(item,dict)):
        attention.append({"strategy":"metals","severity":"warn","message":reason})
    return snapshot
