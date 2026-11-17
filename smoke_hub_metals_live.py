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
    if "High water · LIVE XAU + XAG SHORT" not in page:
        raise AssertionError("Hub combined observed high-water tile missing")
    if "Giveback · LIVE XAU + XAG SHORT" not in page:
        raise AssertionError("Hub combined observed giveback tile missing")

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
    combined_hwm=metals.get("live_metals_high_water") or {}
    if (not combined_hwm.get("ok") or
        basket.get("combined_high_water_status")!="OBSERVED_COMBINED_LIVE" or
        basket.get("high_water_scope")!="LIVE_XAU_PLUS_XAG_SHORT_OBSERVED_SINCE_START"):
        raise AssertionError("Hub combined observed Metals high-water unavailable")
    if basket.get("xau_only_high_water_gbp") is None:
        raise AssertionError("Hub lost separate XAU protection high-water")
    if basket.get("high_water_r") is not None or basket.get("giveback_r") is not None:
        raise AssertionError("Combined Metals wrongly reused XAU-only R")
    if abs(float(basket["giveback_gbp"]) -
           max(0.0,float(basket["high_water_gbp"])-float(basket["pnl_gbp"])))>0.02:
        raise AssertionError("Combined high-water, broker P&L and giveback do not reconcile")
    if not basket.get("high_water_tracking_started_at_utc"):
        raise AssertionError("Hub combined high-water observation baseline missing")
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
    # Stop-floor coverage is a separate broker-risk contract from live P&L.
    # A 16-stop XAU-only subset must never look like all 38 Metals positions.
    counted=metals.get("live_risk_open_trade_count")
    broker_positions=basket.get("open_trades")
    if isinstance(counted,int) and isinstance(broker_positions,int) and counted != broker_positions:
        if portfolio.get("live_stop_floor_scope_complete") is not False:
            raise AssertionError("Missing stop-floor coverage warning for LIVE XAG SHORT")
        if portfolio.get("live_stop_floor_pnl_gbp") is not None:
            raise AssertionError("Partial XAU-only stop floor falsely shown as complete")
        if portfolio.get("live_stop_floor_metals_excluded_count") != max(0,broker_positions-counted):
            raise AssertionError("Uncovered Metals stop-trade count inconsistent")
        if "Stop-floor incomplete" not in page:
            raise AssertionError("Hub stop-floor card still claims complete protection")
        if not any("stop-floor coverage incomplete" in str(x.get("message") or "").lower()
                   for x in (snap.get("attention") or {}).get("items",[])):
            raise AssertionError("Partial stop coverage not raised as an attention warning")

    tile=json.loads(get("/api/metals-live-trade-tile"))
    if tile.get("source")!="metals:/api/live-metals-open-positions" or not tile.get("ok"):
        raise AssertionError("Metals tile still reads XAU-only endpoint")
    if tile.get("open_count")!=tile.get("xau_count")+tile.get("xag_short_count"):
        raise AssertionError("Metals tile lacks XAG Short positions")

    print(f"PASS: served Hub HTML uses combined Metals feed; LIVE XAU={xau}, XAG_SHORT={xag}, total={combined}; broker-verified all-live P&L; combined observed high-water + giveback; XAU historical/harvest HWM preserved; XAG risk flagged partial.")


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
