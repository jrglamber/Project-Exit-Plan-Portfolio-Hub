"""Never claim 45 -> UNKNOWN -> 42 was a full basket liquidation/reentry."""
from pathlib import Path
import unittest
from hub_basket_activity_proof import verified_position_count,position_activity

def snapshot(count=45,verified=True):
    if count is None or not verified:
        return {"ok":True,"stale":False},{
            "mode":"live","basket":{
                "open_trades":count,
                "live_positions_status":"UNVERIFIED",
            },
            "live_metals_positions":{"ok":False}}
    xau=min(count,20)
    xag=count-xau
    return {"ok":True,"stale":False},{
        "mode":"live",
        "basket":{"open_trades":count,
                  "live_positions_status":"BROKER_VERIFIED"},
        "live_metals_positions":{
            "ok":True,"open_count":count,"xau_count":xau,
            "xag_short_count":xag},
    }

class BasketActivityProofTests(unittest.TestCase):
    def count(self,value,verified=True):
        view,data=snapshot(value,verified)
        return verified_position_count("metals",view,data)

    def test_missing_broker_count_is_unknown_not_zero(self):
        self.assertIsNone(self.count(None,False))
        view,data=snapshot(42,True)
        data["basket"]["live_positions_status"]="UNVERIFIED"
        self.assertIsNone(verified_position_count("metals",view,data))
        view["stale"]=True
        self.assertIsNone(verified_position_count("metals",view,data))
        view,data=snapshot(0,True)
        self.assertEqual(verified_position_count("metals",view,data),0)

    def test_0519_0520_0600_exact_production_pattern(self):
        initial=self.count(45)
        self.assertEqual(initial,45)
        gap=self.count(None,False)
        self.assertIsNone(gap)
        lost=position_activity({"open_trades":45,"last_verified_open_trades":45},gap)
        self.assertEqual(lost[0],"broker_positions_unavailable")
        self.assertNotIn("flatten",lost[0])
        state_after_gap={"open_trades":None,"last_verified_open_trades":45}
        for _ in range(39):
            self.assertIsNone(position_activity(state_after_gap,None))
        recovered=position_activity(state_after_gap,self.count(42))
        self.assertEqual(recovered[0],"broker_positions_reverified")
        self.assertIn("45 -> 42",recovered[1])
        self.assertNotEqual(recovered[0],"basket_opened")
        self.assertNotEqual(recovered[0],"basket_flattened")

    def test_flatten_requires_explicit_verified_zero(self):
        self.assertEqual(position_activity({"open_trades":45},self.count(0))[0],
                         "basket_flattened")
        self.assertEqual(position_activity({"open_trades":0},self.count(42))[0],
                         "basket_opened")

    def test_unverified_position_count_never_creates_flatten_or_open(self):
        self.assertEqual(position_activity({"open_trades":45},None)[0],
                         "broker_positions_unavailable")
        event=position_activity({"open_trades":None,"last_verified_open_trades":45},42)
        self.assertEqual(event[0],"broker_positions_reverified")

    def test_repeated_good_snapshots_do_not_spam_events(self):
        self.assertIsNone(position_activity({"open_trades":42},42))
        self.assertIsNone(position_activity({"open_trades":45},44))
        self.assertEqual(position_activity({"open_trades":45},42)[0],"basket_reduced")

    def test_indices_and_bco_counts_not_hidden(self):
        for key in ("indices","bco"):
            self.assertEqual(verified_position_count(key,*snapshot(0)),0)
            self.assertEqual(verified_position_count(key,*snapshot(12)),12)

    def test_integrated_hub_activity_uses_only_proven_counts(self):
        s=Path("app.py").read_text(encoding="utf-8")
        self.assertIn('"open_trades": verified_position_count(key, view, data)',s)
        self.assertIn('activity = position_activity(previous, open_now)',s)
        self.assertIn('"last_verified_open_trades"',s)
        self.assertNotIn('"open_trades": safe_int(basket.get("open_trades")) or 0',s)
        self.assertIn("snapshot difference; not an individual trade",s)

if __name__=="__main__":
    unittest.main()
