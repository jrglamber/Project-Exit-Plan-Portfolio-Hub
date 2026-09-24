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
ANALYSIS_GATEWAY_VERSION = "1.16.0"
VISIBLE_HUB_VERSION = "0.3.23"
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
    for name in ("bco", "metals", "indices"):
        producer: Dict[str, Any] = {"checked_at_utc": checked}
        endpoints = [("schema", "/analysis/schema"), ("catalog", "/analysis/catalog")]
        if name != "bco":
            endpoints.append(("summary", "/analysis/summary"))
        for key, endpoint in endpoints:
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
BCO_ANALYSIS_SLICES = ("signals", "execution", "harvest", "exits", "research")

def _fetch_analysis_slice(source: str, slice_name: str, limit: int = 100) -> Dict[str, Any]:
    if source not in ("metals", "indices"):
        raise ValueError("source must be metals or indices")
    if slice_name not in ANALYSIS_SLICES:
        raise ValueError("unknown analysis slice")
    bounded = max(1, min(int(limit), 250))
    return _fetch_producer_endpoint(source, f"/analysis/slice/{slice_name}?limit={bounded}")

@app.get("/api/analysis/episode/{source}/{slice_name}")
def api_analysis_episode(source: str, slice_name: str, limit: int = 100) -> Dict[str, Any]:
    """Fetch a larger bounded historical window on demand without widening periodic logs."""
    try:
        bounded = max(1, min(int(limit), 250))
        payload = _fetch_analysis_slice(source, slice_name, bounded)
        status, error = "ok", None
    except Exception as exc:
        payload = {}
        bounded = max(1, min(int(limit), 250))
        status, error = "error", f"{type(exc).__name__}: {exc}"
    return {
        "status": status,
        "gateway_version": ANALYSIS_GATEWAY_VERSION,
        "generated_at_utc": _now(),
        "read_only": True,
        "execution_authority": False,
        "source": source,
        "slice": slice_name,
        "historical_window": True,
        "limit_per_table": bounded,
        "error": error,
        "payload": payload,
    }

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

def _emit_research_pack(limit: int = 12) -> Dict[str, Any]:
    """Emit a compact bounded pack to Railway logs for direct ChatGPT analysis."""
    pack: Dict[str, Any] = {}
    for source in ("metals", "indices"):
        pack[source] = {}
        for slice_name in ANALYSIS_SLICES:
            try:
                pack[source][slice_name] = _fetch_analysis_slice(source, slice_name, limit)
            except Exception as exc:
                pack[source][slice_name] = {"status":"error","error":f"{type(exc).__name__}: {exc}"}
    # Raw JSON/blob columns can be enormous and duplicate the structured fields.
    # Strip them from the log transport only; producer endpoints remain unchanged.
    def compact(value):
        if isinstance(value, dict):
            return {k: compact(v) for k, v in value.items() if k not in ("raw_json", "point_in_time_json", "response_summary_json", "details_json", "payload_json", "request_json", "response_json", "snapshot_json", "metadata_json", "context_json", "decision_json")}
        if isinstance(value, list):
            return [compact(v) for v in value]
        return value
    pack = compact(pack)
    # Emit one record per source/slice so Railway retrieval never depends on
    # a single oversized all-project log line.
    generated = _now()
    for source, slices in pack.items():
        for slice_name, payload in slices.items():
            print("PEP_ANALYSIS_SLICE " + json.dumps({
                "gateway_version": ANALYSIS_GATEWAY_VERSION,
                "generated_at_utc": generated,
                "limit_per_table": max(1, min(int(limit), 250)),
                "source": source,
                "slice": slice_name,
                "payload": payload,
            }, separators=(",", ":"), default=str), flush=True)
    print("PEP_ANALYSIS_RESEARCH_PACK_READY " + json.dumps({
        "gateway_version": ANALYSIS_GATEWAY_VERSION,
        "generated_at_utc": generated,
        "sources": sorted(pack),
        "slices": list(ANALYSIS_SLICES),
    }, separators=(",", ":")), flush=True)
    return pack


def _emit_bco_history_pack(limit: int = 100) -> None:
    for slice_name in BCO_ANALYSIS_SLICES:
        try:
            payload = _fetch_producer_endpoint("bco", f"/analysis/slice/{slice_name}?limit={max(1, min(int(limit), 250))}")
            print("PEP_BCO_HISTORY_SLICE " + json.dumps({"gateway_version": ANALYSIS_GATEWAY_VERSION, "read_only": True, "execution_authority": False, "slice": slice_name, "payload": payload}, separators=(",", ":"), default=str), flush=True)
        except Exception as exc:
            print("PEP_BCO_HISTORY_SLICE_ERROR " + slice_name + " " + type(exc).__name__ + ": " + str(exc), flush=True)


