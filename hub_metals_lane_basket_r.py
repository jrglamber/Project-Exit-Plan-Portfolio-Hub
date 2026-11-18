"""Fail-closed read-only XAG open R bridge from the authoritative Metals report."""
from __future__ import annotations

from math import isfinite

SCOPE = "LIVE_XAG_SHORT_OPEN_INITIAL_RISK"
BASIS = "SUM_OPEN_TRADE_UNREALISED_GBP_DIVIDED_BY_INITIAL_EFFECTIVE_RISK_GBP"


def _number(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if isfinite(n) else None
    except (ValueError, TypeError):
        return None


def apply(summary, lane, positions):
    """Accept R only for exactly the same verified broker population and GBP P&L."""
    if not isinstance(lane, dict) or lane.get("ok") is not True:
        raise ValueError("XAG open basket R producer unavailable")
    if (lane.get("scope") != SCOPE or lane.get("risk_basis") != BASIS
            or lane.get("reconciled") is not True
            or lane.get("practice_excluded") is not True
            or lane.get("broker_writes") is not False):
        raise ValueError("XAG open R risk basis or reconciliation invalid")
    if not isinstance(positions, dict) or positions.get("ok") is not True:
        raise ValueError("LIVE Metals broker position proof unavailable")
    if (lane.get("observed_at_utc") != positions.get("observed_at_utc")
            or lane.get("open_count") != positions.get("xag_short_count")):
        raise ValueError("XAG R position count / snapshot time mismatch")
    r = _number(lane.get("current_r"))
    cash = _number(lane.get("open_pnl_gbp"))
    verified_cash = _number(positions.get("xag_open_pnl_gbp"))
    if None in (r, cash, verified_cash) or abs(cash - verified_cash) > 0.05:
        raise ValueError("XAG R cash basis not verified against broker")
    basket = summary.setdefault("basket", {})
    basket["live_xag_short_open_r"] = r
    basket["live_xag_short_r_status"] = "BROKER_LEDGER_VERIFIED"
    basket["live_xag_short_r_basis"] = BASIS
    return summary


def mark_unavailable(summary, error):
    basket = summary.setdefault("basket", {})
    basket["live_xag_short_open_r"] = None
    basket["live_xag_short_r_status"] = "UNVERIFIED"
    basket["live_xag_short_r_error"] = str(error)[:160]
    return summary
