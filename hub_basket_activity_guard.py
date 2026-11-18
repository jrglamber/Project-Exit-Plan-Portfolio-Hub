"""Truthful Hub basket activity from complete, broker-verified position snapshots.

A missing or failed position feed is UNKNOWN, never a flat basket. This module
is read-only and never generates trade instructions or broker actions.
"""

from __future__ import annotations


def _count(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return None


def position_observation(strategy, view):
    """Return (count or None, trustworthy) for activity comparison."""
    if not isinstance(view, dict) or view.get("ok") is not True or view.get("stale") is True:
        return None, False
    data = view.get("data")
    if not isinstance(data, dict) or str(data.get("mode") or "").lower() != "live":
        return None, False
    basket = data.get("basket") or {}
    count = _count(basket.get("open_trades"))
    if count is None:
        return None, False
    if strategy == "metals":
        if basket.get("live_positions_status") != "BROKER_VERIFIED":
            return None, False
        proof = data.get("live_metals_positions") or {}
        if proof.get("ok") is not True:
            return None, False
        xau = _count(proof.get("xau_count"))
        xag = _count(proof.get("xag_short_count"))
        broker_count = _count(proof.get("open_count"))
        if xau is None or xag is None or broker_count != count or xau + xag != count:
            return None, False
        ids = proof.get("broker_trade_ids")
        if not isinstance(ids, list) or len(ids) != count:
            return None, False
        normalized = [str(x).strip() for x in ids]
        if any(not x for x in normalized) or len(set(normalized)) != count:
            return None, False
    return count, True


def transition_events(strategy, previous, current):
    """Return [(kind, message, tone)] and annotate candidate on current.

    Only sequential trustworthy samples may generate opened/reduced/flattened
    events. An actual full flatten requires *two* broker-verified zero samples
    following a previously broker-verified nonzero basket.
    """
    current["pending_flatten_from"] = None
    events = []
    if not isinstance(previous, dict):
        return events
    was = previous.get("position_verified") is True
    now = current.get("position_verified") is True
    if was and not now:
        events.append((
            "position_feed_unavailable",
            "Live position verification unavailable; basket state unknown (not flat)",
            "warn",
        ))
        return events
    if not was and now:
        count = current.get("open_trades")
        events.append((
            "position_feed_recovered",
            f"Broker position data restored: {count} positions observed; no reopen inferred",
            "info",
        ))
        return events
    if not was or not now:
        return events

    before = previous["open_trades"]
    after = current["open_trades"]
    if before is None or after is None:
        return events
    if before > 0 and after == 0:
        current["pending_flatten_from"] = before
        return events
    if before == 0 and after == 0 and previous.get("pending_flatten_from"):
        events.append((
            "basket_flattened",
            "Broker-verified basket flat on two consecutive snapshots",
            "good",
        ))
        return events
    if before == 0 and after > 0:
        if previous.get("pending_flatten_from"):
            # A one-sample zero that reverted is NOT a genuine flatten+reopen.
            return events
        events.append(("basket_opened", f"Broker positions rose from 0 to {after}", "info"))
        return events
    if before > after:
        reduction = before - after
        if reduction >= max(3, int(before * .25)):
            events.append((
                "basket_reduced",
                f"Broker position count reduced {before} → {after}; closure reasons unverified",
                "info",
            ))
    return events
