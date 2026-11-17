"""Read-only live Hub acceptance for broker-verified XAG net GBP realised cash."""
import json
import time
from urllib.request import Request,urlopen
from datetime import datetime,timezone

BASE="https://project-exit-plan-portfolio-hub-production.up.railway.app"

def get(path):
    req=Request(BASE+path,method="GET",headers={
        "Accept":"application/json,text/html",
        "User-Agent":"PEP-XAG-Live-Net-Cash-ReadOnly"})
    with urlopen(req,timeout=35) as r:
        if r.status!=200: raise AssertionError(f"{path} HTTP {r.status}")
        raw=r.read(1500000).decode("utf-8","replace")
        return json.loads(raw) if path.startswith("/api") else raw

def check():
    html=get("/")
    if "XAU + XAG SHORT net GBP verified" not in html:
        raise AssertionError("Hub lacks combined realised accounting label")
    aggregate=get("/api/aggregate")
    metals=((aggregate.get("sources") or {}).get("metals") or {}).get("data") or {}
    p=aggregate.get("portfolio") or {}
    a=metals.get("accounting") or {}
    if not a.get("xag_short_broker_net_verified"):
        raise AssertionError("Hub still has unverified XAG net cash")
    if a.get("realised_scope")!="XAU_LEGACY_PLUS_XAG_SHORT_NET_BROKER_VERIFIED":
        raise AssertionError("Combined XAU + XAG historical realised scope incorrect")
    if a.get("lane_reporting_status")!="available":
        raise AssertionError("Combined Metals realised status still pending")
    if a.get("reconciled") is True:
        raise AssertionError("XAU LONG/SHORT lane breakdown falsely marked verified")
    if a.get("xau_long_short_historical_split_status")!="awaiting_reconciliation":
        raise AssertionError("XAU long/short historical split not disclosed")
    for period in ("week","month","all_time","today"):
        key=f"realised_{period}_gbp"
        xau=a.get("xau_only_"+key)
        xag=a.get("xag_short_"+key)
        combined=a.get(key)
        if xag is None:raise AssertionError("Missing XAG net cash "+period)
        if xau is None:
            if period!="today" or combined is not None:
                raise AssertionError("Missing XAU must never be treated as zero for "+period)
        elif combined is None or abs(float(xau)+float(xag)-float(combined))>0.01:
            raise AssertionError("XAG net cash not reconciled exactly once for "+period)
    if p.get("metals_realised_scope")!="XAU_LEGACY_PLUS_XAG_SHORT_NET_BROKER_VERIFIED":
        raise AssertionError("Portfolio header not updated to combined scope")
    sources=aggregate.get("sources") or {}
    for period in ("week","month","all_time"):
        field=f"realised_{period}_gbp"
        total=0.0
        for key in ("indices","metals","bco"):
            d=((sources.get(key) or {}).get("data") or {})
            if str(d.get("mode") or "").lower()!="live":continue
            value=(d.get("accounting") or {}).get(field)
            # The existing Hub explicitly reports "known realised" values:
            # historical Indices all-time can be unavailable, and must not
            # block Metals XAU+XAG broker reconciliation or be assumed zero.
            if value is not None:
                total+=float(value)
        portfolio=p.get(field)
        if portfolio is None or abs(total-float(portfolio))>0.03:
            raise AssertionError("Portfolio "+field+" not equal to included strategies")
    if any(item.get("strategy")=="metals" and
           "lane accounting awaiting reconciliation" in str(item.get("message") or "").lower()
           for item in (aggregate.get("attention") or {}).get("items",[])):
        raise AssertionError("Old XAG lane-reconciliation warning still active")
    stamp=datetime.fromisoformat(a["xag_short_accounting_time_utc"].replace("Z","+00:00"))
    age=(datetime.now(timezone.utc)-stamp).total_seconds()
    if age<0 or age>900: raise AssertionError("XAG net cash evidence stale")
    print("PASS: Hub realised XAU + XAG broker cash reconciled, unmodified XAU source retained, "
          f"XAG all time {float(a['xag_short_realised_all_time_gbp']):.2f} GBP, "
          f"Metals combined all time {float(a['realised_all_time_gbp']):.2f} GBP, "
          f"portfolio all time {float(p['realised_all_time_gbp']):.2f} GBP; "
          "no pending XAG warning or double count.")

if __name__=="__main__":
    err=""
    for i in range(24):
        try:check();break
        except Exception as exc:
            err=f"{type(exc).__name__}: {exc}"
            print("Attempt",i+1,"error:",err)
            time.sleep(7)
    else:raise SystemExit("FAIL: "+err)
