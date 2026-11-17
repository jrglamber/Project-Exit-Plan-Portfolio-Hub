"""Production read-only acceptance: XAU + XAG stops and projected NAV."""
import json
import time
import urllib.request

BASE="https://project-exit-plan-portfolio-hub-production.up.railway.app"

def get(path):
    req=urllib.request.Request(BASE+path,headers={
        "User-Agent":"PEP-Hub-Stop-Floor-Smoke","Accept":"application/json,text/html"},
        method="GET")
    with urllib.request.urlopen(req,timeout=30) as response:
        if response.status!=200: raise AssertionError(path+" not HTTP 200")
        body=response.read(1000000).decode("utf-8","replace")
        return json.loads(body) if path!="/" else body

def verify():
    html=get("/")
    if "Projected NAV at stops:" not in html:
        raise AssertionError("Hub still uses old double-counted stop-floor NAV")
    snap=get("/api/aggregate")
    data=((snap.get("sources") or {}).get("metals") or {}).get("data") or {}
    b=data.get("basket") or {}
    p=snap.get("portfolio") or {}
    ids=(data.get("live_metals_positions") or {}).get("broker_trade_ids") or []
    risk_ids=data.get("live_risk_broker_trade_ids") or []
    expected=b.get("open_trades")
    quantified=data.get("live_risk_quantified_trade_count")
    risk_count=data.get("live_risk_open_trade_count")
    by=data.get("live_risk_by_instrument") or {}
    xau=by.get("XAU_USD") or {}
    xag=by.get("XAG_USD") or {}
    if b.get("live_positions_status")!="BROKER_VERIFIED":
        raise AssertionError("Metals position broker scope not verified")
    if not isinstance(expected,int) or expected<1 or b.get("live_xag_short_open_count",0)<1:
        raise AssertionError("No broker-verified live XAG Short scope")
    if expected!=risk_count or expected!=quantified:
        raise AssertionError("Metals stop-risk count not fully quantified")
    if set(map(str,ids)) != set(map(str,risk_ids)) or len(ids)!=expected or len(risk_ids)!=expected:
        raise AssertionError("Broker trade ID coverage differs")
    if xau.get("quantified_trade_count")!=b.get("live_xau_open_count"):
        raise AssertionError("XAU broker stops unquantified")
    if xag.get("quantified_trade_count")!=b.get("live_xag_short_open_count"):
        raise AssertionError("XAG Short broker stops unquantified")
    if xag.get("short_trade_count")!=b.get("live_xag_short_open_count"):
        raise AssertionError("XAG scope not all short")
    if p.get("live_stop_floor_scope_complete") is not True:
        raise AssertionError("Hub scope guard still marks verified Metals stops incomplete")
    if p.get("live_stop_floor_metals_excluded_count")!=0:
        raise AssertionError("Hub still reports excluded broker stops")
    if p.get("live_stop_floor_complete") is not True:
        raise AssertionError("Not every live portfolio stop quantified")
    floor=p.get("live_stop_floor_pnl_gbp")
    if floor is None:
        raise AssertionError("Hub does not expose a complete stop-floor GBP value")
    if not p.get("unrealised_pnl_complete"):
        raise AssertionError("Current open P&L incomplete; projected NAV cannot be computed")
    nav=p.get("nav_gbp")
    upl=p.get("unrealised_pnl_gbp")
    if nav is None or upl is None:
        raise AssertionError("Cannot compute projected stop-floor NAV")
    projected=float(nav)-float(upl)+float(floor)
    if not 0<projected<float(nav)*2:
        raise AssertionError("Projected stop NAV implausible")
    print(f"PASS: Hub broker-verified XAU {b['live_xau_open_count']}, "
          f"XAG SHORT {b['live_xag_short_open_count']}, stops quantified {quantified}/{expected}; "
          f"floor GBP {float(floor):.2f}; projected NAV GBP {projected:.2f} "
          "(NAV - current open P&L + terminal stop P&L).")

if __name__=="__main__":
    err=""
    for attempt in range(25):
        try:
            verify()
            break
        except Exception as exc:
            err=f"{type(exc).__name__}: {exc}"
            print("Attempt",attempt+1,"read-only scope check:",err)
            time.sleep(8)
    else:
        raise SystemExit("FAIL: "+err)
