"""Project Exit Plan — Portfolio Hub analysis gateway v1.

Adds a read-only analysis aggregation surface without changing the existing
portfolio aggregation contract. BCO is the first producer; Metals and Indices
can adopt the same /analysis/status contract next.

A compact snapshot is emitted to Railway runtime logs so the connected Railway
tool can retrieve current analysis state without database credentials or a
generic public-web fetch.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import Response

import app as core

# Stable outer app: explicit wrapper routes take precedence over the unchanged core app.
app = FastAPI(title="Project Exit Plan — Wrapper")
ANALYSIS_GATEWAY_VERSION = "1.7.0"
VISIBLE_HUB_VERSION = "0.3.9"
ANALYSIS_POLL_SECONDS = max(30, min(int(float(os.getenv("ANALYSIS_POLL_SECONDS", "60"))), 900))
ANALYSIS_TIMEOUT_SECONDS = max(1.0, min(float(os.getenv("ANALYSIS_TIMEOUT_SECONDS", "8")), 20.0))

_analysis_lock = threading.RLock()
_analysis_cache: Dict[str, Dict[str, Any]] = {}
_analysis_worker_started = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "ProjectExitPlanAnalysisGateway/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=ANALYSIS_TIMEOUT_SECONDS) as resp:
        raw = resp.read()
        if int(getattr(resp, "status", 200)) != 200:
            raise RuntimeError(f"HTTP {getattr(resp, 'status', 'error')}")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("analysis payload must be an object")
    return payload


def _public_safe(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Producer contract is already public-safe. Keep an allow-list anyway so
    # future producer additions cannot accidentally forward secrets.
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    health = payload.get("operational_health") if isinstance(payload.get("operational_health"), dict) else {}
    return {
        "status": payload.get("status"),
        "project": payload.get("project"),
        "analysis_interface_version": payload.get("analysis_interface_version"),
        "app_version": payload.get("app_version"),
        "policy_version": payload.get("policy_version"),
        "environment": payload.get("environment"),
        "read_only_interface": payload.get("read_only_interface"),
        "execution_authority": payload.get("execution_authority"),
        "producer_time_utc": payload.get("time_utc"),
        "operational_health": health,
        "data": data,
    }


def _refresh_sources() -> None:
    checked = _now()
    states: Dict[str, Dict[str, Any]] = {}
    for name in ("bco", "metals", "indices"):
        base = (core.SOURCES.get(name) or {}).get("url") or ""
        try:
            if not base:
                raise RuntimeError("source URL not configured")
            payload = _public_safe(_fetch_json(base.rstrip("/") + "/analysis/status"))
            state = {"ok": True, "checked_at_utc": checked, "error": None, "data": payload}
        except Exception as exc:
            with _analysis_lock:
                previous = _analysis_cache.get(name, {})
            state = {
                "ok": False,
                "checked_at_utc": checked,
                "error": f"{type(exc).__name__}: {exc}",
                "data": previous.get("data"),
            }
        states[name] = state

    with _analysis_lock:
        _analysis_cache.update(states)

    print("PEP_ANALYSIS_SNAPSHOT " + json.dumps({
        "gateway_version": ANALYSIS_GATEWAY_VERSION,
        "generated_at_utc": checked,
        "sources": states,
    }, separators=(",", ":"), default=str), flush=True)


def _fetch_producer_endpoint(name: str, endpoint: str) -> Dict[str, Any]:
    base = (core.SOURCES.get(name) or {}).get("url") or ""
    if not base:
        raise RuntimeError("source URL not configured")
    return _fetch_json(base.rstrip("/") + endpoint)


def _refresh_discovery() -> Dict[str, Any]:
    checked = _now()
    out: Dict[str, Any] = {}
    for name in ("metals", "indices"):
        producer: Dict[str, Any] = {"checked_at_utc": checked}
        for key, endpoint in (("schema", "/analysis/schema"), ("catalog", "/analysis/catalog"), ("summary", "/analysis/summary")):
            try:
                payload = _fetch_producer_endpoint(name, endpoint)
                producer[key] = payload
            except Exception as exc:
                producer[key] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        out[name] = producer
    print("PEP_ANALYSIS_DISCOVERY " + json.dumps({
        "gateway_version": ANALYSIS_GATEWAY_VERSION,
        "generated_at_utc": checked,
        "sources": out,
    }, separators=(",", ":"), default=str), flush=True)
    return out


@app.get("/api/analysis/discovery")
def api_analysis_discovery() -> Dict[str, Any]:
    return {
        "gateway": "Project Exit Plan — Analysis Gateway",
        "gateway_version": ANALYSIS_GATEWAY_VERSION,
        "generated_at_utc": _now(),
        "read_only": True,
        "execution_authority": False,
        "sources": _refresh_discovery(),
    }


ANALYSIS_SLICES = ("trades", "signals", "execution", "harvest", "hwm", "exits", "research")

def _fetch_analysis_slice(source: str, slice_name: str, limit: int = 100) -> Dict[str, Any]:
    if source not in ("metals", "indices"):
        raise ValueError("source must be metals or indices")
    if slice_name not in ANALYSIS_SLICES:
        raise ValueError("unknown analysis slice")
    bounded = max(1, min(int(limit), 250))
    return _fetch_producer_endpoint(source, f"/analysis/slice/{slice_name}?limit={bounded}")

@app.get("/api/analysis/slice/{source}/{slice_name}")
def api_analysis_slice(source: str, slice_name: str, limit: int = 100) -> Dict[str, Any]:
    try:
        payload = _fetch_analysis_slice(source, slice_name, limit)
        status, error = "ok", None
    except Exception as exc:
        payload = {}
        status, error = "error", f"{type(exc).__name__}: {exc}"
    return {
        "status": status,
        "gateway_version": ANALYSIS_GATEWAY_VERSION,
        "generated_at_utc": _now(),
        "read_only": True,
        "execution_authority": False,
        "source": source,
        "slice": slice_name,
        "error": error,
        "payload": payload,
    }

def _emit_research_pack(limit: int = 40) -> Dict[str, Any]:
    """Emit a compact bounded pack to Railway logs for direct ChatGPT analysis."""
    pack: Dict[str, Any] = {}
    for source in ("metals", "indices"):
        pack[source] = {}
        for slice_name in ANALYSIS_SLICES:
            try:
                pack[source][slice_name] = _fetch_analysis_slice(source, slice_name, limit)
            except Exception as exc:
                pack[source][slice_name] = {"status":"error","error":f"{type(exc).__name__}: {exc}"}
    print("PEP_ANALYSIS_RESEARCH_PACK " + json.dumps({
        "gateway_version": ANALYSIS_GATEWAY_VERSION,
        "generated_at_utc": _now(),
        "limit_per_table": max(1, min(int(limit), 250)),
        "sources": pack,
    }, separators=(",", ":"), default=str), flush=True)
    return pack


def _analysis_worker() -> None:
    discovery_every = max(5, int(900 / ANALYSIS_POLL_SECONDS))
    cycle = 0
    last_contracts: Dict[str, Any] = {}
    while True:
        _refresh_sources()
        with _analysis_lock:
            contracts = {
                name: (
                    ((_analysis_cache.get(name) or {}).get("data") or {}).get("app_version"),
                    ((_analysis_cache.get(name) or {}).get("data") or {}).get("analysis_interface_version"),
                )
                for name in ("metals", "indices")
            }
        producer_changed = bool(last_contracts) and contracts != last_contracts
        # Run on startup, roughly every 15 minutes, and immediately whenever
        # either producer reports a new app/interface version. This removes the
        # post-deploy wait before ChatGPT can inspect a changed producer.
        if cycle % discovery_every == 0 or producer_changed:
            try:
                _refresh_discovery()
                _emit_research_pack(40)
            except Exception as exc:
                print("PEP_ANALYSIS_DISCOVERY_ERROR " + f"{type(exc).__name__}: {exc}", flush=True)
        last_contracts = contracts
        cycle += 1
        time.sleep(ANALYSIS_POLL_SECONDS)


@app.on_event("startup")
def start_analysis_gateway() -> None:
    global _analysis_worker_started
    if not _analysis_worker_started:
        _analysis_worker_started = True
        threading.Thread(target=_analysis_worker, daemon=True, name="analysis-gateway-poller").start()


@app.get("/api/analysis")
def api_analysis() -> Dict[str, Any]:
    with _analysis_lock:
        sources = {k: dict(v) for k, v in _analysis_cache.items()}
    return {
        "gateway": "Project Exit Plan — Analysis Gateway",
        "gateway_version": ANALYSIS_GATEWAY_VERSION,
        "generated_at_utc": _now(),
        "read_only": True,
        "execution_authority": False,
        "sources": sources,
        "planned_sources": [],
    }


def _rewrite_hub_version(body: bytes, content_type: str) -> bytes:
    if "text/html" not in (content_type or "").lower():
        return body
    try:
        text = body.decode("utf-8")
        current = getattr(core, "APP_VERSION", None)
        if current and str(current) != VISIBLE_HUB_VERSION:
            text = text.replace(str(current), VISIBLE_HUB_VERSION)
        # Core currently returns drawdown_gbp=None unconditionally, so the
        # dashboard tile is presentation-only and misleading. Hide it until a
        # real portfolio drawdown series is wired.
        text = text.replace("grid-template-columns:repeat(5,minmax(0,1fr))", "grid-template-columns:repeat(4,minmax(0,1fr))")
        text = text.replace(",card('Drawdown',money(p.drawdown_gbp),'Stage 2')", "")
        return text.encode("utf-8")
    except Exception:
        return body


async def _hub_passthrough(request: Request, path: str) -> Response:
    scope = dict(request.scope)
    scope["path"] = path
    scope["raw_path"] = path.encode("utf-8")
    messages = []
    async def receive():
        return await request.receive()
    async def send(message):
        messages.append(message)
    await core.app(scope, receive, send)
    start = next((m for m in messages if m["type"] == "http.response.start"), None)
    chunks = [m.get("body", b"") for m in messages if m["type"] == "http.response.body"]
    if not start:
        return Response(status_code=500)
    headers = dict(start.get("headers", []))
    body = _rewrite_hub_version(b"".join(chunks), headers.get(b"content-type", b"").decode("latin-1"))
    out_headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in start.get("headers", []) if k.lower() not in (b"content-length", b"content-encoding")}
    return Response(content=body, status_code=start["status"], headers=out_headers, media_type=None)


@app.get("/")
async def visible_root(request: Request):
    return await _hub_passthrough(request, "/")


@app.get("/dashboard")
async def visible_dashboard(request: Request):
    return await _hub_passthrough(request, "/dashboard")


@app.on_event("startup")
async def start_core_app() -> None:
    await core.app.router.startup()


@app.on_event("shutdown")
async def stop_core_app() -> None:
    await core.app.router.shutdown()


# Catch-all mount stays last so wrapper routes above win; all other routes remain core-owned.
app.mount("/", core.app)
