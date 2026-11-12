"""Portfolio Hub — combined LIVE XAU + XAG SHORT open-trade bridge.

Preserves the original Metals candidate research history while painting
the actual broker-verified LIVE XAU and XAG SHORT count in the Hub tile.
Practice XAG LONG/SHORT never counted. Fail closed on missing broker data.
"""
from __future__ import annotations

import inspect
import json
import urllib.request
from typing import Any, Dict

from fastapi.responses import Response

import hub_tile_order_overlay as base
import hub_metals_live_positions_bridge as live_bridge

app = base.app
core = base.core
HUB_VERSION = "0.6.24"
POLICY_VERSION = "hub_metals_open_trade_tile_v4_xau_xag_2026_11_12"
core.APP_VERSION = HUB_VERSION


def _fetch_metals_trade_tile() -> Dict[str, Any]:
    metals = (core.SOURCES.get("metals") or {}).get("url", "").strip().rstrip("/")
    if not metals:
        raise RuntimeError("Metals service URL not configured")
    headers = {"Accept": "application/json", "User-Agent": f"ProjectExitPlanAggregate/{HUB_VERSION}"}
    secret = str(getattr(core, "SOURCE_SECRET", "") or "").strip()
    if secret:
        headers["X-Aggregate-Secret"] = secret
    req = urllib.request.Request(metals + "/api/live-metals-open-positions", headers=headers, method="GET")
    timeout = float(getattr(core, "SOURCE_TIMEOUT_SECONDS", 5.0) or 5.0)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        if int(getattr(resp, "status", 200)) != 200:
            raise RuntimeError(f"HTTP {getattr(resp, 'status', 'error')}")
    if not isinstance(payload, dict):
        raise ValueError("Metals live trade tile payload is not an object")
    return payload


@app.get("/api/metals-live-trade-tile")
def metals_live_trade_tile() -> Dict[str, Any]:
    source = "metals:/api/live-metals-open-positions"
    try:
        payload = _fetch_metals_trade_tile()
        verified = live_bridge.verify(payload, max_age_seconds=float(getattr(core, "STALE_AFTER_SECONDS", 120)))
        return {
            "ok": True,
            "version": POLICY_VERSION,
            "source": source, "scope": live_bridge.EXPECTED_SCOPE,
            "open_count": verified["open_count"],
            "xau_count": verified["xau_count"],
            "xag_short_count": verified["xag_short_count"],
            "xag_broker_link_reconciled": verified["reconciled"],
            "open_pnl_gbp": verified["open_pnl_gbp"],
            "last_opened_at_utc": verified["last_opened_at_utc"],
            "demo_research_excluded": ["XAG_LONG","XAG_PRACTICE"],
            "strategy_changed": False,
        }
    except Exception as exc:
        return {
            "ok": False, "version": POLICY_VERSION,
            "source": source, "scope": live_bridge.EXPECTED_SCOPE,
            "open_count": None, "xau_count": None,
            "xag_short_count": None, "open_pnl_gbp": None,
            "last_opened_at_utc": None,
            "demo_research_excluded": ["XAG_LONG","XAG_PRACTICE"],
            "strategy_changed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


_WIDGET = r'''
<script id="hub-metals-open-trades-detail-v4">
(function(){
  function findMetals(){
    return Array.from(document.querySelectorAll('.strategy')).find(function(el){
      var t=el.querySelector('.strategy-title');
      return t && String(t.textContent||'').trim().toLowerCase()==='metals';
    }) || null;
  }
  function cards(){
    var s=findMetals(); return s ? Array.from(s.querySelectorAll('.card')) : [];
  }
  function byLabel(name){
    var target=String(name||'').toLowerCase();
    return cards().find(function(c){
      var l=c.querySelector('.label');
      return l && String(l.textContent||'').trim().toLowerCase()===target;
    }) || null;
  }
  function removeCandidate(){
    cards().forEach(function(c){
      var l=c.querySelector('.label');
      if(l && String(l.textContent||'').trim().toLowerCase()==='candidate support') c.remove();
    });
  }
  function whenUK(v){
    if(!v)return '-';
    try{
      return new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/London',day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(v));
    }catch(e){return String(v);}
  }
  function paint(x){
    removeCandidate();
    var c=byLabel('Open Trades');
    if(!c)return;
    var v=c.querySelector('.value');
    var s=c.querySelector('.small');
    if(!x || !x.ok){
      if(v)v.textContent='—';
      if(s)s.textContent='LIVE Metals count unavailable · broker/reconciliation check required';
      return;
    }
    var value=String(x.open_count);
    var note='LIVE XAU '+String(x.xau_count)+' · LIVE XAG SHORT '+
      String(x.xag_short_count)+' · Last opened '+whenUK(x.last_opened_at_utc);
    if(v && v.textContent!==value)v.textContent=value;
    if(s && s.textContent!==note)s.textContent=note;
  }
  async function refresh(){
    removeCandidate();
    try{
      var r=await fetch('/api/metals-live-trade-tile',{cache:'no-store'});
      if(!r.ok)throw Error("Hub tile HTTP "+r.status);
      paint(await r.json());
    }catch(e){paint({ok:false});}
  }
  function start(){ refresh(); setTimeout(refresh,750); setInterval(refresh,5000); }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();
</script>
'''


def _sync_html(text: str) -> str:
    out = str(text)
    for old in ("0.6.23", "0.6.22", "0.6.21", "0.6.20", "0.6.19", "0.6.18", "0.6.17", "0.6.16"):
        out = out.replace(f"v{old} &middot; portfolio cockpit", f"v{HUB_VERSION} &middot; portfolio cockpit")
        out = out.replace(f"v{old} · portfolio cockpit", f"v{HUB_VERSION} · portfolio cockpit")
    if "hub-metals-open-trades-detail-v4" not in out:
        out = out.replace("</body>", _WIDGET + "\n</body>", 1) if "</body>" in out else out + _WIDGET
    return out


def _install_root_wrapper() -> Dict[str, Any]:
    target = None
    for route in list(getattr(app, "routes", [])):
        if str(getattr(route, "path", "")) == "/" and "GET" in set(getattr(route, "methods", []) or []):
            target = route
            break
    if target is None:
        raise RuntimeError("Portfolio Hub root route not found")
    original = getattr(getattr(target, "dependant", None), "call", None) or getattr(target, "endpoint", None)
    if not callable(original):
        raise RuntimeError("Portfolio Hub root endpoint not callable")

    async def live_trade_root(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Response):
            raw = bytes(getattr(result, "body", b"") or b"").decode("utf-8", errors="replace")
            headers = {k: v for k, v in result.headers.items() if k.lower() not in {"content-length", "content-encoding"}}
            return Response(content=_sync_html(raw).encode("utf-8"), status_code=int(getattr(result, "status_code", 200) or 200), headers=headers, media_type=None)
        return _sync_html(str(result))

    target.endpoint = live_trade_root
    if getattr(target, "dependant", None) is not None:
        target.dependant.call = live_trade_root
    return {"ok": True, "version": POLICY_VERSION, "hub_version": HUB_VERSION, "candidate_support_tile": False, "open_trades_enriched": True}


INSTALL = _install_root_wrapper()
print(f"HUB_METALS_OPEN_TRADES installed={INSTALL.get('ok')} version={POLICY_VERSION} hub_version={HUB_VERSION}", flush=True)
