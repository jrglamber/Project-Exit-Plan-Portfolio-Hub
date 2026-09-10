from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

APP_NAME = "Project Exit Plan — Portfolio Hub"
APP_VERSION = "0.3.1"
SCHEMA_VERSION = 1

POLL_SECONDS = max(5, min(int(float(os.getenv("AGGREGATE_POLL_SECONDS", "20"))), 300))
SOURCE_TIMEOUT_SECONDS = max(1.0, min(float(os.getenv("AGGREGATE_SOURCE_TIMEOUT_SECONDS", "5")), 20.0))
STALE_AFTER_SECONDS = max(30, min(int(float(os.getenv("AGGREGATE_STALE_AFTER_SECONDS", "120"))), 3600))
SUMMARY_PATH = os.getenv("AGGREGATE_SUMMARY_PATH", "/api/portfolio-summary").strip() or "/api/portfolio-summary"
SOURCE_SECRET = os.getenv("AGGREGATE_SOURCE_SECRET", "").strip()

SOURCES = {
    "indices": {"label": "Indices", "url": os.getenv("INDICES_SERVICE_URL", "").strip().rstrip("/")},
    "metals": {"label": "Metals", "url": os.getenv("METALS_SERVICE_URL", "").strip().rstrip("/")},
    "bco": {"label": "BCO", "url": os.getenv("BCO_SERVICE_URL", "").strip().rstrip("/")},
}

# v0.3.0: producer app versions are informational only.
# Compatibility is enforced by the shared portfolio-summary schema contract,
# not by exact Indices/Metals/BCO build numbers. Producer apps can therefore
# be upgraded independently without requiring a Portfolio Hub redeploy.
REQUIRED_SOURCE_SCHEMA_VERSION = SCHEMA_VERSION

LIVE_PORTFOLIO_STRATEGIES = tuple(
    x.strip().lower()
    for x in os.getenv("AGGREGATE_LIVE_STRATEGIES", "indices,metals").split(",")
    if x.strip().lower() in SOURCES
)

# Indices and Metals read the same broker account independently, so their NAV
# snapshots can differ slightly simply because they were sampled seconds apart.
# Only flag a genuine mismatch when the spread is materially larger than normal
# live-market drift.
NAV_MISMATCH_ABS_GBP = max(
    0.01, float(os.getenv("PORTFOLIO_NAV_MISMATCH_ABS_GBP", "10"))
)
NAV_MISMATCH_PCT = max(
    0.0, float(os.getenv("PORTFOLIO_NAV_MISMATCH_PCT", "0.0025"))
)

# Signal-feed freshness is intentionally separate from service/API freshness.
# Hourly TradingView alerts can stop while the producer apps remain perfectly
# healthy, so the Hub tracks the most recent signal timestamp independently.
SIGNAL_WARN_AFTER_SECONDS = max(
    900, int(float(os.getenv("PORTFOLIO_SIGNAL_WARN_AFTER_SECONDS", "5400")))
)
SIGNAL_STALE_AFTER_SECONDS = max(
    SIGNAL_WARN_AFTER_SECONDS + 300,
    int(float(os.getenv("PORTFOLIO_SIGNAL_STALE_AFTER_SECONDS", "9000"))),
)


def signal_expected_now() -> bool:
    """Conservative 24/5 guard to avoid weekend false alarms.

    Producer feeds are hourly CFDs. We deliberately keep this broad: Monday to
    Friday is considered active. The 150-minute stale threshold tolerates a
    normal one-hour market/data pause without hiding a genuinely expired alert.
    """
    return datetime.now(timezone.utc).weekday() < 5


def signal_health_state(last_signal_at_utc: Any) -> Dict[str, Any]:
    age = iso_age_seconds(last_signal_at_utc)
    expected = signal_expected_now()
    if not expected:
        status = "MARKET_CLOSED"
    elif age is None:
        status = "UNKNOWN"
    elif age > SIGNAL_STALE_AFTER_SECONDS:
        status = "STALE"
    elif age > SIGNAL_WARN_AFTER_SECONDS:
        status = "LATE"
    else:
        status = "OK"
    return {
        "status": status,
        "expected_now": expected,
        "last_signal_at_utc": last_signal_at_utc or None,
        "age_seconds": age,
        "warn_after_seconds": SIGNAL_WARN_AFTER_SECONDS,
        "stale_after_seconds": SIGNAL_STALE_AFTER_SECONDS,
    }

