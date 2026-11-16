"""Hub should display producer-observed combined high-water, never XAU-only as combined."""
import unittest
from datetime import datetime,timezone,timedelta

import hub_metals_combined_observed_hwm as hwm


def fixture():
    now=datetime.now(timezone.utc)
    ts=now.isoformat()
    past=(now-timedelta(minutes=4)).isoformat()
    producer={
        "ok":True,"scope":hwm.SCOPE,"high_water_status":"OBSERVED_COMBINED_LIVE",
        "observed_at_utc":ts,"high_water_tracking_started_at_utc":past,
        "high_water_at_utc":(now-timedelta(seconds=30)).isoformat(),
        "open_count":4,"xau_count":2,"xag_short_count":2,
        "open_pnl_gbp":30,"high_water_gbp":55,"giveback_gbp":25
    }
    proof={"ok":True,"scope":hwm.SCOPE,"open_count":4,"xau_count":2,
           "xag_short_count":2,"open_pnl_gbp":30.50,"observed_at_utc":ts}
    return producer,proof

class HubCombinedHighWaterTests(unittest.TestCase):
    def test_combined_peak_is_not_xau_only(self):
        producer,proof=fixture()
        verified=hwm.verify(producer,proof)
        self.assertEqual(verified["high_water_gbp"],55)
        self.assertEqual(verified["giveback_gbp"],24.5)
        source={"basket":{"pnl_gbp":30.5,"high_water_gbp":61.79,
                          "high_water_r":9.4,"high_water_at_utc":"old",
                          "giveback_gbp":44.96,"giveback_r":6.86}}
        hwm.apply(source,verified)
        b=source["basket"]
        self.assertEqual(b["high_water_gbp"],55)
        self.assertEqual(b["giveback_gbp"],24.5)
        self.assertIsNone(b["high_water_r"])
        self.assertIsNone(b["giveback_r"])
        self.assertEqual(b["xau_only_high_water_gbp"],61.79)
        self.assertEqual(b["xau_only_giveback_gbp"],44.96)
        self.assertEqual(b["xau_only_high_water_r"],9.4)
        self.assertTrue(source["live_metals_high_water"]["ok"])

    def test_new_hub_peak_is_not_below_current_verified_pnl(self):
        producer,proof=fixture()
        proof["open_pnl_gbp"]=57
        got=hwm.verify(producer,proof)
        self.assertEqual(got["high_water_gbp"],57)
        self.assertEqual(got["giveback_gbp"],0)

    def test_fails_closed_not_back_to_legacy_peak(self):
        s={"basket":{"pnl_gbp":10,"high_water_gbp":67,"high_water_r":10,
                     "giveback_gbp":20,"giveback_r":3}}
        hwm.mark_unavailable(s,"db offline")
        b=s["basket"]
        self.assertIsNone(b["high_water_gbp"])
        self.assertIsNone(b["giveback_gbp"])
        self.assertEqual(b["xau_only_high_water_gbp"],67)
        self.assertEqual(b["pnl_gbp"],10)
        self.assertEqual(b["combined_high_water_status"],"UNAVAILABLE")

    def test_rejects_unverified_stale_or_mismatched_producer(self):
        producer,proof=fixture()
        changes=[("high_water_status","UNAVAILABLE"),("xag_short_count",3),
                 ("giveback_gbp",9),("scope","LIVE_XAU_ONLY"),
                 ("observed_at_utc",(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())]
        for field,bad in changes:
            bad_producer=dict(producer)
            bad_producer[field]=bad
            with self.subTest(field=field),self.assertRaises(ValueError):
                hwm.verify(bad_producer,proof)

if __name__=="__main__":
    unittest.main()