def _emit_producer_episode_index() -> None:
    """Bridge producer-native cycle indexes for all trading systems."""
    for source in ("metals", "indices", "bco"):
        try:
            payload = _fetch_producer_endpoint(source, "/analysis/episode-index?limit=100")
            print("PEP_PRODUCER_EPISODE_INDEX " + json.dumps({"gateway_version": ANALYSIS_GATEWAY_VERSION, "read_only": True, "execution_authority": False, "source": source, "payload": payload}, separators=(",", ":"), default=str), flush=True)
        except Exception as exc:
            print("PEP_PRODUCER_EPISODE_INDEX_ERROR " + source + " " + type(exc).__name__ + ": " + str(exc), flush=True)

def _emit_adaptive_protection_study() -> None:
    """Bridge the Indices producer-native adaptive protection trajectory study."""
    try:
        payload = _fetch_producer_endpoint("indices", "/analysis/adaptive-protection-study")
        print("PEP_ADAPTIVE_PROTECTION_STUDY " + json.dumps({
            "gateway_version": ANALYSIS_GATEWAY_VERSION,
            "read_only": True,
            "execution_authority": False,
            "source": "indices",
            "payload": payload,
        }, separators=(",", ":"), default=str), flush=True)
    except Exception as exc:
        print("PEP_ADAPTIVE_PROTECTION_STUDY_ERROR indices " + type(exc).__name__ + ": " + str(exc), flush=True)


def _emit_post_hwm_deterioration() -> None:
    """Bridge Indices post-HWM deterioration metrics for historical controls."""
    try:
        payload = _fetch_producer_endpoint("indices", "/analysis/post-hwm-deterioration")
        print("PEP_POST_HWM_DETERIORATION " + json.dumps({
            "gateway_version": ANALYSIS_GATEWAY_VERSION,
            "read_only": True,
            "execution_authority": False,
            "source": "indices",
            "payload": payload,
        }, separators=(",", ":"), default=str), flush=True)
    except Exception as exc:
        print("PEP_POST_HWM_DETERIORATION_ERROR indices " + type(exc).__name__ + ": " + str(exc), flush=True)



def _emit_protection_state_shadow() -> None:
    """Bridge research-only Indices protection-state classifications."""
    try:
        payload = _fetch_producer_endpoint("indices", "/analysis/protection-state-shadow")
        print("PEP_PROTECTION_STATE_SHADOW " + json.dumps({
            "gateway_version": ANALYSIS_GATEWAY_VERSION,
            "read_only": True,
            "execution_authority": False,
            "source": "indices",
            "payload": payload,
        }, separators=(",", ":"), default=str), flush=True)
    except Exception as exc:
        print("PEP_PROTECTION_STATE_SHADOW_ERROR indices " + type(exc).__name__ + ": " + str(exc), flush=True)



def _emit_protection_state_timeline() -> None:
    """Bridge point-in-time Indices protection-state timeline."""
    try:
        payload = _fetch_producer_endpoint("indices", "/analysis/protection-state-timeline")
        print("PEP_PROTECTION_STATE_TIMELINE " + json.dumps({
            "gateway_version": ANALYSIS_GATEWAY_VERSION,
            "read_only": True,
            "execution_authority": False,
            "source": "indices",
            "payload": payload,
        }, separators=(",", ":"), default=str), flush=True)
    except Exception as exc:
        print("PEP_PROTECTION_STATE_TIMELINE_ERROR indices " + type(exc).__name__ + ": " + str(exc), flush=True)