app = FastAPI(title=APP_NAME, version=APP_VERSION)

_lock = threading.RLock()
_cache: Dict[str, Dict[str, Any]] = {}
_worker_started = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except Exception:
        return None


def iso_age_seconds(ts: Any) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def validate_summary(key: str, payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("summary payload must be an object")
    schema = safe_int(payload.get("schema_version"))
    if schema != REQUIRED_SOURCE_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version must be {REQUIRED_SOURCE_SCHEMA_VERSION}; received {schema}"
        )
    strategy = str(payload.get("strategy") or "").strip().lower()
    if strategy != key:
        raise ValueError(f"strategy must be '{key}'")

    basket = payload.get("basket")
    if not isinstance(basket, dict):
        raise ValueError("basket must be an object")
    accounting = payload.get("accounting") or {}
    health = payload.get("health") or {}
    exit_management = payload.get("exit_management") or {}
    if (
        not isinstance(accounting, dict)
        or not isinstance(health, dict)
        or not isinstance(exit_management, dict)
    ):
        raise ValueError("accounting, health and exit_management must be objects")

    updated_at = payload.get("updated_at_utc") or payload.get("updated_at")
    if not updated_at:
        raise ValueError("updated_at_utc is required")

    return {
        "schema_version": SCHEMA_VERSION,
        "strategy": strategy,
        "label": str(payload.get("label") or SOURCES[key]["label"]),
        "mode": str(payload.get("mode") or "unknown").lower(),
        "status": str(payload.get("status") or "unknown").upper(),
        "updated_at_utc": str(updated_at),
        "nav_gbp": safe_float(payload.get("nav_gbp")),
        "risk_per_trade_gbp": safe_float(payload.get("risk_per_trade_gbp")),
        "basket": {
            "direction": str(basket.get("direction") or "").upper(),
            "open_trades": safe_int(basket.get("open_trades")),
            "last_trade_opened_at_utc": basket.get("last_trade_opened_at_utc"),
            "pnl_gbp": safe_float(basket.get("pnl_gbp")),
            "pnl_r": safe_float(basket.get("pnl_r")),
            "high_water_gbp": safe_float(basket.get("high_water_gbp")),
            "high_water_r": safe_float(basket.get("high_water_r")),
            "high_water_at_utc": basket.get("high_water_at_utc"),
            "high_water_gbp_source": str(basket.get("high_water_gbp_source") or ""),
            "high_water_gbp_snapshot_at_utc": basket.get("high_water_gbp_snapshot_at_utc"),
            "giveback_gbp": safe_float(basket.get("giveback_gbp")),
            "giveback_r": safe_float(basket.get("giveback_r")),
        },
        "exit_management": {
            "current_manager": str(exit_management.get("current_manager") or ""),
            "next_cycle_manager": str(exit_management.get("next_cycle_manager") or ""),
            "cycle_id": exit_management.get("cycle_id"),
            "live_cutover_ready": bool(exit_management.get("live_cutover_ready")),
            "live_cutover_status": str(exit_management.get("live_cutover_status") or ""),
            "pending_broker_actions": safe_int(exit_management.get("pending_broker_actions")),
        },
        "accounting": {
            "realised_today_gbp": safe_float(accounting.get("realised_today_gbp")),
            "realised_week_gbp": safe_float(accounting.get("realised_week_gbp")),
            "realised_month_gbp": safe_float(accounting.get("realised_month_gbp")),
            "realised_all_time_gbp": safe_float(accounting.get("realised_all_time_gbp")),
        },
        "health": {
            "broker_ok": health.get("broker_ok"),
            "database_ok": health.get("database_ok"),
            "worker_ok": health.get("worker_ok"),
            "last_signal_at_utc": health.get("last_signal_at_utc"),
            "note": str(health.get("note") or ""),
        },
        "source_build": str(payload.get("source_build") or payload.get("version") or ""),
    }


