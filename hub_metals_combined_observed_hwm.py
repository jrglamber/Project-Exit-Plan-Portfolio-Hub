"""Combined LIVE XAU + XAG SHORT reporting peak sourced from Metals.

XAU's own high-water/harvest state remains preserved under xau_only_*.
This module only changes the Hub's displayed combined financial fields.
Never derive a combined peak by summing independently timed XAU/XAG peaks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite

VERSION = "hub_combined_metals_observed_hwm_v1_2026_11_16"
SCOPE = "LIVE_XAU_LONG_SHORT_PLUS_LIVE_XAG_SHORT"


def _num(value):
    if value is None or value == "":
        return None
    try:
        n=float(value)
        return n if isfinite(n) else None
    except (ValueError, TypeError):
        return None


def _date(value):
    try:
        dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def verify(payload, proof, max_age_seconds=120.0):
    """Validate the persisted producer peak against independently verified trades."""
    if not isinstance(payload,dict) or payload.get("ok") is not True:
        raise ValueError("Metals combined reporting endpoint unavailable")
    if payload.get("scope") != SCOPE or payload.get("high_water_status") != "OBSERVED_COMBINED_LIVE":
        raise ValueError("Combined Metals observed high-water unavailable")
    if not isinstance(proof,dict) or proof.get("ok") is not True or proof.get("scope") != SCOPE:
        raise ValueError("Hub live broker proof unavailable")

    ts=_date(payload.get("observed_at_utc"))
    start=_date(payload.get("high_water_tracking_started_at_utc"))
    peak_at=_date(payload.get("high_water_at_utc"))
    now=datetime.now(timezone.utc)
    if ts is None or start is None or peak_at is None:
        raise ValueError("Combined high-water timestamps missing")
    if not -10 <= (now-ts).total_seconds() <= max_age_seconds:
        raise ValueError("Combined high-water snapshot stale")
    if start > ts or peak_at < start or peak_at > ts:
        raise ValueError("Invalid combined high-water observation boundaries")
    if (payload.get("open_count")!=proof.get("open_count") or
        payload.get("xau_count")!=proof.get("xau_count") or
        payload.get("xag_short_count")!=proof.get("xag_short_count")):
        raise ValueError("Combined high-water position count differs from Hub broker proof")

    peak=_num(payload.get("high_water_gbp"))
    curr=_num(payload.get("open_pnl_gbp"))
    giveback=_num(payload.get("giveback_gbp"))
    if peak is None or curr is None or giveback is None or peak < 0:
        raise ValueError("Combined high-water values missing or invalid")
    if peak + 0.01 < curr or abs(giveback-max(0.0,peak-curr))>0.02:
        raise ValueError("Producer combined high-water and giveback inconsistent")
    # The Hub positions snapshot may be slightly newer than the producer peak.
    current_hub=_num(proof.get("open_pnl_gbp"))
    if current_hub is None:
        raise ValueError("Hub combined open P&L unavailable")
    # If the independently verified Hub snapshot has itself printed a new
    # combined peak, show at least that value instead of negative giveback.
    display_peak=max(peak,current_hub)
    display_back=max(0.0,display_peak-current_hub)
    return {
        "high_water_gbp": round(display_peak,4),
        "high_water_at_utc": (proof.get("observed_at_utc")
                              if current_hub > peak else payload["high_water_at_utc"]),
        "high_water_tracking_started_at_utc": payload["high_water_tracking_started_at_utc"],
        "giveback_gbp": round(display_back,4),
        "giveback_pct": round(100*display_back/display_peak,2) if display_peak>0 else None,
        "high_water_scope": "LIVE_XAU_PLUS_XAG_SHORT_OBSERVED_SINCE_START",
        "high_water_status": "OBSERVED_COMBINED_LIVE",
        "source": "metals:/api/live-metals-open-pnl",
        "version": VERSION,
        "historical_backfill_complete": False,
        "affects_trading_or_xau_protection": False,
    }


def _save_xau_fields(basket):
    # Keep the original XAU-only monetary and R-based protection statistics
    # available in the API; never relabel the XAU R number as combined.
    for name in ("high_water_gbp","high_water_r","high_water_at_utc",
                 "high_water_gbp_source","giveback_gbp","giveback_r"):
        key="xau_only_"+name
        if key not in basket:
            basket[key]=basket.get(name)


def apply(summary, observed):
    basket=summary.setdefault("basket",{})
    _save_xau_fields(basket)
    basket.update({
        "high_water_gbp": observed["high_water_gbp"],
        "high_water_at_utc": observed["high_water_at_utc"],
        "high_water_r": None,
        "high_water_gbp_source": observed["source"],
        "high_water_gbp_snapshot_at_utc": observed["high_water_at_utc"],
        "giveback_gbp": observed["giveback_gbp"],
        "giveback_r": None,
        "giveback_pct": observed["giveback_pct"],
        "high_water_scope": observed["high_water_scope"],
        "giveback_scope": observed["high_water_scope"],
        "high_water_tracking_started_at_utc": observed["high_water_tracking_started_at_utc"],
        "combined_high_water_status": observed["high_water_status"],
        "combined_high_water_error": None,
    })
    summary["live_metals_high_water"]={
        "ok":True, **observed, "broker_writes":False}
    return summary


def mark_unavailable(summary, reason):
    basket=summary.setdefault("basket",{})
    _save_xau_fields(basket)
    basket.update({
        "high_water_gbp":None, "high_water_r":None, "high_water_at_utc":None,
        "high_water_gbp_source":None, "high_water_gbp_snapshot_at_utc":None,
        "giveback_gbp":None, "giveback_r":None, "giveback_pct":None,
        "high_water_scope":"LIVE_XAU_PLUS_XAG_SHORT_OBSERVED_SINCE_START",
        "giveback_scope":"LIVE_XAU_PLUS_XAG_SHORT_OBSERVED_SINCE_START",
        "combined_high_water_status":"UNAVAILABLE",
        "combined_high_water_error":str(reason)[:180],
        "high_water_tracking_started_at_utc":None,
    })
    summary["live_metals_high_water"]={
        "ok":False,"source":"metals:/api/live-metals-open-pnl",
        "reason":str(reason)[:180],"broker_writes":False}
    return summary