def _emit_compact_episode_index() -> None:
    """Emit compact historical landmarks without widening the raw-row transport.

    The producer APIs remain SELECT-only.  This index intentionally derives
    landmarks only from rows already exposed by the bounded HWM/harvest slices;
    it never gains execution authority or mutates producer state.
    """
    generated = _now()
    for source in ("metals", "indices"):
        try:
            hwm = _fetch_analysis_slice(source, "hwm", 250)
            harvest = _fetch_analysis_slice(source, "harvest", 250)
            hdata = ((hwm.get("data") or {}) if isinstance(hwm, dict) else {})
            vdata = ((harvest.get("data") or {}) if isinstance(harvest, dict) else {})
            landmarks = []
            for table, rows in hdata.items():
                if not isinstance(rows, list):
                    continue
                # Intrahour NEW_HIGH tables can contain hundreds of near-identical
                # rows. Preserve only economically useful maxima plus cycle rows.
                if table in ("active_basket_cycles", "active_family_basket_cycles"):
                    for row in rows:
                        if isinstance(row, dict):
                            landmarks.append({"table": table, **row})
                    continue
                numeric = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    vals = [row.get(k) for k in ("high_water_gbp", "high_water_pnl", "high_water_r")]
                    score = max([abs(float(v)) for v in vals if isinstance(v, (int, float))] or [0.0])
                    numeric.append((score, row))
                for _, row in sorted(numeric, key=lambda x: x[0], reverse=True)[:5]:
                    landmarks.append({"table": table, **row})
            harvest_events = []
            for table, rows in vdata.items():
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict):
                            harvest_events.append({"table": table, **row})
            # Keep executed/meaningful events and deduplicate repeated historical
            # NO_ELIGIBLE polling noise by cycle+threshold+status.
            seen = set()
            compact_harvest = []
            for row in harvest_events:
                key = (row.get("family_cycle_id") or row.get("basket_cycle_id"), row.get("threshold_r"), row.get("status"))
                if key in seen:
                    continue
                seen.add(key)
                compact_harvest.append(row)
            print("PEP_ANALYSIS_EPISODE_INDEX " + json.dumps({
                "gateway_version": ANALYSIS_GATEWAY_VERSION,
                "generated_at_utc": generated,
                "read_only": True,
                "execution_authority": False,
                "source": source,
                "landmarks": landmarks[:40],
                "harvest_landmarks": compact_harvest[:40],
                "note": "bounded index; use targeted drill-down for older cycles beyond producer slice window",
            }, separators=(",", ":"), default=str), flush=True)
        except Exception as exc:
            print("PEP_ANALYSIS_EPISODE_INDEX_ERROR " + json.dumps({
                "gateway_version": ANALYSIS_GATEWAY_VERSION,
                "generated_at_utc": generated,
                "source": source,
                "error": f"{type(exc).__name__}: {exc}",
            }, separators=(",", ":")), flush=True)


def _emit_historical_episode_pack(limit: int = 250) -> None:
    """Emit bounded historical research windows for offline episode studies.

    This is transport-only: producer endpoints remain SELECT-only and this
    gateway has no execution authority.  Emission is intentionally infrequent
    so normal periodic logs stay compact.
    """
    bounded = max(1, min(int(limit), 250))
    generated = _now()
    for source in ("metals", "indices"):
        for slice_name in ("hwm", "harvest", "research"):
            try:
                payload = _fetch_analysis_slice(source, slice_name, bounded)
                # Reuse the same transport compaction policy as normal packs.
                def compact(value):
                    if isinstance(value, dict):
                        return {k: compact(v) for k, v in value.items() if k not in ("raw_json", "point_in_time_json", "response_summary_json", "details_json", "payload_json", "request_json", "response_json", "snapshot_json", "metadata_json", "context_json", "decision_json")}
                    if isinstance(value, list):
                        return [compact(v) for v in value]
                    return value
                payload = compact(payload)
                print("PEP_ANALYSIS_EPISODE " + json.dumps({
                    "gateway_version": ANALYSIS_GATEWAY_VERSION,
                    "generated_at_utc": generated,
                    "read_only": True,
                    "execution_authority": False,
                    "source": source,
                    "slice": slice_name,
                    "historical_window": True,
                    "limit_per_table": bounded,
                    "payload": payload,
                }, separators=(",", ":"), default=str), flush=True)
            except Exception as exc:
                print("PEP_ANALYSIS_EPISODE_ERROR " + json.dumps({
                    "gateway_version": ANALYSIS_GATEWAY_VERSION,
                    "generated_at_utc": generated,
                    "source": source,
                    "slice": slice_name,
                    "error": f"{type(exc).__name__}: {exc}",
                }, separators=(",", ":")), flush=True)


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
                _emit_research_pack(12)
                # Historical episode windows are emitted only with discovery
                # (startup / ~15 min / producer version change), not every poll.
                _emit_historical_episode_pack(250)
                _emit_compact_episode_index()
                _emit_producer_episode_index()
                _emit_adaptive_protection_study()
                _emit_post_hwm_deterioration()
                _emit_protection_state_shadow()
                _emit_protection_state_timeline()
                _emit_bco_history_pack(100)
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
