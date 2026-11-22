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
    # The browser renders these cards from the API, not static screenshots.
    request=urllib.request.Request(HUB+"/",headers={
        "Accept":"text/html","User-Agent":"PEP-Metals-R-Cards-ReadOnly-Canary/1.0"})
    with urllib.request.urlopen(request,timeout=15) as response:
        html=response.read(1200000).decode("utf-8","replace")
    if "Open basket · R / P&L" not in html or "XAU high-water R" not in html:
        raise AssertionError("Portfolio Hub is missing the combined basket R/P&L or XAU high-water R cards")
    # A separate, read-only source must now power the Metals weekly/monthly
    # accounting dashboard. Confirm its data is honest, not merely present.
    periods=_read(METALS,"/api/live-metals-period-accounting")
    if (periods.get("version")!="live_metals_period_accounting_v1_20261122"
            or periods.get("scope")!="LIVE_XAU_LONG_SHORT_PLUS_LIVE_XAG_SHORT"
            or periods.get("read_only") is not True
            or periods.get("execution_authority") is not False):
        raise AssertionError("Metals period accounting contract missing or unsafe")
    xv=(periods.get("xau") or {}).get("verified_since_epoch") is True
    sv=(periods.get("xag") or {}).get("broker_cash_verified") is True
    if periods.get("ok") is not (xv and sv):
        raise AssertionError("Metals period accounting verification status inconsistent")
    for name in ("weekly","monthly"):
        arr=periods.get(name)
        if not isinstance(arr,list) or not arr:
            raise AssertionError("Metals period accounting "+name+" rows missing")
        for row in arr:
            x=row.get("xau_gbp")
            g=row.get("xag_gbp")
            c=row.get("combined_gbp")
            if c is not None:
                if (x is None or g is None or not (xv and sv)
                        or abs((float(x)+float(g))-float(c))>0.021):
                    raise AssertionError("Metals period accounting falsely combines cash")
            if not xv and x is not None:
                raise AssertionError("Unverified XAU cash shown in period table")
            if not sv and g is not None:
                raise AssertionError("Unverified XAG cash shown in period table")
    print("METALS_PERIOD_CASH_STATUS",json.dumps({
        "xau_verified_since_epoch": xv,
        "xag_broker_verified": sv,
        "current_week_combined_present": periods["weekly"][0].get("combined_gbp") is not None,
        "current_month_combined_present": periods["monthly"][0].get("combined_gbp") is not None,
    }))
    # Server must include the panel itself, not only expose a JSON endpoint.
    request=urllib.request.Request(METALS+"/dashboard",headers={
        "Accept":"text/html","User-Agent":"PEP-Metals-Period-UI-ReadOnly-Canary/1.0"})
    with urllib.request.urlopen(request,timeout=15) as response:
        period_html=response.read(1500000).decode("utf-8","replace")
    if "pep-metals-period-accounting" not in period_html:
        raise AssertionError("Metals dashboard is missing weekly/monthly accounting panel")
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
    # Combined XAU + XAG GBP and XAU-only R are independent scopes.
    if basket.get("live_positions_status")=="BROKER_VERIFIED":
        if ("xau_only_pnl_r" not in basket or
                "xau_only_high_water_r" not in basket):
            raise AssertionError("Original XAU-only R not preserved by the combined broker bridge")
        if basket.get("pnl_r") is not None or basket.get("high_water_r") is not None:
            raise AssertionError("XAU-only R incorrectly advertised as combined Metals R")
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
