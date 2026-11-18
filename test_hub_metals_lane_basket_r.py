"""Hub XAG basket R may only come from matched live Metals broker+ledger proof."""
import unittest
from hub_metals_lane_basket_r import apply, mark_unavailable, SCOPE, BASIS


def evidence():
    row = {"ok": True, "scope": SCOPE, "risk_basis": BASIS,
           "reconciled": True, "practice_excluded": True,
           "broker_writes": False, "observed_at_utc": "2026-11-18T20:00:00Z",
           "open_count": 2, "current_r": 2.25, "open_pnl_gbp": 10.0}
    broker = {"ok": True, "observed_at_utc": "2026-11-18T20:00:00Z",
              "xag_short_count": 2, "xag_open_pnl_gbp": 10.0}
    return row, broker


class BasketRProofTests(unittest.TestCase):
    def test_verified_xag_r_preserves_xau_and_combined_figures(self):
        row, broker = evidence()
        summary = {"basket": {"xau_only_pnl_r": 10.0, "pnl_gbp": 50.0}}
        apply(summary, row, broker)
        self.assertEqual(summary["basket"]["live_xag_short_open_r"], 2.25)
        self.assertEqual(summary["basket"]["xau_only_pnl_r"], 10.0)
        self.assertEqual(summary["basket"]["pnl_gbp"], 50.0)
        self.assertEqual(summary["basket"]["live_xag_short_r_status"], "BROKER_LEDGER_VERIFIED")

    def test_mismatched_cash_count_or_time_rejected(self):
        for field, value in [("xag_open_pnl_gbp", 11.0),
                             ("xag_short_count", 1),
                             ("observed_at_utc", "2026-11-18T19:00:00Z")]:
            row, broker = evidence()
            broker[field] = value
            with self.assertRaises(ValueError):
                apply({}, row, broker)

    def test_fail_closed(self):
        row, broker = evidence()
        for name, value in [("current_r", None), ("current_r", float("nan")),
                            ("risk_basis", "CURRENT_STOP"), ("practice_excluded", False)]:
            p = dict(row)
            p[name] = value
            with self.assertRaises(ValueError):
                apply({}, p, broker)
        summary = {"basket": {"live_xag_short_open_r": 2.25, "pnl_gbp": 50.0}}
        mark_unavailable(summary, "missing ledger risk")
        self.assertIsNone(summary["basket"]["live_xag_short_open_r"])
        self.assertEqual(summary["basket"]["pnl_gbp"], 50.0)


if __name__ == "__main__":
    unittest.main()
