"""Stop-floor must not treat XAU-only quantified stops as all Metals exposure."""
import unittest
from hub_stop_floor_scope import guard_stop_floor_coverage

def fixture(verified=True,open_count=38,risk_count=16,quantified=None,
            wrong_ids=False,xag_long=False):
    xau_n=min(16,open_count) if isinstance(open_count,int) else 16
    xag_n=max(0,open_count-xau_n) if isinstance(open_count,int) else 22
    ids=[f"x{i}" for i in range(xau_n)]+[f"s{i}" for i in range(xag_n)]
    covered=ids[:risk_count]
    if wrong_ids and covered:
        covered[-1]="other-broker-id"
    quantified=risk_count if quantified is None else quantified
    xau_quant=min(quantified,xau_n)
    xag_quant=max(0,quantified-xau_quant)
    return {
        "sources":{"metals":{"data":{
            "mode":"live","live_risk_ok":True,
            "basket":{"open_trades":open_count,
                      "live_xau_open_count":xau_n,
                      "live_xag_short_open_count":xag_n,
                      "live_positions_status":"BROKER_VERIFIED" if verified else "UNVERIFIED"},
            "live_metals_positions":{"broker_trade_ids":ids},
            "live_risk_broker_trade_ids":covered,
            "live_risk_open_trade_count":risk_count,
            "live_risk_quantified_trade_count":quantified,
            "live_risk_by_instrument":{
                "XAU_USD":{"open_trade_count":xau_n,
                           "quantified_trade_count":xau_quant},
                "XAG_USD":{"open_trade_count":xag_n,
                           "short_trade_count":xag_n-(1 if xag_long else 0),
                           "long_trade_count":1 if xag_long else 0,
                           "quantified_trade_count":xag_quant},
            },
            "live_stop_floor_pnl_gbp":-103.38,
            "live_stop_floor_complete":True,
        }}},
        "portfolio":{"live_stop_floor_pnl_gbp":-103.38,
                     "live_stop_floor_complete":True,
                     "live_risk_complete":True,"warnings":[]},
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

    def test_disagreeing_broker_snapshots_are_not_reported_as_40_of_2_missing_stops(self):
        a = guard_stop_floor_coverage(fixture(open_count=2, risk_count=40, quantified=40))
        p = a["portfolio"]
        self.assertEqual(p["live_stop_floor_scope_status"], "SNAPSHOT_MISMATCH")
        self.assertIsNone(p["live_stop_floor_pnl_gbp"])
        self.assertIn("2 verified positions vs 40 risk-feed trades", p["live_stop_floor_coverage_warning"])
        self.assertNotIn("40/2", p["live_stop_floor_coverage_warning"])
        self.assertIn("NOT proof of missing broker stop orders", p["live_stop_floor_coverage_warning"])

    def test_matching_counts_but_unquantified_xag_does_not_claim_full_floor(self):
        a=guard_stop_floor_coverage(fixture(risk_count=38,quantified=16))
        p=a["portfolio"]
        self.assertIsNone(p["live_stop_floor_pnl_gbp"])
        self.assertEqual(p["live_stop_floor_metals_excluded_count"],22)
        self.assertIn("16/38",a["attention"]["items"][0]["message"])

    def test_matching_count_wrong_broker_id_does_not_claim_full_floor(self):
        a=guard_stop_floor_coverage(fixture(risk_count=38,wrong_ids=True))
        self.assertFalse(a["portfolio"]["live_stop_floor_metals_exact_trade_ids"])
        self.assertIsNone(a["portfolio"]["live_stop_floor_pnl_gbp"])

    def test_xag_long_instead_of_short_does_not_count_as_protection(self):
        a=guard_stop_floor_coverage(fixture(risk_count=38,xag_long=True))
        self.assertFalse(a["portfolio"]["live_stop_floor_metals_instrument_coverage"])
        self.assertIsNone(a["portfolio"]["live_stop_floor_pnl_gbp"])

    def test_unverified_live_counts_fail_closed(self):
        a=guard_stop_floor_coverage(fixture(verified=False,open_count=None))
        self.assertIsNone(a["portfolio"]["live_stop_floor_pnl_gbp"])
        self.assertFalse(a["portfolio"]["live_stop_floor_scope_complete"])

    def test_final_headline_layout_does_not_mislabel_partial_floor(self):
        from pathlib import Path
        content=Path("hub_tile_order_overlay.py").read_text(encoding="utf-8")
        self.assertIn("p.live_stop_floor_scope_complete===false?'Stop-floor incomplete'",content)
        self.assertIn("p.live_stop_floor_pnl_gbp==null",content)

    def test_flat_scope_with_zero_stops_is_complete(self):
        a=guard_stop_floor_coverage(fixture(open_count=0,risk_count=0))
        self.assertTrue(a["portfolio"]["live_stop_floor_scope_complete"])

    def test_projected_nav_replaces_current_upl_before_adding_stop_floor(self):
        from pathlib import Path
        code=Path("hub_tile_order_overlay.py").read_text(encoding="utf-8")
        self.assertIn("Number(p.nav_gbp)-Number(p.unrealised_pnl_gbp)+Number(p.live_stop_floor_pnl_gbp)",code)
        self.assertIn("p.unrealised_pnl_complete!==true",code)
        self.assertIn("p.live_stop_floor_complete!==true",code)
        self.assertIn("Projected NAV at stops:",code)
        self.assertNotIn("Number(p.nav_gbp)+Number(p.live_stop_floor_pnl_gbp)",code)
        # NAV already includes current unrealised P&L.
        self.assertAlmostEqual(5439.06 - 13.57 - 136.43, 5289.06)

    def test_no_metals_does_not_mutate_other_strategies(self):
        a={"sources":{"indices":{"data":{"mode":"live"}}},
           "portfolio":{"live_stop_floor_pnl_gbp":-10}}
        guard_stop_floor_coverage(a)
        self.assertEqual(a["portfolio"]["live_stop_floor_pnl_gbp"],-10)

if __name__=="__main__": unittest.main()
