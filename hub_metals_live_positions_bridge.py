"""Strict read-only LIVE Metals broker-position bridge for Portfolio Hub.

The legacy /api/portfolio-summary Metals basket is XAU-only. Never treat
practice XAG or unavailable broker data as complete Metals exposure.
P&L and counts come exclusively from the live XAU+XAG broker-backed endpoint,
with exact XAG link reconciliation; historical realised/HWM remain XAU-only.
No broker execution or risk-sizing authority is conferred here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any, Dict

VERSION = "hub_metals_xau_xag_live_bridge_v1_2026_11_12"
EXPECTED_SCOPE = "LIVE_XAU_LONG_SHORT_PLUS_LIVE_XAG_SHORT"


def _num(v: Any):
    if v is None or v == "":
        return None
    try:
        n = float(v)
        return n if isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _count(v: Any):
    n = _num(v)
    return int(n) if n is not None and n >= 0 and int(n) == n else None


def _timestamp(v: Any):
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            return None
        return d.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def verify(payload: Dict[str, Any], *, max_age_seconds: float = 120.0) -> Dict[str, Any]:
    """Fail closed: never promote a partially-known or unlinked broker basket."""
    if not isinstance(payload, dict):
        raise ValueError("LIVE Metals positions response is not an object")
    if payload.get("scope") != EXPECTED_SCOPE or payload.get("ok") is not True:
        raise ValueError("LIVE Metals combined scope unavailable")
    if payload.get("xau_ok") is not True or payload.get("xag_ok") is not True:
        raise ValueError("LIVE XAU or LIVE XAG broker snapshot unavailable")
    if payload.get("practice_excluded") is not True:
        raise ValueError("Practice exclusion not verified")
    if payload.get("xag_broker_link_reconciled") is not True:
        raise ValueError("LIVE XAG broker-link reconciliation mismatch")
    if payload.get("xag_unrealized_complete") is not True:
        raise ValueError("LIVE XAG unrealised P&L incomplete")
    if _count(payload.get("xag_untracked_broker_count")) != 0:
        raise ValueError("LIVE XAG untracked broker positions")
    ts = _timestamp(payload.get("observed_at_utc"))
    now = datetime.now(timezone.utc)
    if ts is None or not -10 <= (now - ts).total_seconds() <= max_age_seconds:
        raise ValueError("LIVE Metals broker snapshot stale or timestamp invalid")

    xau = _count(payload.get("xau_count"))
    xag = _count(payload.get("xag_count"))
    shorts = _count(payload.get("xag_short_count"))
    total = _count(payload.get("count"))
    trades = payload.get("trades")
    if None in (xau, xag, shorts, total) or not isinstance(trades, list):
        raise ValueError("LIVE Metals broker counts missing")
    if xag != shorts or total != xau+xag or len(trades) != total:
        raise ValueError("LIVE Metals open counts conflict")

    ids = set()
    xau_rows = 0
    xag_rows = 0
    xau_pnl = 0.0
    xag_pnl = 0.0
    for row in trades:
        if not isinstance(row, dict):
            raise ValueError("LIVE Metals position row invalid")
        bid = str(row.get("broker_trade_id") or "").strip()
        if not bid or bid in ids:
            raise ValueError("LIVE Metals broker ID missing or duplicated")
        ids.add(bid)
        pnl = _num(row.get("unrealized_gbp"))
        if pnl is None:
            raise ValueError("LIVE Metals broker unrealised P&L missing")
        lane = row.get("lane")
        if lane == "XAU_LIVE" and str(row.get("instrument") or "").upper().replace("_", "") == "XAUUSD":
            xau_rows += 1
            xau_pnl += pnl
        elif lane == "XAG_SHORT_LIVE" and str(row.get("instrument") or "").upper().replace("_", "") == "XAGUSD":
            if str(row.get("side") or "").upper() != "SHORT" or row.get("tracking") != "TRACKED":
                raise ValueError("Untracked or non-short XAG broker trade")
            xag_rows += 1
            xag_pnl += pnl
        else:
            raise ValueError("Unknown or non-live Metals lane")
    if xau_rows != xau or xag_rows != xag:
        raise ValueError("LIVE Metals row ownership counts conflict")
    given_xag_pl = _num(payload.get("xag_unrealized_gbp"))
    if given_xag_pl is None or abs(given_xag_pl - xag_pnl) > 0.05:
        raise ValueError("LIVE XAG broker unrealised P&L mismatch")

    return {
        "ok": True, "version": VERSION,
        "scope": EXPECTED_SCOPE,
        "open_count": total,
        "xau_count": xau,
        "xag_short_count": xag,
        "open_pnl_gbp": round(xau_pnl+xag_pnl, 4),
        "xau_open_pnl_gbp": round(xau_pnl, 4),
        "xag_open_pnl_gbp": round(xag_pnl, 4),
        "last_opened_at_utc": payload.get("last_opened_at_utc") or None,
        "observed_at_utc": payload.get("observed_at_utc"),
        "source": "metals:/api/live-metals-open-positions",
        "reconciled": True, "practice_excluded": True,
        "broker_writes": False,
    }


def apply(summary: Dict[str, Any], proof: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade only current live economic facts; archive XAU-only metrics."""
    if proof.get("ok") is not True:
        raise ValueError("Cannot apply an unverified LIVE Metals basket")
    basket = summary.setdefault("basket", {})
    accounting = summary.setdefault("accounting", {})
    basket["xau_only_open_trades"] = basket.get("open_trades")
    basket["xau_only_pnl_gbp"] = basket.get("pnl_gbp")
    basket["xau_only_pnl_r"] = basket.get("pnl_r")
    basket["open_trades"] = proof["open_count"]
    basket["pnl_gbp"] = proof["open_pnl_gbp"]
    # A raw XAU R cannot be relabelled as a combined XAU/XAG portfolio R.
    basket["pnl_r"] = None
    basket["last_trade_opened_at_utc"] = proof.get("last_opened_at_utc")
    basket["live_open_scope"] = EXPECTED_SCOPE
    basket["live_xau_open_count"] = proof["xau_count"]
    basket["live_xag_short_open_count"] = proof["xag_short_count"]
    basket["live_xau_open_pnl_gbp"] = proof["xau_open_pnl_gbp"]
    basket["live_xag_short_open_pnl_gbp"] = proof["xag_open_pnl_gbp"]
    basket["open_positions_verified_at_utc"] = proof["observed_at_utc"]
    basket["live_positions_status"] = "BROKER_VERIFIED"
    basket["high_water_scope"] = "LIVE_XAU_ONLY"
    basket["giveback_scope"] = "LIVE_XAU_ONLY"
    accounting["realised_scope"] = "LIVE_XAU_ONLY_XAG_LEDGER_PENDING"
    summary["live_metals_positions"] = proof
    return summary


def mark_incomplete(summary: Dict[str, Any], reason: str) -> Dict[str, Any]:
    basket = summary.setdefault("basket", {})
    account = summary.setdefault("accounting", {})
    basket["xau_only_open_trades"] = basket.get("open_trades")
    basket["xau_only_pnl_gbp"] = basket.get("pnl_gbp")
    basket["xau_only_pnl_r"] = basket.get("pnl_r")
    basket["open_trades"] = None
    basket["pnl_gbp"] = None
    basket["pnl_r"] = None
    basket["live_positions_status"] = "UNVERIFIED"
    basket["live_positions_error"] = str(reason)[:180]
    basket["live_open_scope"] = EXPECTED_SCOPE
    basket["high_water_scope"] = "LIVE_XAU_ONLY"
    basket["giveback_scope"] = "LIVE_XAU_ONLY"
    account["realised_scope"] = "LIVE_XAU_ONLY_XAG_LEDGER_PENDING"
    summary["live_metals_positions"] = {
        "ok": False, "source": "metals:/api/live-metals-open-positions",
        "reason": str(reason)[:180],
        "broker_writes": False,
    }
    return summary
