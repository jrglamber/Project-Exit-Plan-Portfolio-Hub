"""AI observer health classification remains truthful, research-only and safe."""
import io
import json
import unittest
from unittest.mock import patch
import ai_observer_watchdog as w


class Response(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass


class WatchdogTests(unittest.TestCase):
    def run_case(self, payload):
        with patch.object(w.urllib.request, "urlopen", return_value=Response(json.dumps(payload).encode())):
            return w.check_sources({"indices": {"url": "https://example.com"}}, timeout=1)["indices"]

    def test_quota_pause_is_not_an_unexplained_backlog(self):
        row = self.run_case({
            "ok": False, "alerts": ["ELIGIBLE_CALL_OVERDUE", "NO_COMPLETED_AI_CALL_DESPITE_RECENT_ELIGIBLE_EVENTS"],
            "quota_operator_action_required": True, "paused_quota_count_last_500": 55,
            "pipeline_diagnostics": {"status_counts_last_500": {"PAUSED_QUOTA": 55}}
        })
        self.assertEqual(row["state"], "ALERT")
        self.assertEqual(row["primary_cause"], "API_QUOTA_EXHAUSTED")
        self.assertEqual(row["paused_quota_count"], 55)

    def test_http_429_is_not_silently_hidden(self):
        row = self.run_case({
            "ok": False, "alerts": ["RECENT_AI_ERRORS"],
            "active_ai_errors": [{"http_status":429}, {"http_status":429}]
        })
        self.assertEqual(row["state"], "ALERT")
        self.assertEqual(row["primary_cause"], "API_HTTP_429")
        self.assertEqual(row["active_429_count"], 2)

    def test_healthy_source_remains_ok(self):
        row = self.run_case({"ok": True, "alerts": [], "latest_complete_age_hours": 1})
        self.assertEqual(row["state"], "OK")
        self.assertIsNone(row["primary_cause"])


if __name__ == "__main__":
    unittest.main()