def fetch_source(key: str) -> Dict[str, Any]:
    base = SOURCES[key]["url"]
    if not base:
        raise ValueError(f"{key} service URL is not configured")
    headers = {"Accept": "application/json", "User-Agent": f"ProjectExitPlanAggregate/{APP_VERSION}"}
    if SOURCE_SECRET:
        headers["X-Aggregate-Secret"] = SOURCE_SECRET
    req = urllib.request.Request(base + SUMMARY_PATH, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=SOURCE_TIMEOUT_SECONDS) as resp:
        raw = resp.read()
        if int(getattr(resp, "status", 200)) != 200:
            raise RuntimeError(f"HTTP {getattr(resp, 'status', 'error')}")
    return validate_summary(key, json.loads(raw.decode("utf-8")))


def refresh_one(key: str) -> None:
    checked = now_iso()
    try:
        data = fetch_source(key)
        with _lock:
            _cache[key] = {
                "ok": True,
                "checked_at_utc": checked,
                "error": None,
                "data": data,
                "last_good_at_utc": checked,
            }
    except Exception as exc:
        with _lock:
            previous = _cache.get(key, {})
            _cache[key] = {
                "ok": False,
                "checked_at_utc": checked,
                "error": f"{type(exc).__name__}: {exc}",
                "data": previous.get("data"),
                "last_good_at_utc": previous.get("last_good_at_utc"),
            }


def refresh_all() -> None:
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(refresh_one, k) for k in SOURCES]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass


def _worker() -> None:
    while True:
        refresh_all()
        time.sleep(POLL_SECONDS)


@app.on_event("startup")
def startup() -> None:
    global _worker_started
    if not _worker_started:
        _worker_started = True
        threading.Thread(target=_worker, daemon=True, name="aggregate-poller").start()


def source_view(key: str) -> Dict[str, Any]:
    with _lock:
        state = dict(_cache.get(key, {}))
    data = state.get("data")
    age = iso_age_seconds(data.get("updated_at_utc")) if isinstance(data, dict) else None
    state["data_age_seconds"] = age
    state["stale"] = bool(age is None or age > STALE_AFTER_SECONDS)
    state["configured"] = bool(SOURCES[key]["url"])
    # A successful fetch/validation proves the producer speaks the required
    # schema. Build/version remains informational for display and audit only.
    reported_build = str(data.get("source_build") or "") if isinstance(data, dict) else ""
    state["reported_build"] = reported_build
    state["required_schema_version"] = REQUIRED_SOURCE_SCHEMA_VERSION
    state["reported_schema_version"] = (
        safe_int(data.get("schema_version")) if isinstance(data, dict) else None
    )
    state["contract_match"] = bool(
        isinstance(data, dict)
        and state["reported_schema_version"] == REQUIRED_SOURCE_SCHEMA_VERSION
    )
    health = data.get("health") if isinstance(data, dict) else {}
    last_signal_at_utc = (health or {}).get("last_signal_at_utc") if isinstance(health, dict) else None
    state["signal"] = signal_health_state(last_signal_at_utc)
    return state


