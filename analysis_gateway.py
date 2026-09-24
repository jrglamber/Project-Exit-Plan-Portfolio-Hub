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
ANALYSIS_GATEWAY_VERSION = "1.2.0"
VISIBLE_HUB_VERSION = "0.3.2"
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


def _analysis_worker() -> None:
    while True:
        _refresh_sources()
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
