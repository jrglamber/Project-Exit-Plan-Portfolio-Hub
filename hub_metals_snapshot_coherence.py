"""Check that independently read live Metals risk and position feeds describe the same broker trade population.

Read-only. A mismatch means inconsistent snapshots, NOT proof of missing
broker stop orders. Never infer a position count from base-risk calculations.
"""

def _nonnegative_int(value):
    if isinstance(value, bool): return None
    try:
        number = float(value)
        return int(number) if number >= 0 and number == int(number) else None
    except (ValueError, TypeError): return None


def population_consistent(positions, risk):
    """True/False when risk declares comparable complete broker scope; None if unavailable."""
    if not isinstance(positions, dict) or not isinstance(risk, dict):
        return None
    if risk.get("ok") is not True or positions.get("ok") is not True:
        return None
    count = _nonnegative_int(positions.get("count"))
    risk_count = _nonnegative_int(risk.get("open_trade_count"))
    xau = _nonnegative_int(positions.get("xau_count"))
    xag = _nonnegative_int(positions.get("xag_short_count"))
    instruments = risk.get("by_instrument")
    if None in (count, risk_count, xau, xag) or not isinstance(instruments, dict):
        return None
    xr = instruments.get("XAU_USD") or {}
    sr = instruments.get("XAG_USD") or {}
    if _nonnegative_int(xr.get("open_trade_count", 0 if xau == 0 else None)) is None:
        return None
    if _nonnegative_int(sr.get("open_trade_count", 0 if xag == 0 else None)) is None:
        return None
    # A legacy XAU-only risk endpoint is not a comparable combined witness.
    if _nonnegative_int(xr.get("open_trade_count", 0)) + _nonnegative_int(sr.get("open_trade_count", 0)) != risk_count:
        return None
    if count != risk_count or xau != _nonnegative_int(xr.get("open_trade_count", 0)) or xag != _nonnegative_int(sr.get("open_trade_count", 0)):
        return False
    pos_ids = positions.get("trades")
    risk_ids = risk.get("broker_trade_ids")
    if not isinstance(pos_ids, list) or not isinstance(risk_ids, list):
        return None
    a = [str(x.get("broker_trade_id") or "").strip() for x in pos_ids if isinstance(x, dict)]
    b = [str(x).strip() for x in risk_ids]
    if len(a) != count or len(b) != count or any(not x for x in a+b):
        return False
    if len(set(a)) != count or len(set(b)) != count:
        return False
    return set(a) == set(b)
