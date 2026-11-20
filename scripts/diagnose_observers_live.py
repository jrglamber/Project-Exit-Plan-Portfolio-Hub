"""Read-only public observer status checks. Never calls an AI model or broker.

Print only operational dates/counts, a bounded error class, and response status.
"""
from __future__ import annotations
import csv
import io
import json
import urllib.error
import urllib.request

SOURCES = {
    "indices": ("https://ai-signal-bot-production-2750.up.railway.app", "/api/ai-observer/status"),
    "metals": ("https://metals-production-bb81.up.railway.app", "/api/ai-observer/status"),
    "bco": ("https://bco-live-production.up.railway.app", "/ai-regime-observer/status"),
    "hub": ("https://project-exit-plan-portfolio-hub-production.up.railway.app", "/api/ai-observer-watchdog"),
}


def fetch(url, timeout=12):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "PEP-health-proof/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def scrub_failure(v):
    v = str(v or "").strip()
    # Only an error category, not raw response bodies, prompts or API keys.
    import re
    m = re.match(r"^([A-Za-z_][A-Za-z_0-9]{0,48})(?::|$)", v)
    return m.group(1) if m else ("unspecified" if v else "")


def main():
    for name, (base, route) in SOURCES.items():
        try:
            doc = json.loads(fetch(base + route))
            if name == "hub":
                doc = (doc.get("sources") or {})
                print("hub_alerts:", json.dumps({
                    key: {"state": item.get("state"), "alerts": item.get("alerts")}
                    for key, item in doc.items()
                }, default=str))
                continue
            audit = doc.get("audit") or doc
            keys = (
                "ai_enabled", "api_key_configured", "capture_enabled",
                "worker_started", "thread_alive", "queue_size",
                "last_heartbeat_utc", "model", "counts", "observer_version",
            )
            health = {key: doc.get(key) for key in keys if key in doc}
            whitelist = (
                "ok", "alerts", "captured_rows", "capture_age_hours", "latest_complete_age_hours",
                "latest_eligible_age_hours", "error_count_recent_100", "active_ai_error_count",
                "overdue_eligible_count", "overdue_eligible", "recent_errors",
            )
            summary = {key: audit.get(key) for key in whitelist if key in audit}
            errs = audit.get("active_ai_errors") or []
            if isinstance(errs, list):
                summary["active_ai_errors"] = [
                    {"asset": e.get("asset"), "http_status": e.get("http_status"),
                     "error_class": scrub_failure(e.get("error_class")),
                     "failed_at_utc": e.get("failed_at_utc")}
                    for e in errs[:5] if isinstance(e, dict)
                ]
            print(name, json.dumps({"runtime": health, "audit": summary}, default=str))
            if name == "bco":
                raw = fetch(base + "/export/ai-regime-observer.csv?limit=60")
                txt = raw.decode("utf-8-sig", errors="replace")
                recs = list(csv.DictReader(io.StringIO(txt)))
                if recs and "status" in recs[0]:
                    print("bco_latest_rows:", json.dumps([
                        {"status": r.get("status"), "created": r.get("created_at_utc"),
                         "updated": r.get("updated_at_utc"), "completed": r.get("completed_at_utc"),
                         "eligible": r.get("api_eligible"), "http": r.get("last_http_status"),
                         "attempt": r.get("attempt_count"), "error_class": scrub_failure(r.get("error"))}
                        for r in recs[:10]
                    ]))
            if name in ("indices", "metals"):
                try:
                    raw = fetch(base + "/export/ai-regime-observer.csv?limit=50", timeout=8)
                    recs = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))))
                    if recs and "status" in recs[0]:
                        print(name + "_latest_rows:", json.dumps([
                            {"status": r.get("status"), "created": r.get("created_at_utc"),
                             "updated": r.get("updated_at_utc"), "completed": r.get("completed_at_utc"),
                             "eligible": r.get("api_eligible"), "http": r.get("last_http_status"),
                             "attempt": r.get("attempt_count"), "error_class": scrub_failure(r.get("error"))}
                            for r in recs[:10]
                        ]))
                except Exception as exc:
                    print(name + "_export_unavailable", type(exc).__name__)
        except Exception as exc:
            print(name + "_endpoint_unavailable", type(exc).__name__, str(exc)[:140])


if __name__ == "__main__":
    main()
