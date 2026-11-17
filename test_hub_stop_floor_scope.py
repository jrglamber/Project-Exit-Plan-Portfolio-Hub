"""Stop-floor must not treat XAU-only quantified stops as all Metals exposure."""
import unittest
from hub_stop_floor_scope import guard_stop_floor_coverage

def fixture(verified=True,open_count=38,risk_count=16):
    return {
        "sources":{"metals":{"data":{
            "mode":"live",
            "basket":{"open_trades":open_count,
                      "live_positions_status":"BROKER_VERIFIED" if verified else "UNVERIFIED"},
            "live_risk_open_trade_count":risk_count,
            "live_stop_floor_pnl_gbp":-103.38,
            "live_stop_floor_complete":True,
        }}},
        "portfolio":{"live_stop_floor_pnl_gbp":-103.38,
                     "live_stop_floor_complete":True,
                     "live_risk_complete":True,
                     "warnings":[]},
        "attention":{"items":[]},
    }

class StopFloorScopeTests(unittest.TestCase):
    def test_16_quantified_stop_trades_cannot_claim_38_live_metals_trades(self):
        a=guard_stop_floor_coverage(fixture())
        p=a["portfolio"]
        self.assertEqual(p["live_stop_floor_scope_status"],"INCOMPLETE")
        self.assertFalse(p["live_stop_floor_complete"])
        self.assertIsNone(p["live_stop_floor_pnl_gbp"])
        self.assertEqual(p["live_stop_floor_known_partial_gbp"],-103.38)
        self.assertEqual(p["live_stop_floor_metals_excluded_count"],22)
        self.assertFalse(p["live_risk_complete"])
        self.assertEqual(a["sources"]["metals"]["data"]["live_stop_floor_pnl_gbp"],None)
        self.assertEqual(len(a["attention"]["items"]),1)
        self.assertIn("16/38",a["attention"]["items"][0]["message"])

    def test_complete_stops_preserve_value(self):
        a=guard_stop_floor_coverage(fixture(risk_count=38))
        p=a["portfolio"]
        self.assertEqual(p["live_stop_floor_scope_status"],"COMPLETE")
        self.assertEqual(p["live_stop_floor_pnl_gbp"],-103.38)
        self.assertTrue(p["live_stop_floor_complete"])
        self.assertFalse(a["attention"]["items"])

    def test_unverified_live_counts_fail_closed(self):
        a=guard_stop_floor_coverage(fixture(verified=False,open_count=None))
        self.assertIsNone(a["portfolio"]["live_stop_floor_pnl_gbp"])
        self.assertFalse(a["portfolio"]["live_stop_floor_scope_complete"])

    def test_flat_scope_with_zero_stops_is_complete(self):
        a=guard_stop_floor_coverage(fixture(open_count=0,risk_count=0))
        self.assertTrue(a["portfolio"]["live_stop_floor_scope_complete"])

    def test_no_metals_does_not_mutate_other_strategies(self):
        a={"sources":{"indices":{"data":{"mode":"live"}}},
           "portfolio":{"live_stop_floor_pnl_gbp":-10}}
        guard_stop_floor_coverage(a)
        self.assertEqual(a["portfolio"]["live_stop_floor_pnl_gbp"],-10)

if __name__=="__main__": unittest.main()
