"""Read-only production acceptance for Portfolio Hub LIVE Metals XAU + XAG.

No token and no broker writes; uses the actual deployed public Hub endpoints.
"""
import json
import os
import time
import urllib.request

BASE = os.environ.get(
    "PORTFOLIO_HUB_URL",
    "https://project-exit-plan-portfolio-hub-production.up.railway.app",
).rstrip("/")


def get(path):
    request = urllib.request.Request(BASE+path,headers={
        "User-Agent":"ProjectExitPlan-Hub-Live-Metals-Smoke/1.0",
        "Accept":"application/json,text/html",
    },method="GET")
    with urllib.request.urlopen(request,timeout=25) as response:
        if response.status != 200:
            raise AssertionError(f"{path} returned HTTP {response.status}")
        return response.read(800000).decode("utf-8","replace")


def verify():
    page=get("/")
    if "hub-metals-open-trades-detail-v4" not in page:
        raise AssertionError("Actual Hub page still has old XAU-only Metals tile script")
    if "LIVE XAU + XAG" not in page:
        raise AssertionError("Actual Hub page missing combined LIVE Metals P&L label")
    if "High water · XAU only" not in page:
        raise AssertionError("XAU-only HWM not separately labelled")

    snap=json.loads(get("/api/aggregate"))
    metals=((snap.get("sources") or {}).get("metals") or {}).get("data") or {}
    basket=metals.get("basket") or {}
    proof=metals.get("live_metals_positions") or {}
    if not proof.get("ok") or basket.get("live_positions_status")!="BROKER_VERIFIED":
        raise AssertionError("Hub has not yet applied broker-verified XAU+XAG positions")
    xau=proof.get("xau_count")
    xag=proof.get("xag_short_count")
    combined=proof.get("open_count")
    if not all(isinstance(v,int) for v in [xau,xag,combined]):
        raise AssertionError("Live Metals counts unavailable")
    if xau+xag!=combined or basket.get("open_trades")!=combined:
        raise AssertionError("Hub Metals open trade total does not include both live lanes")
    if basket.get("live_xag_short_open_count")!=xag:
        raise AssertionError("Hub Metals XAG Short count missing")
    if xag<1:
        raise AssertionError("No live XAG Short trade available to prove dashboard inclusion")
    if abs(float(basket.get("pnl_gbp"))-float(proof["open_pnl_gbp"]))>0.001:
        raise AssertionError("Hub Metals P&L differs from its broker-verified combined P&L")
    if basket.get("pnl_r") is not None:
        raise AssertionError("Mixed XAU/XAG basket R erroneously computed from XAU only")
    if basket.get("high_water_scope")!="LIVE_XAU_ONLY":
        raise AssertionError("Hub incorrectly treats XAU high-water as all-Metals")
    if (metals.get("accounting") or {}).get("realised_scope")!="LIVE_XAU_ONLY_XAG_LEDGER_PENDING":
        raise AssertionError("Unreconciled XAG realised ledger not disclosed")

    portfolio=snap.get("portfolio") or {}
    if not portfolio.get("unrealised_pnl_complete"):
        raise AssertionError("Portfolio headline current open P&L is incomplete")
    sources=snap.get("sources") or {}
    included=[(sources.get(k) or {}).get("data") or {}
              for k in ("indices","metals","bco")]
    calc=sum(float(d.get("basket",{}).get("pnl_gbp")) for d in included
             if d.get("mode")=="live")
    if abs(calc-float(portfolio["unrealised_pnl_gbp"]))>0.01:
        raise AssertionError("Portfolio headline open P&L double-counted or missed XAG")
    if portfolio.get("open_risk_estimate_complete") is True:
        raise AssertionError("XAG pilot risk incorrectly represented as XAU base risk")

    tile=json.loads(get("/api/metals-live-trade-tile"))
    if tile.get("source")!="metals:/api/live-metals-open-positions" or not tile.get("ok"):
        raise AssertionError("Metals tile still reads XAU-only endpoint")
    if tile.get("open_count")!=tile.get("xau_count")+tile.get("xag_short_count"):
        raise AssertionError("Metals tile lacks XAG Short positions")

    print(f"PASS: served Hub HTML uses combined Metals feed; LIVE XAU={xau}, XAG_SHORT={xag}, total={combined}; broker-verified all-live P&L; XAU-only historical figures; XAG risk correctly flagged partial.")


def main():
    error=""
    for i in range(25):
        try:
            verify()
            return
        except Exception as exc:
            error=f"{type(exc).__name__}: {exc}"
            print(f"Hub production not yet verified ({i+1}/25): {error}")
            time.sleep(8)
    raise SystemExit("Hub LIVE Metals production smoke failed: "+error)


if __name__=="__main__":
    main()
