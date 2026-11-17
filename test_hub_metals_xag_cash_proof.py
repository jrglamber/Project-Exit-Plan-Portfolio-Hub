import unittest
from datetime import datetime,timezone,timedelta
from hub_metals_xag_cash_proof import combine_xau_xag

NOW=datetime(2026,11,17,21,4,tzinfo=timezone.utc)

def payload():
    return {"reconciled":False,"xag_short_net_gbp_verified":True,
            "xag_short_verified_realised":{
                "ok":True,"scope":"XAG_SHORT_BROKER_NET_CASH_GBP",
                "time_utc":"2026-11-17T21:04:00Z",
                "broker_trade_count":77,"broker_closed_trade_count":56,
                "realised_today_gbp":1.2,"realised_week_gbp":12.8748,
                "realised_month_gbp":-32.1221,
                "realised_all_time_gbp":-32.1221,
            }}

def view():
    return {"accounting":{
        "realised_today_gbp":0.0,
        "realised_week_gbp":20.82,
        "realised_month_gbp":39.37,
        "realised_all_time_gbp":250.43,
    },"basket":{"pnl_gbp":20.38,"open_trades":36}}

class HubXagCashTests(unittest.TestCase):
    def test_combines_once_and_preserves_xau_totals(self):
        v=view()
        result=combine_xau_xag(v,payload(),now=NOW)
        a=result["accounting"]
        self.assertEqual(a["realised_today_gbp"],1.2)
        self.assertAlmostEqual(a["realised_week_gbp"],33.6948)
        self.assertAlmostEqual(a["realised_month_gbp"],7.2479)
        self.assertAlmostEqual(a["realised_all_time_gbp"],218.3079)
        self.assertEqual(a["xau_only_realised_all_time_gbp"],250.43)
        self.assertAlmostEqual(a["xag_short_realised_all_time_gbp"],-32.1221)
        self.assertEqual(a["realised_scope"],"XAU_LEGACY_PLUS_XAG_SHORT_NET_BROKER_VERIFIED")
        self.assertEqual(a["lane_reporting_status"],"available")
        self.assertFalse(a["reconciled"])
        self.assertEqual(result["basket"]["open_trades"],36)
        self.assertEqual(result["basket"]["pnl_gbp"],20.38)

    def test_invalid_proof_leaves_xau_untouched(self):
        for invalid in ({"reconciled":False},
                        {**payload(),"reconciled":True},
                        {"reconciled":False,"xag_short_net_gbp_verified":False},
                        {"reconciled":False,"xag_short_net_gbp_verified":True,
                         "xag_short_verified_realised":{"ok":False}}):
            v=view()
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                combine_xau_xag(v,invalid,now=NOW)
            self.assertEqual(v["accounting"]["realised_week_gbp"],20.82)

    def test_stale_xag_proof_rejected(self):
        with self.assertRaises(ValueError):
            combine_xau_xag(view(),payload(),now=NOW+timedelta(minutes=20))

    def test_missing_one_accounting_field_cannot_report_partial_total(self):
        v=view();v["accounting"]["realised_month_gbp"]=None
        with self.assertRaises(ValueError):
            combine_xau_xag(v,payload(),now=NOW)
        self.assertEqual(v["accounting"]["realised_week_gbp"],20.82)

    def test_missing_xau_today_does_not_block_verified_week_month_alltime(self):
        v=view()
        v["accounting"]["realised_today_gbp"]=None
        result=combine_xau_xag(v,payload(),now=NOW)
        a=result["accounting"]
        self.assertIsNone(a["realised_today_gbp"])
        self.assertEqual(a["xag_short_realised_today_gbp"],1.2)
        self.assertAlmostEqual(a["realised_week_gbp"],33.6948)
        self.assertAlmostEqual(a["realised_month_gbp"],7.2479)
        self.assertTrue(a["xag_short_broker_net_verified"])

    def test_second_application_is_blocked(self):
        v=view()
        combine_xau_xag(v,payload(),now=NOW)
        with self.assertRaises(ValueError):
            combine_xau_xag(v,payload(),now=NOW)
        self.assertAlmostEqual(v["accounting"]["realised_all_time_gbp"],218.3079)

    def test_future_canonical_combined_source_fails_closed(self):
        v=view()
        v["accounting"]["realised_scope"]="XAU_LEGACY_PLUS_XAG_SHORT_NET_BROKER_VERIFIED"
        with self.assertRaises(ValueError):
            combine_xau_xag(v,payload(),now=NOW)

    def test_dashboard_copy_explicitly_labels_net_broker_scope(self):
        from pathlib import Path
        s=Path("app.py").read_text(encoding="utf-8")
        self.assertIn("combine_xau_xag(validated, lane_payload)",s)
        self.assertIn("XAU + XAG SHORT net GBP verified",s)
        self.assertIn("broker-verified XAG Short net cash",s)

if __name__=="__main__":
    unittest.main()
