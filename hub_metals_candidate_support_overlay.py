"""Portfolio Hub v0.6.18 — consolidate Metals live trade status.

Presentation/read-only bridge:
- removes the separate Metals Candidate Support tile;
- enriches the existing Metals Open Trades tile with LONG/SHORT counts and
  last-opened UK date/time;
- reads only the Metals live-XAU telemetry endpoint;
- XAG demo/research remains excluded.
"""
from __future__ import annotations

import inspect
import json
import urllib.request
from typing import Any, Dict

from fastapi.responses import Response

import hub_tile_order_overlay as base

app = base.app
core = base.core
HUB_VERSION = "0.6.18"
POLICY_VERSION = "hub_metals_open_trade_tile_v2_2026_10_16"
core.APP_VERSION = HUB_VERSION


def _fetch_metals_trade_tile() -> Dict[str, Any]:
    metals = (core.SOURCES.get("metals") or {}).get("url", "").strip().rstrip("/")
    if not metals:
        return {"ok": False, "error": "Metals service URL not configured"}
    headers = {
        "Accept": "application/json",
        "User-Agent": f"ProjectExitPlanAggregate/{HUB_VERSION}",
    }
    secret = str(getattr(core, "SOURCE_SECRET", "") or "").strip()
    if secret:
        headers["X-Aggregate-Secret"] = secret
    req = urllib.request.Request(
        metals + "/api/live-xau-trade-tile",
        headers=headers,
        method="GET",
    )
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
    try:
        payload = _fetch_metals_trade_tile()
        return {
            "ok": bool(payload.get("ok", True)),
            "source": "metals:/api/live-xau-trade-tile",
            "scope": "LIVE_XAU_ONLY",
            "open_count": int(payload.get("open_count") or 0),
            "long_count": int(payload.get("long_count") or 0),
            "short_count": int(payload.get("short_count") or 0),
            "last_opened_at_utc": payload.get("last_opened_at_utc"),
            "demo_research_excluded": ["XAGUSD"],
            "strategy_changed": False,
        }
    except Exception as exc:
        return {
            "ok": False,
            "source": "metals:/api/live-xau-trade-tile",
            "scope": "LIVE_XAU_ONLY",
            "open_count": None,
            "long_count": None,
            "short_count": None,
            "last_opened_at_utc": None,
            "demo_research_excluded": ["XAGUSD"],
            "strategy_changed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


_WIDGET = r'''
<script id="hub-metals-open-trades-detail">
(function(){
  var latest=null;
  function findMetals(){
    return Array.from(document.querySelectorAll('.strategy')).find(function(el){
      var t=el.querySelector('.strategy-title');
      return t && String(t.textContent||'').trim().toLowerCase()==='metals';
    }) || null;
  }
  function removeCandidateSupport(){
    var old=document.getElementById('metals-live-candidate-support-card');
    if(old)old.remove();
    var section=findMetals(); if(!section)return;
    Array.from(section.querySelectorAll('.card')).forEach(function(card){
      var label=card.querySelector('.label');
      if(label && String(label.textContent||'').trim().toLowerCase()==='candidate support')card.remove();
    });
  }
  function findOpenTradesCard(){
    var section=findMetals(); if(!section)return null;
    return Array.from(section.querySelectorAll('.card')).find(function(card){
      var label=card.querySelector('.label');
      return label && String(label.textContent||'').trim().toLowerCase()==='open trades';
    }) || null;
  }
  function whenUK(v){
    if(!v)return '-';
    try{
      return new Intl.DateTimeFormat('en-GB',{
        timeZone:'Europe/London',day:'2-digit',month:'short',year:'numeric',
        hour:'2-digit',minute:'2-digit',hour12:false
      }).format(new Date(v));
    }catch(e){return String(v);}
  }
  function paint(x){
    latest=x||latest;
    removeCandidateSupport();
    var card=findOpenTradesCard(); if(!card || !latest)return;
    var value=card.querySelector('.value');
    var note=card.querySelector('.small');
    if(latest.ok){
      if(value)value.textContent=String(latest.open_count||0);
      if(note)note.textContent=String(latest.long_count||0)+' LONG · '+String(latest.short_count||0)+' SHORT · Last opened '+whenUK(latest.last_opened_at_utc);
    }else{
      if(note)note.textContent='LIVE XAU trade detail unavailable';
    }
  }
  async function refresh(){
    removeCandidateSupport();
    try{
      var r=await fetch('/api/metals-live-trade-tile',{cache:'no-store'});
      var x=await r.json(); paint(x);
    }catch(e){paint({ok:false});}
  }
  var observer=new MutationObserver(function(){removeCandidateSupport();if(latest)paint(latest);});
  function start(){observer.observe(document.body,{childList:true,subtree:true});refresh();setInterval(refresh,15000);}
  if(document.body)start(); else document.addEventListener('DOMContentLoaded',start);
})();
</script>
'''


def _sync_html(text: str) -> str:
    out = str(text)
    for old in ("0.6.17", "0.6.16"):
        out = out.replace(f"v{old} &middot; portfolio cockpit", f"v{HUB_VERSION} &middot; portfolio cockpit")
        out = out.replace(f"v{old} · portfolio cockpit", f"v{HUB_VERSION} · portfolio cockpit")
    if "hub-metals-open-trades-detail" not in out:
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
            return Response(
                content=_sync_html(raw).encode("utf-8"),
                status_code=int(getattr(result, "status_code", 200) or 200),
                headers=headers,
                media_type=None,
            )
        return _sync_html(str(result))

    target.endpoint = live_trade_root
    if getattr(target, "dependant", None) is not None:
        target.dependant.call = live_trade_root
    return {
        "ok": True,
        "version": POLICY_VERSION,
        "hub_version": HUB_VERSION,
        "candidate_support_tile": False,
        "open_trades_enriched": True,
    }


INSTALL = _install_root_wrapper()
print(
    f"HUB_METALS_OPEN_TRADES installed={INSTALL.get('ok')} version={POLICY_VERSION} hub_version={HUB_VERSION} candidate_tile={INSTALL.get('candidate_support_tile')}",
    flush=True,
)
