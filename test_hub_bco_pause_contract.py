"""BCO live-entry pause badge must follow the producer, not LIVE service health."""
import pathlib
import unittest
from hub_bco_pause_contract import verify_bco_pause


class BcoPauseTests(unittest.TestCase):
    def payload(self, paused):
        return {"ok": True, "paused": paused,
                "scope": "production_trade_creation_only",
                "candidate_signal_preserved_for_research": True,
                "management_preserved": True}

    def test_paused_is_verified(self):
        status=verify_bco_pause(self.payload(True))
        self.assertTrue(status["ok"])
        self.assertTrue(status["paused"])
        self.assertTrue(status["management_preserved"])

    def test_unpaused_is_verified_without_claiming_trade_armed(self):
        self.assertFalse(verify_bco_pause(self.payload(False))["paused"])

    def test_malformed_or_unverified_pause_cannot_read_as_active(self):
        for invalid in ({}, {"ok":False,"paused":False},
                        {"ok":True,"paused":"false","scope":"production_trade_creation_only"},
                        {"ok":True,"paused":True,"scope":"other"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                verify_bco_pause(invalid)

    def test_dashboard_distinguishes_pause_from_worker_health(self):
        src=pathlib.Path("app.py").read_text(encoding="utf-8")
        self.assertIn("bco-live-entry-pause-v2",src)
        self.assertIn("NEW ENTRIES PAUSED",src)
        self.assertIn("ENTRY PAUSE UNKNOWN",src)
        self.assertIn("research and existing-position management continue",src)

if __name__=="__main__":
    unittest.main()