def aggregate_snapshot() -> Dict[str, Any]:
    views = {k: source_view(k) for k in SOURCES}

    # Only explicitly live-money strategies feed the combined portfolio strip.
    # BCO remains fully visible in its own top tiles while PRACTICE/DEMO, but it
    # cannot contaminate live NAV/P&L/risk. Promotion is a deliberate env change.
    live_rows = []
    portfolio_warnings = []
    for key in LIVE_PORTFOLIO_STRATEGIES:
        view = views.get(key) or {}
        data = view.get("data")
        if not isinstance(data, dict):
            portfolio_warnings.append(f"{key}: unavailable")
            continue
        if str(data.get("mode") or "").lower() != "live":
            portfolio_warnings.append(f"{key}: mode={data.get('mode') or 'unknown'}")
            continue
        if view.get("stale"):
            portfolio_warnings.append(f"{key}: stale")
        live_rows.append((key, data, view))

    live_values = [row[1] for row in live_rows]

    # NAV is a shared broker-account value, not a strategy value. Use the most
    # recently sampled live source rather than whichever strategy happens to be
    # first in the configured list.
    nav_candidates = []
    for key, data, view in live_rows:
        nav_value = data.get("nav_gbp")
        if nav_value is None:
            continue
        updated_raw = data.get("updated_at_utc") or ""
        try:
            updated_dt = datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00"))
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            updated_ts = updated_dt.timestamp()
        except Exception:
            updated_ts = 0.0
        nav_candidates.append((updated_ts, key, float(nav_value)))

    nav_candidates.sort(reverse=True)
    nav = nav_candidates[0][2] if nav_candidates else None
    nav_source = nav_candidates[0][1] if nav_candidates else None
    nav_values = [row[2] for row in nav_candidates]

    nav_spread_gbp = (
        max(nav_values) - min(nav_values) if len(nav_values) > 1 else 0.0
    )
    nav_reference = (
        sum(nav_values) / len(nav_values) if nav_values else 0.0
    )
    nav_tolerance_gbp = max(
        NAV_MISMATCH_ABS_GBP,
        abs(nav_reference) * NAV_MISMATCH_PCT,
    )
    nav_disagreement = bool(
        len(nav_values) > 1 and nav_spread_gbp > nav_tolerance_gbp
    )

    pnl_values = [
        d["basket"].get("pnl_gbp")
        for d in live_values
        if d.get("basket", {}).get("pnl_gbp") is not None
    ]
    total_unrealised = sum(pnl_values) if pnl_values else None

    mtd_values = [
        d["accounting"].get("realised_month_gbp")
        for d in live_values
        if d.get("accounting", {}).get("realised_month_gbp") is not None
    ]
    mtd = sum(mtd_values) if mtd_values else None

    open_risk_values = []
    for d in live_values:
        count = d.get("basket", {}).get("open_trades")
        risk = d.get("risk_per_trade_gbp")
        if count is not None and risk is not None:
            open_risk_values.append(max(0, count) * max(0.0, risk))
    open_risk = sum(open_risk_values) if open_risk_values else None

    if nav_disagreement:
        portfolio_warnings.append("live NAV mismatch")

    signal_alerts = []
    for key, view in views.items():
        sig = view.get("signal") or {}
        status = str(sig.get("status") or "UNKNOWN").upper()
        if status in ("STALE", "UNKNOWN") and sig.get("expected_now"):
            signal_alerts.append({
                "strategy": key,
                "label": SOURCES[key]["label"],
                "status": status,
                "last_signal_at_utc": sig.get("last_signal_at_utc"),
                "age_seconds": sig.get("age_seconds"),
                "mode": ((view.get("data") or {}).get("mode") if isinstance(view.get("data"), dict) else None),
            })

    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "generated_at_utc": now_iso(),
        "sources": views,
        "signal_feed": {
            "status": "ALERT" if signal_alerts else "OK",
            "alerts": signal_alerts,
            "expected_now": signal_expected_now(),
            "warn_after_seconds": SIGNAL_WARN_AFTER_SECONDS,
            "stale_after_seconds": SIGNAL_STALE_AFTER_SECONDS,
        },
        "portfolio": {
            "scope": list(LIVE_PORTFOLIO_STRATEGIES),
            "complete": len(portfolio_warnings) == 0,
            "warnings": portfolio_warnings,
            "nav_gbp": nav,
            "nav_source": nav_source,
            "nav_spread_gbp": nav_spread_gbp,
            "nav_tolerance_gbp": nav_tolerance_gbp,
            "nav_disagreement": nav_disagreement,
            "unrealised_pnl_gbp": total_unrealised,
            "realised_month_gbp": mtd,
            "open_risk_estimate_gbp": open_risk,
            "drawdown_gbp": None,
        },
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    snap = aggregate_snapshot()
    return {
        "status": "ok",
        "version": APP_VERSION,
        "configured_sources": {k: bool(v["url"]) for k, v in SOURCES.items()},
        "required_source_schema_version": REQUIRED_SOURCE_SCHEMA_VERSION,
        "live_portfolio_strategies": LIVE_PORTFOLIO_STRATEGIES,
        "source_state": {
            k: {
                "ok": v.get("ok"),
                "stale": v.get("stale"),
                "checked_at_utc": v.get("checked_at_utc"),
                "last_good_at_utc": v.get("last_good_at_utc"),
                "reported_build": v.get("reported_build"),
                "required_schema_version": v.get("required_schema_version"),
                "reported_schema_version": v.get("reported_schema_version"),
                "contract_match": v.get("contract_match"),
                "signal": v.get("signal"),
                "error": v.get("error"),
            }
            for k, v in snap["sources"].items()
        },
        "time_utc": now_iso(),
    }


