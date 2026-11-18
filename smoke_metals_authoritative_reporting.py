"""Read-only production canary for canonical Metals reporting.

Checks invariants, not specific prices, trades or P&L amounts. Uses no tokens,
prints no broker trade IDs, and never issues broker execution requests.
"""
import json
import os
import time
import urllib.request

METALS=os.getenv("METALS_CANARY_URL","https://metals-production-bb81.up.railway.app").rstrip("/")
HUB=os.getenv("HUB_CANARY_URL","https://project-exit-plan-portfolio-hub-production.up.railway.app").rstrip("/")
VERSION="metals_reporting_snapshot_v1_20261118"
CASH_SCOPE="XAU_LEGACY_PLUS_XAG_SHORT_NET_BROKER_VERIFIED"


def _read(base,path):
    req=urllib.request.Request(base+path,headers={
        "Accept":"application/json","User-Agent":"PEP-Metals-ReadOnly-Canary/1.0"})
    with urllib.request.urlopen(req,timeout=15) as response:
        if response.status!=200:
            raise AssertionError("Unexpected status for "+path)
        return json.load(response)


def verify():
    producer=_read(METALS,"/api/live-metals-reporting-snapshot")
    hub=_read(HUB,"/api/aggregate")
    if producer.get("version")!=VERSION:
        raise AssertionError("Metals producer contract version mismatch")
    source=((hub.get("sources") or {}).get("metals") or {}).get("data") or {}
    meta=source.get("live_metals_report") or {}
    if meta.get("version")!=VERSION:
        raise AssertionError("Portfolio Hub not consuming authoritative Metals contract")
    pp=producer.get("positions") or {}
    pcomb=producer.get("combined") or {}
    prisk=producer.get("risk") or {}
    cash=producer.get("cash") or {}
    account=source.get("accounting") or {}
    basket=source.get("basket") or {}
    portfolio=hub.get("portfolio") or {}
    if account.get("realised_scope")== "COMBINED_METALS_CASH_UNVERIFIED":
        for field in ("realised_week_gbp","realised_month_gbp","realised_all_time_gbp"):
            if account.get(field) is not None:
                raise AssertionError("Unverified combined Metals cash advertised as full amount")
    if account.get("xag_short_broker_net_verified") is True:
        if account.get("realised_scope")!=CASH_SCOPE:
            raise AssertionError("Broker-verified XAG cash has the wrong scope")
        if account.get("realised_week_gbp") is None or account.get("realised_month_gbp") is None:
            raise AssertionError("Verified Metals accounting missing week/month totals")
    if basket.get("live_positions_status")=="BROKER_VERIFIED":
        if not isinstance(basket.get("open_trades"),int) or basket.get("pnl_gbp") is None:
            raise AssertionError("Broker verified basket has incomplete open position data")
    if portfolio.get("live_stop_floor_scope_status")=="COMPLETE":
        if basket.get("live_positions_status")!="BROKER_VERIFIED":
            raise AssertionError("Stop-floor complete without broker-verified positions")
        if (source.get("live_risk_open_trade_count")!=basket.get("open_trades")
             or source.get("live_risk_quantified_trade_count")!=basket.get("open_trades")):
            raise AssertionError("Incomplete broker stop-floor labelled complete")
        pids=(source.get("live_metals_positions") or {}).get("broker_trade_ids") or []
        rids=source.get("live_risk_broker_trade_ids") or []
        if sorted(map(str,pids))!=sorted(map(str,rids)):
            raise AssertionError("Stop-floor complete despite broker ID mismatch")
    status={
        "contract":meta.get("version"),
        "producer_positions":pp.get("count"),
        "producer_cash_status":cash.get("status"),
        "producer_risk_status":prisk.get("status"),
        "hub_open_status":basket.get("live_positions_status"),
        "hub_risk_status":meta.get("risk_status"),
        "hub_cash_status":meta.get("cash_status"),
        "hub_stop_floor":portfolio.get("live_stop_floor_scope_status"),
    }
    print("CANARY",json.dumps(status))
    return bool(
        pp.get("ok") is True and pcomb.get("ok") is True
        and cash.get("ok") is True and prisk.get("ok") is True
        and basket.get("live_positions_status")=="BROKER_VERIFIED"
        and account.get("xag_short_broker_net_verified") is True
        and portfolio.get("live_stop_floor_scope_status")=="COMPLETE"
    )


def main():
    last_error=None
    for attempt in range(5):
        try:
            if verify():
                print("PASS canonical Metals reporting and Portfolio Hub verified end to end")
                return
            last_error="At least one independently verified reporting area not complete"
        except Exception as exc:
            last_error=f"{type(exc).__name__}: {str(exc)[:180]}"
            print("CANARY_ATTEMPT_FAILED",attempt+1,last_error)
        if attempt<4:
            time.sleep(8)
    raise SystemExit("Metals reporting contract unhealthy: "+str(last_error))


if __name__=="__main__":
    main()
