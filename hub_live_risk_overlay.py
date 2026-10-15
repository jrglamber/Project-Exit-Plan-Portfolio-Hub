"""Portfolio Hub v0.6.12 — broker-backed live capital-at-risk telemetry.

Read-only observability overlay.  Trading producers remain sole execution
owners.  This replaces the old count x base-risk proxy with each producer's
trade-by-trade OANDA entry/current-stop/actual-units calculation.
"""
from __future__ import annotations

import inspect
import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict

from fastapi.responses import Response

import hub_nas100_risk_overlay as existing

app = existing.app
core = existing.base.gateway.core
HUB_VERSION = "0.6.12"
POLICY_VERSION = "hub_live_broker_risk_v1_2026_10_15"
core.APP_VERSION = HUB_VERSION

_ORIGINAL_AGGREGATE = core.aggregate_snapshot
_RISK_CACHE: Dict[str, dict] = {}
_RISK_CACHE_AT = 0.0
_RISK_LOCK = threading.RLock()
_RISK_TTL_SECONDS = 10.0


def _service_urls() -> Dict[str, str]:
    return {
        "indices": os.getenv("INDICES_SERVICE_URL", "").strip().rstrip("/"),
        "metals": os.getenv("METALS_SERVICE_URL", "").strip().rstrip("/"),
        "bco": os.getenv("BCO_SERVICE_URL", "").strip().rstrip("/"),
    }


