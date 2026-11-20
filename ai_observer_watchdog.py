"""Independent, read-only AI Observer watchdog. No trading or AI API calls."""
import json
import time
import urllib.request
from datetime import datetime, timezone

PATHS = {
    "indices": ("/api/ai-observer/status", "/ai-regime-observer/status"),
    "metals": ("/api/ai-observer/status", "/ai-regime-observer/status"),
    "bco": ("/ai-regime-observer/status",),
}

def check_sources(sources, timeout=3):
    result = {}
    for name, config in sources.items():
        base = (config.get("url") or "").rstrip("/")
        if not base:
            result[name] = {"state": "UNCONFIGURED", "reason": "service URL missing"}
            continue
        result[name] = {"state": "UNKNOWN", "reason": "observer status endpoint unavailable"}
        for path in PATHS.get(name, ()):
            try:
                req = urllib.request.Request(base + path, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    continue
                audit = payload.get("audit") or (payload if "capture_age_hours" in payload else {})
                alerts = audit.get("alerts") or []
                if alerts or payload.get("api_key_configured") is False or payload.get("ai_enabled") is False:
                    state = "ALERT"
                elif audit and audit.get("ok") is True:
                    state = "OK"
                else:
                    state = "UNVERIFIED"
                diag = audit.get("pipeline_diagnostics") or {}
                counts = diag.get("status_counts_last_500") or {}
                active_errors = audit.get("active_ai_errors") or []
                recent_error_categories = diag.get("recent_error_categories") or {}
                paused = int(audit.get("paused_quota_count_last_500") or counts.get("PAUSED_QUOTA") or 0)
                # The source stores explicit PAUSED_QUOTA / insufficient_quota
                # errors. Don't mislabel them as an ordinary worker backlog.
                if (audit.get("quota_operator_action_required") is True or
                    (paused and "NO_COMPLETED_AI_CALL_DESPITE_RECENT_ELIGIBLE_EVENTS" in alerts)):
                    cause = "API_QUOTA_EXHAUSTED"
                    guidance = "Check API credit balance and project/organization spend limits"
                elif (any(str(item.get("http_status")) == "429" for item in active_errors if isinstance(item, dict)) or
                      any(str(k).startswith("OPENAI_HTTP_429") for k in recent_error_categories)):
                    cause = "API_HTTP_429"
                    guidance = "Check provider error code for exhausted credits versus rate limit"
                elif "ELIGIBLE_CALL_OVERDUE" in alerts:
                    cause = "OBSERVER_BACKLOG"
                    guidance = "Inspect AI worker state and overdue eligible records"
                elif alerts:
                    cause = "OBSERVER_HEALTH_ALERT"
                    guidance = "Check source health and retry/error details"
                else:
                    cause = None
                    guidance = None
                result[name] = {
                    "state": state, "alerts": alerts,
                    "primary_cause": cause, "operator_action": guidance,
                    "paused_quota_count": paused,
                    "active_429_count": sum(str(item.get("http_status")) == "429" for item in active_errors if isinstance(item, dict)),
                    "capture_age_hours": audit.get("capture_age_hours"),
                    "complete_age_hours": audit.get("latest_complete_age_hours"),
                    "outcome_24h": audit.get("forward_24h"),
                    "reason": "endpoint responded" if state != "UNVERIFIED" else "freshness not verified by source",
                    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                }
                break
            except Exception as exc:
                result[name]["reason"] = f"{path}: {type(exc).__name__}: {str(exc)[:160]}"
                continue
    return result
