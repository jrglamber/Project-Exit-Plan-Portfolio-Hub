"""Read-only, fail-closed basket activity classification for the Portfolio Hub.

An unavailable broker position count is UNKNOWN, never zero.  An opening or
flattening event requires two consecutive verified position snapshots.
After an unverified interval report recovery, not execution of a basket.
"""
from typing import Any, Dict, Optional, Tuple


def verified_position_count(key: str, view: Dict[str, Any], data: Dict[str, Any]) -> Optional[int]:
    if not isinstance(view, dict) or view.get("ok") is not True or view.get("stale"):
        return None
    if not isinstance(data, dict) or str(data.get("mode") or "").lower()!="live":
        return None
    basket=data.get("basket") or {}
    count=basket.get("open_trades")
    # bool is a subclass of int, and 0.0 / "0" can arise from bad schemas:
    # accept only an explicitly verified broker-provided integer count.
    if type(count) is not int or count < 0:
        return None
    if key == "metals":
        proof=data.get("live_metals_positions") or {}
        if (basket.get("live_positions_status") != "BROKER_VERIFIED"
                or not isinstance(proof, dict) or proof.get("ok") is not True
                or type(proof.get("open_count")) is not int
                or proof.get("open_count") != count):
            return None
        xau=proof.get("xau_count")
        xag=proof.get("xag_short_count")
        if type(xau) is not int or type(xag) is not int or xau<0 or xag<0 or xau+xag!=count:
            return None
    return count


def position_activity(previous: Dict[str, Any], current_count: Optional[int]) -> Optional[Tuple[str,str,str]]:
    """Emit only verified transitions, and distinguish telemetry gaps from trades."""
    if not isinstance(previous,dict):
        return None
    prev_count=previous.get("open_trades")
    last_verified=previous.get("last_verified_open_trades")
    if current_count is None:
        if type(prev_count) is int:
            return ("broker_positions_unavailable",
                    "Broker position verification unavailable; basket status unknown, not flat",
                    "warn")
        return None
    if type(prev_count) is not int:
        if type(last_verified) is int:
            return ("broker_positions_reverified",
                    f"Broker positions reverified after reporting gap: {last_verified} -> {current_count} trades; not a basket reopening",
                    "info")
        return None
    if prev_count == 0 and current_count > 0:
        return ("basket_opened", f"Basket opened - {current_count} trades", "good")
    if prev_count > 0 and current_count == 0:
        return ("basket_flattened", "Basket flattened (verified broker position count zero)", "good")
    if current_count < prev_count:
        reduction=prev_count-current_count
        if reduction >= max(3,int(prev_count*0.25)):
            return ("basket_reduced", f"Basket reduced {prev_count} -> {current_count} trades", "info")
    return None
