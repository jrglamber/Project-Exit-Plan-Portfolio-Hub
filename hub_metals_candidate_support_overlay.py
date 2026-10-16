"""Portfolio Hub v0.6.17 — live-XAU-only Metals Candidate Support.

Presentation/read-only bridge. The Hub consumes the Metals producer's
/api/live-xau-candidate-support endpoint so both dashboards use one definition:
1/1 LONG, 1/1 SHORT, or 0/1 NONE. XAG demo/research is excluded.
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
HUB_VERSION = "0.6.17"
POLICY_VERSION = "hub_metals_live_xau_candidate_support_v1_2026_10_16"
core.APP_VERSION = HUB_VERSION


def _fetch_metals_candidate_support() -> Dict[str, Any]:
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
        metals + "/api/live-xau-candidate-support",
        headers=headers,
        method="GET",
    )
    timeout = float(getattr(core, "SOURCE_TIMEOUT_SECONDS", 5.0) or 5.0)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        if int(getattr(resp, "status", 200)) != 200:
            raise RuntimeError(f"HTTP {getattr(resp, 'status', 'error')}")
    if not isinstance(payload, dict):
        raise ValueError("Metals candidate support payload is not an object")
    return payload


@app.get("/api/metals-live-candidate-support")
def metals_live_candidate_support() -> Dict[str, Any]:
    try:
        payload = _fetch_metals_candidate_support()
        return {
            "ok": bool(payload.get("ok", True)),
            "source": "metals:/api/live-xau-candidate-support",
            "scope": "LIVE_XAU_ONLY",
            "supported": int(payload.get("supported") or 0),
            "expected": 1,
            "side": str(payload.get("side") or "NONE").upper(),
            "display": str(payload.get("display") or "0/1 · NONE"),
            "latest": payload.get("latest") or {},
            "demo_research_excluded": ["XAGUSD"],
            "strategy_changed": False,
        }
    except Exception as exc:
        return {
            "ok": False,
            "source": "metals:/api/live-xau-candidate-support",
            "scope": "LIVE_XAU_ONLY",
            "supported": 0,
            "expected": 1,
            "side": "UNKNOWN",
            "display": "—/1 · UNKNOWN",
            "latest": {},
            "demo_research_excluded": ["XAGUSD"],
            "strategy_changed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


_WIDGET = r'''
<script id="hub-metals-live-xau-candidate-support">
(function(){
  function findMetals(){
    return Array.from(document.querySelectorAll('.strategy')).find(function(el){
      var t=el.querySelector('.strategy-title');
      return t && String(t.textContent||'').trim().toLowerCase()==='metals';
    }) || null;
  }
  function ensureCard(){
    var section=findMetals(); if(!section)return null;
    var grid=section.querySelector('.grid.cards')||section.querySelector('.cards'); if(!grid)return null;
    var card=document.getElementById('metals-live-candidate-support-card');
    if(!card){
      card=document.createElement('div');
      card.className='card';
      card.id='metals-live-candidate-support-card';
      card.innerHTML='<div class="label">Candidate Support</div><div class="value" id="metals-live-candidate-support-value">—/1 · UNKNOWN</div><div class="small" id="metals-live-candidate-support-note">LIVE XAU only · LONG or SHORT · XAG research excluded</div>';
      grid.appendChild(card);
    }
    return card;
  }
  function paint(x){
    var card=ensureCard(); if(!card)return;
    var value=card.querySelector('#metals-live-candidate-support-value');
    var note=card.querySelector('#metals-live-candidate-support-note');
    if(value){
      value.textContent=(x&&x.display)||'—/1 · UNKNOWN';
      value.className='value '+((x&&x.ok&&Number(x.supported||0)>0)?'green':((x&&x.ok)?'':'amber'));
    }
    if(note){
      var candle=x&&x.latest&&x.latest.candle ? ' · candle '+x.latest.candle : '';
      note.textContent=(x&&x.ok?'LIVE XAU only · current candidate':'LIVE XAU candidate telemetry unavailable')+candle+' · XAG research excluded';
    }
  }
  async function refresh(){
    ensureCard();
    try{
      var r=await fetch('/api/metals-live-candidate-support',{cache:'no-store'});
      var x=await r.json(); paint(x);
    }catch(e){paint({ok:false,display:'—/1 · UNKNOWN'});}
  }
  var observer=new MutationObserver(function(){ensureCard();});
  if(document.body)observer.observe(document.body,{childList:true,subtree:true});
  else document.addEventListener('DOMContentLoaded',function(){observer.observe(document.body,{childList:true,subtree:true});refresh();});
  refresh(); setInterval(refresh,15000);
})();
</script>
'''


def _sync_html(text: str) -> str:
    out = str(text)
    out = out.replace("v0.6.16 &middot; portfolio cockpit", "v0.6.17 &middot; portfolio cockpit")
    out = out.replace("v0.6.16 · portfolio cockpit", "v0.6.17 · portfolio cockpit")
    if "hub-metals-live-xau-candidate-support" not in out:
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

    async def candidate_support_root(*args: Any, **kwargs: Any):
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

    target.endpoint = candidate_support_root
    if getattr(target, "dependant", None) is not None:
        target.dependant.call = candidate_support_root
    return {"ok": True, "version": POLICY_VERSION, "hub_version": HUB_VERSION}


INSTALL = _install_root_wrapper()
print(
    f"HUB_METALS_CANDIDATE_SUPPORT installed={INSTALL.get('ok')} version={POLICY_VERSION} hub_version={HUB_VERSION}",
    flush=True,
)
