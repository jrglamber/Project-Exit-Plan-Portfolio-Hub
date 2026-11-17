"""Strict, fail-closed combination of existing XAU realised and audited XAG cash.

XAU aggregate values originate from the Metals canonical summary; XAG SHORT
originates only from independently broker-verified, timestamped net GBP cash.
This is a read-only accounting projection; never changes positions or brokers.
"""
from datetime import datetime,timezone
import math

FIELDS=("realised_today_gbp","realised_week_gbp",
        "realised_month_gbp","realised_all_time_gbp")


def number(v):
    if isinstance(v,bool) or v is None:return None
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (ValueError,TypeError):
        return None


def combine_xau_xag(validated, lane_payload, now=None):
    now=now or datetime.now(timezone.utc)
    if not isinstance(lane_payload,dict):
        raise ValueError("Metals ledger producer missing")
    if lane_payload.get("xag_short_net_gbp_verified") is not True:
        raise ValueError("XAG broker net GBP not verified")
    if lane_payload.get("reconciled") is True:
        # The producer has NOT independently verified XAU long/short split.
        # Never confuse proof of XAG net cash with full per-lane accounting.
        raise ValueError("Producer unexpectedly asserts all-lane reconciliation")
    xag=lane_payload.get("xag_short_verified_realised") or {}
    if (not isinstance(xag,dict) or xag.get("ok") is not True
        or xag.get("scope")!="XAG_SHORT_BROKER_NET_CASH_GBP"):
        raise ValueError("XAG net GBP audit scope invalid")
    try:
        dt=datetime.fromisoformat(str(xag["time_utc"]).replace("Z","+00:00"))
        if dt.tzinfo is None: raise ValueError("No UTC offset")
        age=(now-dt.astimezone(timezone.utc)).total_seconds()
    except (TypeError,KeyError,ValueError) as exc:
        raise ValueError("XAG net cash audit timestamp invalid") from exc
    if age<0 or age>900:
        raise ValueError("XAG realised audit stale or future")

    source=validated.get("accounting")
    if not isinstance(source,dict):
        raise ValueError("Metals canonical XAU accounting missing")
    results={}
    for field in FIELDS:
        xau_amount=number(source.get(field))
        xag_amount=number(xag.get(field))
        if xau_amount is None or xag_amount is None:
            raise ValueError("XAU or XAG net GBP amount unavailable: "+field)
        results[field]=(xau_amount,xag_amount,round(xau_amount+xag_amount,4))
    for field,(xau,xag_amount,combined) in results.items():
        source["xau_only_"+field]=xau
        source["xag_short_"+field]=xag_amount
        source[field]=combined
    source["xag_short_broker_net_verified"]=True
    source["realised_scope"]="XAU_LEGACY_PLUS_XAG_SHORT_NET_BROKER_VERIFIED"
    source["lane_reporting_status"]="available"
    source["xag_short_accounting_time_utc"]=xag["time_utc"]
    source["xag_short_broker_trade_count"]=int(xag.get("broker_trade_count") or 0)
    # Preserve that the XAU per-side split was not independently proven.
    source["reconciled"]=False
    source["coverage_verified"]=False
    return validated