def _fetch_one(key: str, base_url: str) -> dict:
    if not base_url:
        return {"ok": False, "scope": key, "error": "service URL not configured"}
    req = urllib.request.Request(
        base_url + "/api/portfolio-risk",
        headers={"Accept": "application/json", "User-Agent": "PEP-Portfolio-Hub/0.6.12"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("risk endpoint returned non-object")
        payload = dict(payload)
        payload.setdefault("scope", key)
        return payload
    except Exception as exc:
        return {"ok": False, "scope": key, "error": f"{type(exc).__name__}: {exc}"}


def _risk_bundle() -> Dict[str, dict]:
    global _RISK_CACHE, _RISK_CACHE_AT
    now = time.monotonic()
    with _RISK_LOCK:
        if _RISK_CACHE and now - _RISK_CACHE_AT < _RISK_TTL_SECONDS:
            return {k: dict(v) for k, v in _RISK_CACHE.items()}
    urls = _service_urls()
    results: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_fetch_one, key, url): key for key, url in urls.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                results[key] = {"ok": False, "scope": key, "error": f"{type(exc).__name__}: {exc}"}
    with _RISK_LOCK:
        _RISK_CACHE = {k: dict(v) for k, v in results.items()}
        _RISK_CACHE_AT = now
    return results


def _f(value: Any):
    try:
        return None if value is None or value == "" else float(value)
    except Exception:
        return None


def aggregate_snapshot() -> Dict[str, Any]:
    snap = _ORIGINAL_AGGREGATE()
    risks = _risk_bundle()
    total_known = 0.0
    total_locked_floor = 0.0
    total_open = 0
    total_quantified = 0
    total_unquantified = 0
    endpoint_failures = []

    for key in ("indices", "metals", "bco"):
        risk = risks.get(key) or {"ok": False, "error": "no risk result"}
        source = ((snap.get("sources") or {}).get(key) or {})
        data = source.get("data") if isinstance(source, dict) else None
        if isinstance(data, dict):
            data["live_risk_ok"] = bool(risk.get("ok"))
            data["live_capital_risk_gbp"] = _f(risk.get("capital_at_risk_gbp"))
            data["live_locked_profit_floor_gbp"] = _f(risk.get("locked_profit_floor_gbp"))
            data["live_risk_open_trade_count"] = int(risk.get("open_trade_count") or 0) if risk.get("ok") else None
            data["live_risk_quantified_trade_count"] = int(risk.get("quantified_trade_count") or 0) if risk.get("ok") else None
            data["live_risk_unquantified_trade_count"] = int(risk.get("unquantified_trade_count") or 0) if risk.get("ok") else None
            data["live_risk_by_instrument"] = risk.get("by_instrument") if risk.get("ok") else None
            data["live_risk_error"] = risk.get("error")

        if not risk.get("ok"):
            endpoint_failures.append(key)
            continue
        known = _f(risk.get("capital_at_risk_gbp"))
        floor = _f(risk.get("locked_profit_floor_gbp"))
        if known is not None:
            total_known += max(0.0, known)
        if floor is not None:
            total_locked_floor += max(0.0, floor)
        total_open += int(risk.get("open_trade_count") or 0)
        total_quantified += int(risk.get("quantified_trade_count") or 0)
        total_unquantified += int(risk.get("unquantified_trade_count") or 0)

    portfolio = snap.setdefault("portfolio", {})
    endpoints_complete = not endpoint_failures
    fully_quantified = bool(endpoints_complete and total_unquantified == 0)
    portfolio["live_capital_risk_gbp"] = round(total_known, 2) if endpoints_complete else None
    portfolio["live_locked_profit_floor_gbp"] = round(total_locked_floor, 2) if endpoints_complete else None
    portfolio["live_risk_total_trade_count"] = total_open if endpoints_complete else None
    portfolio["live_risk_quantified_trade_count"] = total_quantified if endpoints_complete else None
    portfolio["live_risk_unquantified_trade_count"] = total_unquantified if endpoints_complete else None
    portfolio["live_risk_endpoint_complete"] = endpoints_complete
    portfolio["live_risk_complete"] = fully_quantified
    portfolio["live_risk_endpoint_failures"] = endpoint_failures
    portfolio["live_risk_method"] = "broker entry → current stop × actual units, converted to GBP"
    portfolio["risk_by_strategy"] = risks

    # Preserve the legacy key because the existing cockpit renders it, but its
    # value is now broker-backed.  If a producer endpoint is unavailable we
    # deliberately return null rather than understate risk with a partial sum.
    portfolio["open_risk_estimate_gbp"] = round(total_known, 2) if endpoints_complete else None
    return snap


# Replace the core callable. Existing /api/aggregate and root rendering functions
# resolve this module global at request time, so no producer contract changes are
# needed in the Hub itself.
core.aggregate_snapshot = aggregate_snapshot


def _sync_html(text: str) -> str:
    out = str(text)
    out = out.replace("v0.6.11 &middot; portfolio cockpit", "v0.6.12 &middot; portfolio cockpit")
    out = out.replace("v0.6.11 · portfolio cockpit", "v0.6.12 · portfolio cockpit")

    old = "card('Nominal risk proxy',money(p.open_risk_estimate_gbp),'Open trades × base/requested risk · not actual locked stop risk')"
    new = "card((p.live_risk_complete?'Live capital at risk':(p.live_risk_endpoint_complete?'Known capital at risk':'Live capital at risk')),money(p.live_capital_risk_gbp),(p.live_risk_endpoint_complete?((p.live_risk_unquantified_trade_count||0)>0?('⚠ '+p.live_risk_unquantified_trade_count+' trade(s) unquantified · '+p.live_risk_quantified_trade_count+'/'+p.live_risk_total_trade_count+' quantified'):((p.live_risk_total_trade_count||0)+' broker trade(s) stop-backed · locked profit floor '+money(p.live_locked_profit_floor_gbp))):('Telemetry unavailable: '+((p.live_risk_endpoint_failures||[]).join(', ')||'producer error'))))"
    out = out.replace(old, new)

    out = out.replace(
        "riskPct+' current NAV · policy target 0.050% · signals 1–5 can execute 2x units'",
        "riskPct+' current NAV · policy target 0.050% · live capital risk '+money(d.live_capital_risk_gbp)+' · signals 1–5 can execute 2x units'",
    )
    out = out.replace(
        "riskPct+' current NAV · target 0.100% · broker minimum can raise effective risk'",
        "riskPct+' current NAV · target 0.100% · live capital risk '+money(d.live_capital_risk_gbp)+' · broker minimum can raise effective risk'",
    )
    out = out.replace(
        "riskPct+' current NAV · target 0.100% · discrete units can alter effective risk'",
        "riskPct+' current NAV · target 0.100% · live capital risk '+money(d.live_capital_risk_gbp)+' · discrete units can alter effective risk'",
    )
    out = out.replace(" / nominal risk proxy '+money(p.open_risk_estimate_gbp)", " / live capital risk '+money(p.live_capital_risk_gbp)")
    return out


def _install_root_wrapper() -> dict:
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

    async def synced_root(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Response):
            raw = bytes(getattr(result, "body", b"") or b"").decode("utf-8", errors="replace")
            headers = {k: v for k, v in result.headers.items() if k.lower() not in {"content-length", "content-encoding"}}
            return Response(content=_sync_html(raw).encode("utf-8"), status_code=int(getattr(result, "status_code", 200) or 200), headers=headers, media_type=None)
        return _sync_html(str(result))

    target.endpoint = synced_root
    if getattr(target, "dependant", None) is not None:
        target.dependant.call = synced_root
    return {"ok": True, "version": POLICY_VERSION}


INSTALL = _install_root_wrapper()
print(f"HUB_LIVE_RISK installed={INSTALL.get('ok')} version={POLICY_VERSION} hub_version={HUB_VERSION}", flush=True)
