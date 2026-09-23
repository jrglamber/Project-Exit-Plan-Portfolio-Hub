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

import app as core

app = core.app
ANALYSIS_GATEWAY_VERSION = "1.0.0"
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


def _refresh_bco() -> None:
    base = (core.SOURCES.get("bco") or {}).get("url") or ""
    checked = _now()
    try:
        payload = _public_safe(_fetch_json(base.rstrip("/") + "/analysis/status"))
        state = {"ok": True, "checked_at_utc": checked, "error": None, "data": payload}
    except Exception as exc:
        with _analysis_lock:
            previous = _analysis_cache.get("bco", {})
        state = {
            "ok": False,
            "checked_at_utc": checked,
            "error": f"{type(exc).__name__}: {exc}",
            "data": previous.get("data"),
        }
    with _analysis_lock:
        _analysis_cache["bco"] = state

    # Deliberately compact, machine-readable and secret-free. This makes the
    # Railway connector a safe read path for ChatGPT while richer APIs evolve.
    print("PEP_ANALYSIS_SNAPSHOT " + json.dumps({
        "gateway_version": ANALYSIS_GATEWAY_VERSION,
        "generated_at_utc": checked,
        "sources": {"bco": state},
    }, separators=(",", ":"), default=str), flush=True)


def _analysis_worker() -> None:
    while True:
        _refresh_bco()
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
        "planned_sources": ["metals", "indices"],
    }
