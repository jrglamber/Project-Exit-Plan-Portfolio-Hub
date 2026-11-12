"""Guard combined LIVE XAU + XAG Short Hub P&L without mixing historical XAU."""
import pathlib
import unittest
from datetime import datetime, timedelta, timezone

import hub_metals_live_positions_bridge as bridge


def positions():
    return {
        "ok": True, "scope": bridge.EXPECTED_SCOPE,
        "xau_ok": True, "xag_ok": True, "practice_excluded": True,
        "xag_broker_link_reconciled": True,
        "xag_unrealized_complete": True,
        "xag_untracked_broker_count": 0,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "xau_count": 2, "xag_count": 3, "xag_short_count": 3, "count": 5,
        "xag_unrealized_gbp": 2.9,
        "last_opened_at_utc": "2026-11-12T15:00:07Z",
        "trades": [
            {"lane":"XAU_LIVE","instrument":"XAU_USD","side":"SHORT",
             "broker_trade_id":"xau1","unrealized_gbp":6.0},
            {"lane":"XAU_LIVE","instrument":"XAU_USD","side":"SHORT",
             "broker_trade_id":"xau2","unrealized_gbp":7.0},
            {"lane":"XAG_SHORT_LIVE","instrument":"XAG_USD","side":"SHORT",
             "broker_trade_id":"xag1","unrealized_gbp":1.25,"tracking":"TRACKED"},
            {"lane":"XAG_SHORT_LIVE","instrument":"XAG_USD","side":"SHORT",
             "broker_trade_id":"xag2","unrealized_gbp":1.50,"tracking":"TRACKED"},
            {"lane":"XAG_SHORT_LIVE","instrument":"XAG_USD","side":"SHORT",
             "broker_trade_id":"xag3","unrealized_gbp":0.15,"tracking":"TRACKED"},
        ],
    }


class LiveMetalsBridgeTests(unittest.TestCase):
    def test_combined_live_count_and_cash_pnl(self):
        proof=bridge.verify(positions())
        self.assertEqual(proof["open_count"],5)
        self.assertEqual(proof["xau_count"],2)
        self.assertEqual(proof["xag_short_count"],3)
        self.assertAlmostEqual(proof["open_pnl_gbp"],15.9)
        self.assertAlmostEqual(proof["xag_open_pnl_gbp"],2.9)
        summary={
            "basket":{"open_trades":2,"pnl_gbp":13.0,"pnl_r":2.0,
                      "high_water_gbp":47.23,"giveback_gbp":35.0},
            "accounting":{"realised_all_time_gbp":219.75},
        }
        bridge.apply(summary,proof)
        basket=summary["basket"]
        self.assertEqual(basket["open_trades"],5)
        self.assertAlmostEqual(basket["pnl_gbp"],15.9)
        self.assertIsNone(basket["pnl_r"])
        self.assertEqual(basket["xau_only_open_trades"],2)
        self.assertEqual(basket["xau_only_pnl_r"],2.0)
        self.assertEqual(basket["high_water_gbp"],47.23)
        self.assertEqual(basket["giveback_gbp"],35.0)
        self.assertEqual(summary["accounting"]["realised_all_time_gbp"],219.75)
        self.assertEqual(basket["high_water_scope"],"LIVE_XAU_ONLY")
        self.assertEqual(summary["accounting"]["realised_scope"],
                         "LIVE_XAU_ONLY_XAG_LEDGER_PENDING")

    def test_unavailable_broker_is_not_xau_only_fallback(self):
        s={"basket":{"open_trades":2,"pnl_gbp":13.0,"pnl_r":2.0},
           "accounting":{}}
        bridge.mark_incomplete(s,"broker unavailable")
        self.assertIsNone(s["basket"]["open_trades"])
        self.assertIsNone(s["basket"]["pnl_gbp"])
        self.assertEqual(s["basket"]["xau_only_open_trades"],2)
        self.assertEqual(s["basket"]["live_positions_status"],"UNVERIFIED")

    def test_disallow_unlinked_xag(self):
        x=positions()
        x["trades"][-1]["tracking"]="UNLINKED — CHECK"
        with self.assertRaisesRegex(ValueError,"Untracked"):
            bridge.verify(x)

    def test_disallow_wrong_combined_counts(self):
        x=positions()
        x["count"]=4
        with self.assertRaisesRegex(ValueError,"counts conflict"):
            bridge.verify(x)

    def test_disallow_incomplete_xag_upl(self):
        x=positions()
        x["xag_unrealized_complete"]=False
        with self.assertRaisesRegex(ValueError,"incomplete"):
            bridge.verify(x)

    def test_disallow_practice_or_long_xag(self):
        x=positions()
        x["trades"][-1]["side"]="LONG"
        with self.assertRaisesRegex(ValueError,"non-short"):
            bridge.verify(x)
        y=positions()
        y["practice_excluded"]=False
        with self.assertRaisesRegex(ValueError,"Practice"):
            bridge.verify(y)

    def test_disallow_broker_pnl_mismatch(self):
        x=positions()
        x["xag_unrealized_gbp"]=4.5
        with self.assertRaisesRegex(ValueError,"P&L mismatch"):
            bridge.verify(x)

    def test_disallow_stale_broker_sample(self):
        x=positions()
        x["observed_at_utc"]=(datetime.now(timezone.utc)-timedelta(minutes=8)).isoformat()
        with self.assertRaisesRegex(ValueError,"stale"):
            bridge.verify(x)

    def test_real_hub_code_wired_to_live_producer_and_mixed_scope_labels(self):
        app=pathlib.Path("app.py").read_text()
        overlay=pathlib.Path("hub_metals_candidate_support_overlay.py").read_text()
        self.assertIn('"/api/live-metals-open-positions"', app)
        self.assertIn('metals_live.apply(validated, proof)',app)
        self.assertIn('metals_live.mark_incomplete(validated,',app)
        self.assertIn('basket.get("xau_only_open_trades")',app)
        self.assertIn("XAU only",app)
        self.assertIn("/api/live-metals-open-positions",overlay)
        self.assertNotIn('metals + "/api/live-xau-trade-tile"',overlay)
        self.assertIn("LIVE XAG SHORT",overlay)


if __name__ == "__main__":
    unittest.main()
