"""Independent, read-only AI Observer watchdog. No trading or AI API calls."""
import json
import time
import urllib.request
from datetime import datetime, timezone

PATHS = {
    "indices": ("/ai-regime-observer/status", "/api/ai-observer/status"),
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
                result[name] = {
                    "state": state, "alerts": alerts,
                    "capture_age_hours": audit.get("capture_age_hours"),
                    "complete_age_hours": audit.get("latest_complete_age_hours"),
                    "outcome_24h": audit.get("forward_24h"),
                    "reason": "endpoint responded" if state != "UNVERIFIED" else "freshness not verified by source",
                    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                }
                break
            except Exception:
                continue
    return result
