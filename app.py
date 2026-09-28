from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from financial_review import financial_review_line
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

APP_NAME = "Project Exit Plan — Portfolio Hub"
APP_VERSION = "0.4.0"
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

# Short live-lane descriptions for the cockpit. These are presentation-only and
# can be overridden without changing producer contracts.
STRATEGY_LANE_NOTES = {
    "indices": os.getenv("INDICES_LANE_NOTE", "NAS100 + US500 live - long-only"),
    "metals": os.getenv("METALS_LANE_NOTE", "XAU LONG live - XAU SHORT / XAG research"),
    "bco": os.getenv("BCO_LANE_NOTE", "BCO live"),
}

# v0.3.0: producer app versions are informational only.
# Compatibility is enforced by the shared portfolio-summary schema contract,
# not by exact Indices/Metals/BCO build numbers. Producer apps can therefore
# be upgraded independently without requiring a Portfolio Hub redeploy.
REQUIRED_SOURCE_SCHEMA_VERSION = SCHEMA_VERSION

LIVE_PORTFOLIO_STRATEGIES = tuple(
    x.strip().lower()
    for x in os.getenv("AGGREGATE_LIVE_STRATEGIES", "indices,metals,bco").split(",")
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

# v0.4.0 cockpit telemetry is deliberately read-only. It observes the aggregate
# snapshots already fetched by the Hub; it never writes to producer services.
_telemetry_lock = threading.RLock()
_previous_material_state: Dict[str, Dict[str, Any]] = {}
_recent_activity = []
_performance_history: Dict[str, Dict[str, Any]] = {}
_portfolio_nav_high_water: Optional[float] = None


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
        try:
            print(financial_review_line(aggregate_snapshot()), flush=True)
        except Exception as exc:
            print('PEP_FINANCIAL_REVIEW_ERROR ' + str(exc), flush=True)
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



def _next_month_end_utc() -> str:
    now = datetime.now(timezone.utc)
    if now.month == 12:
        first_next = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        first_next = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return (first_next - timedelta(seconds=1)).isoformat()


def _append_activity(kind: str, strategy: str, message: str, tone: str = "info") -> None:
    _recent_activity.insert(0, {
        "at_utc": now_iso(),
        "kind": kind,
        "strategy": strategy,
        "label": SOURCES.get(strategy, {}).get("label", strategy.title()),
        "message": message,
        "tone": tone,
    })
    del _recent_activity[40:]


def _record_material_state(views: Dict[str, Dict[str, Any]]) -> None:
    with _telemetry_lock:
        for key, view in views.items():
            data = view.get("data") if isinstance(view, dict) else None
            if not isinstance(data, dict):
                continue
            basket = data.get("basket") or {}
            exit_mgmt = data.get("exit_management") or {}
            accounting = data.get("accounting") or {}
            signal = view.get("signal") or {}
            current = {
                "mode": str(data.get("mode") or "unknown").lower(),
                "open_trades": safe_int(basket.get("open_trades")) or 0,
                "realised_all_time_gbp": safe_float(accounting.get("realised_all_time_gbp")),
                "high_water_gbp": safe_float(basket.get("high_water_gbp")),
                "manager": str(exit_mgmt.get("current_manager") or ""),
                "pending": safe_int(exit_mgmt.get("pending_broker_actions")) or 0,
                "signal": str(signal.get("status") or "UNKNOWN").upper(),
                "stale": bool(view.get("stale")),
                "ok": bool(view.get("ok")),
            }
            previous = _previous_material_state.get(key)
            if previous:
                prev_open = int(previous.get("open_trades") or 0)
                open_now = int(current["open_trades"])
                if prev_open == 0 and open_now > 0:
                    _append_activity("basket_opened", key, f"Basket opened - {open_now} trades", "good")
                elif prev_open > 0 and open_now == 0:
                    _append_activity("basket_flattened", key, "Basket flattened", "good")
                elif open_now < prev_open:
                    reduction = prev_open - open_now
                    material_cut = reduction >= max(3, int(prev_open * 0.25))
                    if material_cut:
                        _append_activity("basket_reduced", key, f"Basket reduced {prev_open} -> {open_now} trades", "info")

                prev_realised = safe_float(previous.get("realised_all_time_gbp"))
                cur_realised = safe_float(current.get("realised_all_time_gbp"))
                if prev_realised is not None and cur_realised is not None:
                    delta = cur_realised - prev_realised
                    if abs(delta) >= 0.01:
                        sign = "+" if delta > 0 else ""
                        _append_activity("realised", key, f"Realised P&L changed {sign}GBP {delta:.2f}", "good" if delta > 0 else "bad")

                prev_hwm = safe_float(previous.get("high_water_gbp"))
                cur_hwm = safe_float(current.get("high_water_gbp"))
                if prev_hwm is not None and cur_hwm is not None and open_now > 0 and cur_hwm < prev_hwm - 0.01:
                    _append_activity("high_water_reset", key, f"High-water reset GBP {prev_hwm:.2f} -> GBP {cur_hwm:.2f}", "info")

                if current["manager"] and previous.get("manager") and current["manager"] != previous.get("manager"):
                    _append_activity("manager_change", key, f"Exit manager {previous.get('manager')} -> {current['manager']}", "info")

                if current["mode"] != previous.get("mode"):
                    _append_activity("mode_change", key, f"Mode changed {str(previous.get('mode')).upper()} -> {current['mode'].upper()}", "info")

                if current["pending"] > 0 and int(previous.get("pending") or 0) == 0:
                    _append_activity("pending_broker_action", key, f"{current['pending']} pending broker action(s)", "warn")

                if current["signal"] != previous.get("signal"):
                    if current["signal"] in {"STALE", "UNKNOWN"}:
                        _append_activity("signal_health", key, f"Signal feed {current['signal']}", "bad")
                    elif previous.get("signal") in {"STALE", "UNKNOWN", "LATE"} and current["signal"] == "OK":
                        _append_activity("signal_health", key, "Signal feed recovered", "good")
            _previous_material_state[key] = current


def _record_performance_history(live_rows: list) -> None:
    now = datetime.now(timezone.utc)
    month_prefix = now.strftime("%Y-%m")
    hour_key = now.strftime("%Y-%m-%dT%H:00Z")
    point = {"at_utc": now_iso(), "hour": hour_key}
    total = 0.0
    any_total = False
    for key, data, _view in live_rows:
        value = safe_float((data.get("accounting") or {}).get("realised_month_gbp"))
        point[key] = value
        if value is not None:
            total += value
            any_total = True
    point["total"] = total if any_total else None
    with _telemetry_lock:
        _performance_history[hour_key] = point
        stale_keys = [k for k in _performance_history if not k.startswith(month_prefix)]
        for k in stale_keys:
            _performance_history.pop(k, None)
        if len(_performance_history) > 800:
            for k in sorted(_performance_history)[:-800]:
                _performance_history.pop(k, None)


def aggregate_snapshot() -> Dict[str, Any]:
    global _portfolio_nav_high_water
    views = {k: source_view(k) for k in SOURCES}

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
    nav_spread_gbp = max(nav_values) - min(nav_values) if len(nav_values) > 1 else 0.0
    nav_reference = sum(nav_values) / len(nav_values) if nav_values else 0.0
    nav_tolerance_gbp = max(NAV_MISMATCH_ABS_GBP, abs(nav_reference) * NAV_MISMATCH_PCT)
    nav_disagreement = bool(len(nav_values) > 1 and nav_spread_gbp > nav_tolerance_gbp)
    if nav_disagreement:
        portfolio_warnings.append("live NAV mismatch")

    with _telemetry_lock:
        if nav is not None and (_portfolio_nav_high_water is None or nav > _portfolio_nav_high_water):
            _portfolio_nav_high_water = nav
        nav_hwm = _portfolio_nav_high_water
    drawdown = (nav_hwm - nav) if nav is not None and nav_hwm is not None else None

    pnl_values = [
        d["basket"].get("pnl_gbp")
        for d in live_values
        if d.get("basket", {}).get("pnl_gbp") is not None
    ]
    total_unrealised = sum(pnl_values) if pnl_values else None

    def sum_accounting(field: str) -> Optional[float]:
        values = [safe_float((d.get("accounting") or {}).get(field)) for d in live_values]
        values = [v for v in values if v is not None]
        return sum(values) if values else None

    realised_today = sum_accounting("realised_today_gbp")
    realised_week = sum_accounting("realised_week_gbp")
    realised_month = sum_accounting("realised_month_gbp")
    realised_all_time = sum_accounting("realised_all_time_gbp")
    month_total = (
        (realised_month or 0.0) + (total_unrealised or 0.0)
        if realised_month is not None or total_unrealised is not None
        else None
    )

    open_risk_values = []
    for d in live_values:
        count = d.get("basket", {}).get("open_trades")
        risk = d.get("risk_per_trade_gbp")
        if count is not None and risk is not None:
            open_risk_values.append(max(0, count) * max(0.0, risk))
    open_risk = sum(open_risk_values) if open_risk_values else None

    signal_alerts = []
    attention = []
    for key, view in views.items():
        data = view.get("data") if isinstance(view, dict) else None
        sig = view.get("signal") or {}
        status = str(sig.get("status") or "UNKNOWN").upper()
        mode = ((data or {}).get("mode") if isinstance(data, dict) else None)
        if status in ("STALE", "UNKNOWN") and sig.get("expected_now"):
            signal_alerts.append({
                "strategy": key, "label": SOURCES[key]["label"], "status": status,
                "last_signal_at_utc": sig.get("last_signal_at_utc"),
                "age_seconds": sig.get("age_seconds"), "mode": mode,
            })
        if not view.get("configured"):
            attention.append({"strategy": key, "severity": "bad", "message": "Source not configured"})
        elif not isinstance(data, dict):
            attention.append({"strategy": key, "severity": "bad", "message": "Source unavailable"})
        else:
            if view.get("stale"):
                attention.append({"strategy": key, "severity": "warn", "message": "Producer data stale"})
            if view.get("contract_match") is False:
                attention.append({"strategy": key, "severity": "bad", "message": "Schema mismatch"})
            health = data.get("health") or {}
            for health_key, label in (("broker_ok", "Broker"), ("database_ok", "Database"), ("worker_ok", "Worker")):
                if health.get(health_key) is False:
                    attention.append({"strategy": key, "severity": "bad", "message": f"{label} health failed"})
            pending = safe_int((data.get("exit_management") or {}).get("pending_broker_actions"))
            if pending:
                attention.append({"strategy": key, "severity": "warn", "message": f"{pending} pending broker action(s)"})
            if status in {"STALE", "UNKNOWN"} and sig.get("expected_now"):
                attention.append({"strategy": key, "severity": "bad", "message": f"Signal feed {status}"})
            elif status == "LATE":
                attention.append({"strategy": key, "severity": "warn", "message": "Signal feed late"})
    if nav_disagreement:
        attention.append({"strategy": "portfolio", "severity": "warn", "message": "Live NAV sources disagree materially"})

    _record_material_state(views)
    _record_performance_history(live_rows)

    portfolio_row = {
        "scope": list(LIVE_PORTFOLIO_STRATEGIES),
        "complete": len(portfolio_warnings) == 0,
        "warnings": portfolio_warnings,
        "nav_gbp": nav,
        "nav_source": nav_source,
        "nav_spread_gbp": nav_spread_gbp,
        "nav_tolerance_gbp": nav_tolerance_gbp,
        "nav_disagreement": nav_disagreement,
        "unrealised_pnl_gbp": total_unrealised,
        "realised_today_gbp": realised_today,
        "realised_week_gbp": realised_week,
        "realised_month_gbp": realised_month,
        "realised_all_time_gbp": realised_all_time,
        "month_total_gbp": month_total,
        "open_risk_estimate_gbp": open_risk,
        "nav_high_water_gbp": nav_hwm,
        "drawdown_gbp": drawdown,
        "drawdown_scope": "hub_observed_since_process_start",
    }

    risk_review = []
    for key, data, _view in live_rows:
        risk = safe_float(data.get("risk_per_trade_gbp"))
        source_nav = safe_float(data.get("nav_gbp")) or nav
        pct_nav = (risk / source_nav * 100.0) if risk is not None and source_nav else None
        risk_review.append({
            "strategy": key, "label": SOURCES[key]["label"],
            "risk_per_trade_gbp": risk, "nav_gbp": source_nav,
            "risk_pct_nav": pct_nav, "proposal_gbp": None,
            "approval_status": "MANUAL_REVIEW",
            "applies_to_existing_positions": False,
        })

    with _telemetry_lock:
        performance = [_performance_history[k] for k in sorted(_performance_history)]
        recent_activity = list(_recent_activity[:12])

    return {
        "app": APP_NAME, "version": APP_VERSION, "generated_at_utc": now_iso(),
        "sources": views, "strategy_lane_notes": STRATEGY_LANE_NOTES,
        "signal_feed": {
            "status": "ALERT" if signal_alerts else "OK", "alerts": signal_alerts,
            "expected_now": signal_expected_now(), "warn_after_seconds": SIGNAL_WARN_AFTER_SECONDS,
            "stale_after_seconds": SIGNAL_STALE_AFTER_SECONDS,
        },
        "portfolio": portfolio_row,
        "attention": {
            "status": "ACTION_REQUIRED" if any(x.get("severity") == "bad" for x in attention) else ("REVIEW" if attention else "OK"),
            "items": attention,
        },
        "recent_activity": recent_activity,
        "performance": {
            "scope": "current_month_hub_observed", "points": performance,
            "note": "Hourly MTD realised snapshots captured by this Hub process; history begins/restarts with the Hub process.",
        },
        "risk_review": {
            "review_due_at_utc": _next_month_end_utc(), "approval_mode": "MANUAL_ONLY",
            "existing_positions_never_resized": True, "strategies": risk_review,
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
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project Exit Plan - Portfolio Hub</title>
<style>
:root{--bg:#0b0f14;--panel:#141a21;--panel2:#0f151c;--border:#29313a;--text:#f4f6f8;--muted:#98a4b1;--green:#5fd99a;--amber:#f0c45b;--red:#ff7b72;--blue:#7db7ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Arial,Helvetica,sans-serif;padding:12px}.page{max-width:1280px;margin:0 auto}
h1{margin:2px 0 3px;font-size:28px}.sub{color:var(--muted);font-size:11px;margin-bottom:10px}.section-title{font-size:16px;font-weight:800;margin:16px 0 7px}
.grid{display:grid;gap:7px}.headline{grid-template-columns:repeat(4,minmax(0,1fr))}.cards{grid-template-columns:repeat(4,minmax(0,1fr));padding:8px}
.strategy{background:var(--panel);border:1px solid var(--border);border-radius:12px;margin:8px 0;overflow:hidden}.strategy-head{display:flex;justify-content:space-between;gap:8px;align-items:flex-start;padding:10px 11px;border-bottom:1px solid var(--border)}.strategy-title{font-weight:900;font-size:17px}.lane{color:var(--muted);font-size:10px;margin-top:3px}.badges{display:flex;gap:5px;align-items:center;justify-content:flex-end;flex-wrap:wrap}.badge,.pill{font-size:9px;font-weight:800;border:1px solid var(--border);border-radius:999px;padding:3px 6px}.green{color:var(--green)}.amber{color:var(--amber)}.red{color:var(--red)}.muted{color:var(--muted)}
.card{background:var(--panel2);border:1px solid var(--border);border-radius:9px;padding:9px;min-height:72px}.label{color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.05em}.value{font-size:21px;font-weight:900;margin-top:5px}.small{font-size:10px;color:var(--muted);margin-top:4px;line-height:1.35}.top-status{font-size:10px;color:var(--muted);margin:6px 0}
.healthbar,.okbar,.warnbar,.badbar{border-radius:9px;padding:8px 10px;font-size:11px;font-weight:800;margin:7px 0}.healthbar,.okbar{border:1px solid #275e42;background:#0e2118;color:var(--green)}.warnbar{border:1px solid #6d5b28;background:#241d0d;color:var(--amber)}.badbar{border:1px solid #713334;background:#291417;color:var(--red)}
.attention{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}.attention-item{background:var(--panel);border:1px solid var(--border);border-radius:9px;padding:9px;font-size:11px}.attention-item.bad{border-color:#713334}.attention-item.warn{border-color:#6d5b28}
.split{display:grid;grid-template-columns:1.25fr .75fr;gap:8px}.panel{background:var(--panel);border:1px solid var(--border);border-radius:11px;padding:10px}.activity-row,.research-row,.risk-row{display:grid;grid-template-columns:110px 1fr auto;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid var(--border);font-size:11px}.activity-row:last-child,.research-row:last-child,.risk-row:last-child{border-bottom:0}.time{color:var(--muted);font-size:9px}.chart-wrap{height:210px;position:relative}.chart-empty{color:var(--muted);font-size:11px;padding:18px 4px}
details{background:var(--panel);border:1px solid var(--border);border-radius:10px;margin:8px 0}summary{cursor:pointer;padding:10px;font-weight:800;font-size:12px}.body{border-top:1px solid var(--border);padding:10px;color:var(--muted);font-size:11px;line-height:1.5}
@media(max-width:900px){.headline,.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.split{grid-template-columns:1fr}.attention{grid-template-columns:1fr 1fr}}
@media(max-width:560px){body{padding:7px}h1{font-size:23px}.attention{grid-template-columns:1fr}.value{font-size:18px}.strategy-head{display:block}.badges{justify-content:flex-start;margin-top:7px}.activity-row,.research-row,.risk-row{grid-template-columns:78px 1fr}.activity-row>*:last-child,.research-row>*:last-child,.risk-row>*:last-child{grid-column:2}.chart-wrap{height:180px}}
</style></head>
<body><div class="page">
<h1>Project Exit Plan - Portfolio Hub</h1>
<div class="sub">v0.4.0 &middot; portfolio cockpit &middot; read-only &middot; live-money first</div>
<div class="section-title">Portfolio</div><div id="portfolioScope" class="top-status">Loading...</div><div id="headline" class="grid headline"></div><div id="healthBar" class="healthbar">Checking health...</div>
<div class="section-title">Live Strategies</div><div id="strategies"></div>
<div class="section-title">Needs Attention</div><div id="attention"></div>
<div class="split"><div><div class="section-title">Performance</div><div class="panel"><div class="small">Cumulative realised P&amp;L this month - Hub-captured hourly snapshots</div><div id="performanceChart" class="chart-wrap"></div></div></div><div><div class="section-title">Recent Activity</div><div id="recentActivity" class="panel"></div></div></div>
<div class="section-title">Research &amp; Challengers</div><div id="research" class="panel"><div class="small">Loading research status...</div></div>
<div class="section-title">Monthly Risk Review</div><div id="riskReview" class="panel"></div>
<details><summary>Accounting detail</summary><div class="body" id="accounting"></div></details>
<details><summary>Exposure detail</summary><div class="body" id="exposure"></div></details>
<details><summary>System Health</summary><div class="body" id="health"></div></details>
<div id="topStatus" class="top-status"></div></div>
<script>
var ORDER=['indices','metals','bco']; var NAMES={indices:'Indices',metals:'Metals',bco:'BCO',portfolio:'Portfolio'};
function num(v){var n=Number(v);return Number.isFinite(n)?n:null}
function money(v){var n=num(v);if(n===null)return '-';return (n<0?'-':'')+'&pound;'+Math.abs(n).toLocaleString('en-GB',{minimumFractionDigits:2,maximumFractionDigits:2})}
function rr(v){var n=num(v);if(n===null)return '-';return (n>0?'+':'')+n.toFixed(1)+'R'}
function pct(v){var n=num(v);return n===null?'-':n.toFixed(3)+'%'}
function intval(v){var n=num(v);return n===null?'-':String(Math.round(n))}
function cls(v){var n=num(v);return n===null?'':(n>0?'green':n<0?'red':'')}
function when(v){if(!v)return '-';try{return new Date(v).toLocaleString('en-GB',{timeZone:'Europe/London',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'})}catch(e){return String(v)}}
function stateBadge(s){if(!s.configured)return '<span class="badge red">NOT CONFIGURED</span>';if(!s.data)return '<span class="badge red">UNAVAILABLE</span>';if(s.contract_match===false)return '<span class="badge red">SCHEMA MISMATCH</span>';if(s.stale)return '<span class="badge amber">STALE</span>';if(s.ok)return '<span class="badge green">HEALTHY</span>';return '<span class="badge amber">LAST GOOD</span>'}
function card(label,val,sub){return '<div class="card"><div class="label">'+label+'</div><div class="value">'+val+'</div><div class="small">'+(sub||'')+'</div></div>'}
function strategyHtml(key,s,notes){var d=s.data||{},b=d.basket||{},a=d.accounting||{},em=d.exit_management||{};var mode=(d.mode||'unknown').toUpperCase(),status=(d.status||'UNKNOWN').toUpperCase(),dir=(b.direction||'').toUpperCase();var lane=(notes||{})[key]||'';var riskPct=(num(d.risk_per_trade_gbp)!==null&&num(d.nav_gbp))?pct(num(d.risk_per_trade_gbp)/num(d.nav_gbp)*100):'-';var next=(em.next_cycle_manager&&em.next_cycle_manager!==em.current_manager)?' / next '+em.next_cycle_manager:'';return '<div class="strategy"><div class="strategy-head"><div><div class="strategy-title">'+NAMES[key]+'</div><div class="lane">'+lane+'</div></div><div class="badges"><span class="badge">'+mode+'</span><span class="badge">'+status+(dir?' / '+dir:'')+'</span>'+stateBadge(s)+'</div></div><div class="grid cards">'+card('Open P&L','<span class="'+cls(b.pnl_gbp)+'">'+money(b.pnl_gbp)+'</span>',rr(b.pnl_r))+card('Realised W / M',money(a.realised_week_gbp),'Month '+money(a.realised_month_gbp))+card('All-time realised',money(a.realised_all_time_gbp),'Today '+money(a.realised_today_gbp))+card('High water',money(b.high_water_gbp),rr(b.high_water_r)+' / '+when(b.high_water_at_utc))+card('Giveback',money(b.giveback_gbp),rr(b.giveback_r))+card('Open trades',intval(b.open_trades),'Last opened '+when(b.last_trade_opened_at_utc))+card('Approved risk / trade',money(d.risk_per_trade_gbp),riskPct+' of NAV')+card('Manager',em.current_manager||'-',next+(em.pending_broker_actions?' / '+em.pending_broker_actions+' pending':''))+'</div></div>'}
function renderAttention(att){var box=document.getElementById('attention'),items=(att||{}).items||[];if(!items.length){box.innerHTML='<div class="okbar">&#10003; No action required</div>';return}box.innerHTML='<div class="attention">'+items.map(function(x){return '<div class="attention-item '+(x.severity||'')+'"><strong>'+(NAMES[x.strategy]||x.strategy)+'</strong><div class="'+(x.severity==='bad'?'red':x.severity==='warn'?'amber':'')+'" style="margin-top:4px">'+x.message+'</div></div>'}).join('')+'</div>'}
function renderActivity(items){var box=document.getElementById('recentActivity');if(!items||!items.length){box.innerHTML='<div class="small">No material events captured since this Hub process started.</div>';return}box.innerHTML=items.map(function(x){return '<div class="activity-row"><div class="time">'+when(x.at_utc)+'</div><div><strong>'+(NAMES[x.strategy]||x.label||x.strategy)+'</strong><br>'+x.message+'</div><span class="pill '+(x.tone==='good'?'green':x.tone==='bad'?'red':x.tone==='warn'?'amber':'muted')+'">'+String(x.kind||'event').replaceAll('_',' ')+'</span></div>'}).join('')}
function renderRisk(risk){var rows=(risk||{}).strategies||[],due=when((risk||{}).review_due_at_utc);document.getElementById('riskReview').innerHTML='<div class="small">Manual approval only - existing positions are never resized automatically - next review due '+due+'</div>'+(rows.length?rows.map(function(r){return '<div class="risk-row"><strong>'+(r.label||NAMES[r.strategy])+'</strong><div>Current '+money(r.risk_per_trade_gbp)+' / trade - '+pct(r.risk_pct_nav)+' of NAV</div><span class="pill amber">MANUAL REVIEW</span></div>'}).join(''):'<div class="small">No live strategy risk rows available.</div>')}
function renderChart(points){var el=document.getElementById('performanceChart');if(!points||points.length<2){el.innerHTML='<div class="chart-empty">Collecting hourly realised-P&amp;L history. This chart populates as the Hub observes the month.</div>';return}var keys=['total','indices','metals','bco'],vals=[];points.forEach(function(p){keys.forEach(function(k){var n=num(p[k]);if(n!==null)vals.push(n)})});if(!vals.length){el.innerHTML='<div class="chart-empty">No realised-P&amp;L data available yet.</div>';return}var min=Math.min.apply(null,vals),max=Math.max.apply(null,vals);if(min===max){min-=1;max+=1}var pad=(max-min)*.12;min-=pad;max+=pad;var w=900,h=190,left=55,right=10,top=10,bottom=28,iw=w-left-right,ih=h-top-bottom;function x(i){return left+(points.length===1?0:i/(points.length-1))*iw}function y(v){return top+(max-v)/(max-min)*ih}var colors={total:'#f4f6f8',indices:'#7db7ff',metals:'#5fd99a',bco:'#f0c45b'};var paths=keys.map(function(k){var seg=[];points.forEach(function(p,i){var v=num(p[k]);if(v!==null)seg.push((seg.length?'L':'M')+x(i).toFixed(1)+','+y(v).toFixed(1))});return seg.length?'<path d="'+seg.join(' ')+'" fill="none" stroke="'+colors[k]+'" stroke-width="'+(k==='total'?3:1.8)+'"/>':''}).join('');el.innerHTML='<svg viewBox="0 0 '+w+' '+h+'" width="100%" height="100%" preserveAspectRatio="none"><line x1="'+left+'" x2="'+left+'" y1="'+top+'" y2="'+(h-bottom)+'" stroke="#29313a"/><line x1="'+left+'" x2="'+(w-right)+'" y1="'+(h-bottom)+'" y2="'+(h-bottom)+'" stroke="#29313a"/>'+paths+'<text x="4" y="'+(top+7)+'" fill="#98a4b1" font-size="10">'+money(max)+'</text><text x="4" y="'+(h-bottom)+'" fill="#98a4b1" font-size="10">'+money(min)+'</text></svg>'}
async function loadResearch(){try{var r=await fetch('/api/research-summary',{cache:'no-store'}),x=await r.json(),rows=x.sources||{};document.getElementById('research').innerHTML=ORDER.map(function(k){var d=rows[k]||{},main=d.main_challenger||{},sample=(main.sample!==null&&main.sample!==undefined)?' / sample '+main.sample:'';return '<div class="research-row"><strong>'+NAMES[k]+'</strong><div>Current '+(d.current_manager||'-')+' / Main challenger '+(main.label||'collecting')+sample+'<div class="small">'+(main.status||d.status||'research-only')+'</div></div><span class="pill '+(d.ok?'green':'amber')+'">'+(d.ok?'COLLECTING':'DEGRADED')+'</span></div>'}).join('')}catch(e){document.getElementById('research').innerHTML='<div class="small">Research summary unavailable; producer research collection is independent of this dashboard.</div>'}}
async function load(){try{var res=await fetch('/api/aggregate',{cache:'no-store'}),x=await res.json(),p=x.portfolio||{},scope=(p.scope||[]).map(function(k){return NAMES[k]||k}).join(' + ');document.getElementById('portfolioScope').innerHTML='LIVE scope: <strong>'+(scope||'none')+'</strong> / open risk '+money(p.open_risk_estimate_gbp)+' / refreshed '+when(x.generated_at_utc);document.getElementById('headline').innerHTML=[card('Portfolio NAV',money(p.nav_gbp),'Freshest '+(NAMES[p.nav_source]||p.nav_source||'source')),card('Open P&L','<span class="'+cls(p.unrealised_pnl_gbp)+'">'+money(p.unrealised_pnl_gbp)+'</span>','Live strategies only'),card('Today realised','<span class="'+cls(p.realised_today_gbp)+'">'+money(p.realised_today_gbp)+'</span>','Live strategies only'),card('Week realised','<span class="'+cls(p.realised_week_gbp)+'">'+money(p.realised_week_gbp)+'</span>','Live strategies only'),card('Month total','<span class="'+cls(p.month_total_gbp)+'">'+money(p.month_total_gbp)+'</span>','Realised '+money(p.realised_month_gbp)+' + open '+money(p.unrealised_pnl_gbp)),card('All-time realised','<span class="'+cls(p.realised_all_time_gbp)+'">'+money(p.realised_all_time_gbp)+'</span>','Live strategies only'),card('Observed drawdown',money(p.drawdown_gbp),'NAV HWM '+money(p.nav_high_water_gbp)+' / since Hub process start'),card('Open risk estimate',money(p.open_risk_estimate_gbp),'Open trades x approved risk')].join('');document.getElementById('strategies').innerHTML=ORDER.map(function(k){return strategyHtml(k,x.sources[k]||{},x.strategy_lane_notes||{})}).join('');renderAttention(x.attention);renderActivity(x.recent_activity);renderRisk(x.risk_review);renderChart((x.performance||{}).points||[]);var att=(x.attention||{}).items||[];var hb=document.getElementById('healthBar');hb.className=att.some(function(a){return a.severity==='bad'})?'badbar':att.length?'warnbar':'healthbar';hb.innerHTML=att.length?(att.length+' item(s) need review - expand System Health for detail'):'&#10003; All systems healthy - signals current - no pending actions';document.getElementById('accounting').innerHTML=ORDER.map(function(k){var a=((x.sources[k]||{}).data||{}).accounting||{};return '<strong>'+NAMES[k]+'</strong> - Today '+money(a.realised_today_gbp)+' / Week '+money(a.realised_week_gbp)+' / Month '+money(a.realised_month_gbp)+' / All time '+money(a.realised_all_time_gbp)}).join('<br>');document.getElementById('exposure').innerHTML=ORDER.map(function(k){var d=((x.sources[k]||{}).data||{}),b=d.basket||{};return '<strong>'+NAMES[k]+'</strong> - '+intval(b.open_trades)+' open / risk '+money(d.risk_per_trade_gbp)+' / basket '+money(b.pnl_gbp)+' / HWM '+money(b.high_water_gbp)+' / giveback '+money(b.giveback_gbp)}).join('<br>');document.getElementById('health').innerHTML=ORDER.map(function(k){var s=x.sources[k]||{},d=s.data||{},h=d.health||{},sig=s.signal||{},err=s.error?(' / '+s.error):'';return '<strong>'+NAMES[k]+'</strong> - service '+(s.ok&&!s.stale&&s.contract_match!==false?'OK':s.contract_match===false?'SCHEMA MISMATCH':s.stale?'STALE':'DEGRADED')+' / broker '+(h.broker_ok===false?'FAIL':'OK')+' / DB '+(h.database_ok===false?'FAIL':'OK')+' / worker '+(h.worker_ok===false?'FAIL':'OK')+' / signal '+(sig.status||'UNKNOWN')+' / build '+(s.reported_build||'-')+' / schema '+(s.reported_schema_version??'-')+'/'+(s.required_schema_version??'-')+' / last good '+when(s.last_good_at_utc)+err}).join('<br>');document.getElementById('topStatus').textContent='Portfolio Hub v'+(x.version||'0.4.0')+' / last refresh '+when(x.generated_at_utc)+' / auto-refresh 20s'}catch(e){document.getElementById('topStatus').textContent='Portfolio Hub API unavailable: '+e}}
load();loadResearch();setInterval(load,20000);setInterval(loadResearch,60000);
</script></body></html>'''