@app.get("/api/aggregate")
def api_aggregate() -> Dict[str, Any]:
    return aggregate_snapshot()


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    return '''<!doctype html>
<html>
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project Exit Plan — Portfolio Hub</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--panel2:#11161d;--border:#30363d;--text:#f3f4f6;--muted:#9da7b3;--green:#54d98c;--amber:#f7c65d;--red:#ff7b72}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Arial,Helvetica,sans-serif;padding:14px}.page{max-width:1220px;margin:0 auto}
h1{margin:2px 0 4px;font-size:30px}.sub{color:var(--muted);font-size:12px;margin-bottom:12px}.top-status{font-size:11px;color:var(--muted);margin:7px 0 12px}
.strategy{background:var(--panel);border:1px solid var(--border);border-radius:12px;margin:8px 0;overflow:hidden}.strategy-head{display:flex;justify-content:space-between;gap:8px;align-items:center;padding:9px 11px;border-bottom:1px solid var(--border)}.strategy-title{font-weight:800}.badges{display:flex;gap:6px;align-items:center}.badge{font-size:10px;font-weight:800;border:1px solid var(--border);border-radius:999px;padding:3px 6px}.green{color:var(--green)}.amber{color:var(--amber)}.red{color:var(--red)}.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;padding:8px}.card{background:var(--panel2);border:1px solid var(--border);border-radius:9px;padding:9px;min-height:78px}.label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.04em}.value{font-size:22px;font-weight:800;margin-top:5px}.small{font-size:10px;color:var(--muted);margin-top:4px;line-height:1.35}
.portfolio{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:7px;margin:13px 0}.portfolio .card{min-height:70px}
.signal-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin:7px 0 12px}.signal-card{background:var(--panel2);border:1px solid var(--border);border-radius:9px;padding:9px;min-height:72px}.signal-card.ok{border-color:#285f42}.signal-card.late{border-color:#7d652a}.signal-card.stale,.signal-card.unknown{border-color:#7a3434}.signal-alert{border:1px solid #7a3434;background:#2a1518;color:var(--red);border-radius:9px;padding:9px 11px;margin:6px 0 8px;font-size:12px;font-weight:800}.signal-ok{border:1px solid #285f42;background:#102219;color:var(--green);border-radius:9px;padding:7px 10px;margin:6px 0 8px;font-size:11px;font-weight:800}
details{background:var(--panel);border:1px solid var(--border);border-radius:10px;margin:8px 0}summary{cursor:pointer;padding:11px;font-weight:800}.body{border-top:1px solid var(--border);padding:11px;color:var(--muted);font-size:12px;line-height:1.5}.section-title{font-size:17px;margin:16px 0 7px}
@media(max-width:800px){body{padding:8px}h1{font-size:25px}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.portfolio{grid-template-columns:repeat(2,minmax(0,1fr))}.signal-grid{grid-template-columns:1fr}.value{font-size:19px}.strategy-head{align-items:flex-start}}
</style>
</head>
<body><div class="page">
<h1>Project Exit Plan — Portfolio Hub</h1>
<div class="sub">v0.3.1 · Live portfolio overview · read-only · producer build numbers are informational; schema contract controls compatibility · practice/demo strategies remain visible but excluded from live-money totals</div>
<div class="section-title">Live Portfolio</div><div id="portfolioNote" class="top-status">Waiting for live portfolio scope…</div>
<div id="portfolio" class="portfolio"></div>
<div class="section-title">Signal Feed Health</div>
<div id="signalBanner">Checking hourly TradingView feeds…</div>
<div id="signalGrid" class="signal-grid"></div>
<div id="topStatus" class="top-status">Loading live strategy snapshots…</div>
<div id="strategies"></div>
<details><summary>Accounting</summary><div class="body" id="accounting">Waiting for connected strategy accounting data.</div></details>
<details><summary>Exposure</summary><div class="body" id="exposure">Waiting for connected strategy exposure data.</div></details>
<details><summary>Recent Activity</summary><div class="body">Stage 2. This will aggregate closures, harvests, basket resets and material events after the top-tile contract is proven.</div></details>
<details><summary>Monthly Risk Review</summary><div class="body">Stage 3. Manual review/approval only; approved changes apply to subsequent trades and never automatically resize existing positions.</div></details>
<details><summary>System Health</summary><div class="body" id="health">Loading source health…</div></details>
</div>
<script>
const ORDER=['indices','metals','bco']; const NAMES={indices:'Indices',metals:'Metals',bco:'BCO'};
function num(v){const n=Number(v);return Number.isFinite(n)?n:null}
function money(v){const n=num(v);if(n===null)return '—';return (n<0?'-':'')+'£'+Math.abs(n).toLocaleString('en-GB',{minimumFractionDigits:2,maximumFractionDigits:2})}
function rr(v){const n=num(v);if(n===null)return '—';return (n>0?'+':'')+n.toFixed(1)+'R'}
function intval(v){const n=num(v);return n===null?'—':String(Math.round(n))}
function cls(v){const n=num(v);return n===null?'':(n>0?'green':n<0?'red':'')}
function when(v){if(!v)return '—';try{return new Date(v).toLocaleString('en-GB',{timeZone:'Europe/London',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch{return String(v)}}
function stateBadge(s){if(!s.configured)return '<span class="badge red">NOT CONFIGURED</span>';if(!s.data)return '<span class="badge red">UNAVAILABLE</span>';if(s.contract_match===false)return '<span class="badge red">SCHEMA MISMATCH</span>';if(s.stale)return '<span class="badge amber">STALE</span>';if(s.ok)return '<span class="badge green">FRESH DATA</span>';return '<span class="badge amber">LAST GOOD</span>'}
function strategyHtml(key,s){const d=s.data||{},b=d.basket||{},em=d.exit_management||{},mode=(d.mode||'unknown').toUpperCase(),status=(d.status||'UNKNOWN').toUpperCase(),dir=(b.direction||'').toUpperCase();const hw=[rr(b.high_water_r),when(b.high_water_at_utc)].filter(x=>x&&x!=='—').join(' · ');const managerBadges=key==='bco'?`${em.current_manager&&em.current_manager!=='FLAT'?`<span class="badge">EXIT ${em.current_manager}</span>`:''}${em.next_cycle_manager&&em.next_cycle_manager!==em.current_manager?`<span class="badge green">NEXT ${em.next_cycle_manager}</span>`:''}${em.live_cutover_ready?'<span class="badge green">READY FOR LIVE CUTOVER</span>':''}`:'';const lastOpened=b.last_trade_opened_at_utc?when(b.last_trade_opened_at_utc):'—';return `<div class="strategy"><div class="strategy-head"><div class="strategy-title">${NAMES[key]}</div><div class="badges"><span class="badge">${mode}</span><span class="badge">${d.source_build||'BUILD ?'}</span><span class="badge">${status}${dir?' · '+dir:''}</span>${managerBadges}${stateBadge(s)}</div></div><div class="cards"><div class="card"><div class="label">Broker P&amp;L</div><div class="value ${cls(b.pnl_gbp)}">${money(b.pnl_gbp)}</div><div class="small">${rr(b.pnl_r)}</div></div><div class="card"><div class="label">High Water</div><div class="value">${money(b.high_water_gbp)}</div><div class="small">${hw||'—'}</div></div><div class="card"><div class="label">Giveback</div><div class="value">${money(b.giveback_gbp)}</div><div class="small">${rr(b.giveback_r)}</div></div><div class="card"><div class="label">Open Trades</div><div class="value">${intval(b.open_trades)}</div><div class="small">Last opened ${lastOpened}</div><div class="small">${key==='bco'&&em.live_cutover_status?em.live_cutover_status.replaceAll('_',' ')+' · ':''}Updated ${when(d.updated_at_utc)}</div></div></div></div>`}
function card(label,val,sub=''){return `<div class="card"><div class="label">${label}</div><div class="value">${val}</div><div class="small">${sub}</div></div>`}
function ageText(seconds){const n=num(seconds);if(n===null)return 'age unknown';const mins=Math.floor(n/60);if(mins<60)return `${mins}m ago`;const h=Math.floor(mins/60),m=mins%60;return `${h}h ${m}m ago`}
function signalCard(key,s){const sig=s.signal||{},status=(sig.status||'UNKNOWN').toUpperCase(),last=sig.last_signal_at_utc;let css=status.toLowerCase(),tone=status==='OK'?'green':status==='LATE'?'amber':status==='MARKET_CLOSED'?'muted':'red';let headline=status==='OK'?'OK — signals arriving':status==='LATE'?'LATE — check feed':status==='STALE'?'STALE — ACTION REQUIRED':status==='MARKET_CLOSED'?'Market closed':'NO SIGNAL TIMESTAMP';return `<div class="signal-card ${css}"><div class="label">${NAMES[key]}</div><div class="value ${tone}" style="font-size:17px">${headline}</div><div class="small">Last signal ${when(last)} · ${ageText(sig.age_seconds)}</div></div>`}
async function load(){try{const res=await fetch('/api/aggregate',{cache:'no-store'});const x=await res.json();document.getElementById('strategies').innerHTML=ORDER.map(k=>strategyHtml(k,x.sources[k]||{})).join('');document.getElementById('signalGrid').innerHTML=ORDER.map(k=>signalCard(k,x.sources[k]||{})).join('');const sf=x.signal_feed||{},alerts=sf.alerts||[];document.getElementById('signalBanner').innerHTML=alerts.length?`<div class="signal-alert">⚠ SIGNAL FEED ALERT — ${alerts.map(a=>`${NAMES[a.strategy]||a.label||a.strategy}: ${a.status}${a.last_signal_at_utc?' · last '+when(a.last_signal_at_utc):''}${num(a.age_seconds)!==null?' · '+ageText(a.age_seconds):''}`).join(' | ')}</div>`:`<div class="signal-ok">✓ Signal feeds healthy — latest hourly alerts are arriving within the expected window.</div>`;const p=x.portfolio||{};const scope=(p.scope||[]).map(k=>NAMES[k]||k).join(' + ');const warns=p.warnings||[];document.getElementById('portfolioNote').innerHTML=p.complete?`<span class="green"><strong>LIVE scope: ${scope||'none'} · complete</strong></span>`:`<span class="amber"><strong>LIVE scope: ${scope||'none'} · ${warns.join(' · ')||'partial'}</strong></span>`;document.getElementById('portfolio').innerHTML=[card('Portfolio NAV',money(p.nav_gbp),p.nav_disagreement?'LIVE NAV materially different — review':`Shared live broker NAV · freshest ${NAMES[p.nav_source]||p.nav_source||'source'}${num(p.nav_spread_gbp)!==null&&num(p.nav_spread_gbp)>0?' · source drift '+money(p.nav_spread_gbp):''}`),card('Unrealised P&L',money(p.unrealised_pnl_gbp),'LIVE strategies only'),card('MTD Realised',money(p.realised_month_gbp),'LIVE strategies only'),card('Open Risk',money(p.open_risk_estimate_gbp),'LIVE open trades × approved risk'),card('Drawdown',money(p.drawdown_gbp),'Stage 2')].join('');document.getElementById('accounting').innerHTML=ORDER.map(k=>{const s=x.sources[k]||{},a=(s.data||{}).accounting||{};return `<strong>${NAMES[k]}</strong> — Today ${money(a.realised_today_gbp)} · Week ${money(a.realised_week_gbp)} · Month ${money(a.realised_month_gbp)} · All time ${money(a.realised_all_time_gbp)}`}).join('<br>');document.getElementById('exposure').innerHTML=ORDER.map(k=>{const d=(x.sources[k]||{}).data||{},b=d.basket||{};return `<strong>${NAMES[k]}</strong> — ${intval(b.open_trades)} open · risk/trade ${money(d.risk_per_trade_gbp)} · basket ${money(b.pnl_gbp)}`}).join('<br>');document.getElementById('health').innerHTML=ORDER.map(k=>{const s=x.sources[k]||{},err=s.error?` · ${s.error}`:'';const sig=s.signal||{};return `<strong>${NAMES[k]}</strong> — service ${s.ok&&!s.stale&&s.contract_match!==false?'OK':s.contract_match===false?'SCHEMA MISMATCH':s.stale?'STALE':'DEGRADED'} · signal ${(sig.status||'UNKNOWN')} · last signal ${when(sig.last_signal_at_utc)} · build ${s.reported_build||'—'} · schema ${s.reported_schema_version??'—'}/${s.required_schema_version??'—'} · last good ${when(s.last_good_at_utc)}${err}`}).join('<br>');document.getElementById('topStatus').textContent='Last Portfolio Hub refresh '+when(x.generated_at_utc)+' · auto-refresh 20s'}catch(e){document.getElementById('topStatus').textContent='Portfolio Hub API unavailable: '+e}}
load(); setInterval(load,20000);
</script></body></html>'''
