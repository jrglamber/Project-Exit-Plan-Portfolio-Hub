"""No phantom basket flatten/reopen events from unavailable broker telemetry."""
import unittest
from pathlib import Path

from hub_basket_activity_guard import position_observation, transition_events


def view(count=45, status="BROKER_VERIFIED", ok=True, stale=False, ids=True):
    basket={"open_trades":count,"live_positions_status":status}
    xau = count-21 if isinstance(count,int) and count>=21 else 0
    xag = count-xau if isinstance(count,int) else 0
    proof={
        "ok":True, "open_count":count,
        "xau_count":xau,"xag_short_count":xag,
        "broker_trade_ids":list(range(count)) if isinstance(count,int) and ids else None,
    }
    return {"ok":ok,"stale":stale,
            "data":{"mode":"live","basket":basket,
                    "live_metals_positions":proof}}


def state(v,previous=None):
    count,trusted=position_observation("metals",v)
    current={"open_trades":count,"position_verified":trusted}
    events=transition_events("metals",previous,current)
    return current,events


class BrokerBasketActivityGuardTests(unittest.TestCase):
    def test_actual_incident_45_unknown_one_hour_then_42(self):
        prev,events=state(view(45))
        self.assertEqual(events,[])
        unknown,events=state(view(count=None),prev)
        self.assertIsNone(unknown["open_trades"])
        self.assertEqual([e[0] for e in events],["position_feed_unavailable"])
        for _ in range(45):
            unknown,events=state(view(count=None),unknown)
            self.assertEqual(events,[])
        restored,events=state(view(42),unknown)
        self.assertEqual([e[0] for e in events],["position_feed_recovered"])
        self.assertEqual(restored["open_trades"],42)
        self.assertFalse(any(e[0] in {"basket_flattened","basket_opened"} for e in events))

    def test_zero_from_bad_broker_snapshot_cannot_be_flatten(self):
        prior,_=state(view(21))
        for poor in (
            view(0,status="UNVERIFIED"),
            view(0,ok=False),
            view(0,stale=True),
            view(0,ids=False),
        ):
            with self.subTest(snapshot=poor):
                cur,events=state(poor,prior)
                self.assertFalse(cur["position_verified"])
                self.assertEqual(events[0][0],"position_feed_unavailable")
                self.assertNotIn("basket_flattened",[e[0] for e in events])

    def test_genuine_verified_flatten_requires_two_independent_zeros(self):
        prior,_=state(view(12))
        first,events=state(view(0),prior)
        self.assertEqual(events,[])
        self.assertEqual(first["pending_flatten_from"],12)
        second,events=state(view(0),first)
        self.assertEqual([e[0] for e in events],["basket_flattened"])
        third,events=state(view(0),second)
        self.assertEqual(events,[])

    def test_single_zero_rebound_not_reopen(self):
        prior,_=state(view(12))
        zero,events=state(view(0),prior)
        positive,events=state(view(12),zero)
        self.assertEqual(events,[])
        self.assertIsNone(positive["pending_flatten_from"])

    def test_true_flat_to_positive_is_observed_broker_open(self):
        prev,_=state(view(0))
        cur,events=state(view(3),prev)
        self.assertEqual([e[0] for e in events],["basket_opened"])
        self.assertIn("0 to 3",events[0][1])

    def test_partial_reduction_is_not_called_flatten(self):
        prev,_=state(view(45))
        cur,events=state(view(30),prev)
        self.assertEqual([e[0] for e in events],["basket_reduced"])
        self.assertIn("45",events[0][1])
        self.assertIn("30",events[0][1])

    def test_malformed_trade_proof_rejected(self):
        for invalid in (view(3,ids=False),view(None)):
            count,trusted=position_observation("metals",invalid)
            self.assertIsNone(count)
            self.assertFalse(trusted)
        x=view(3)
        x["data"]["live_metals_positions"]["broker_trade_ids"]=["same"]*3
        self.assertEqual(position_observation("metals",x),(None,False))

    def test_other_strategies_require_nonmissing_fresh_counts(self):
        for strategy in ("indices","bco"):
            snapshot={"ok":True,"stale":False,"data":{
                "mode":"live","basket":{"open_trades":0}}}
            self.assertEqual(position_observation(strategy,snapshot),(0,True))
            snapshot["data"]["basket"]["open_trades"]=None
            self.assertEqual(position_observation(strategy,snapshot),(None,False))

    def test_hub_wiring_and_realised_revision_label(self):
        content=Path("app.py").read_text(encoding="utf-8")
        self.assertIn("position_observation(key, view)",content)
        self.assertIn("transition_events(key, previous, current)",content)
        self.assertIn("Reported realised P&L revised",content)
        self.assertNotIn("Basket opened - {open_now} trades",content)
        self.assertNotIn("elif prev_open > 0 and open_now == 0",content)

if __name__=="__main__":
    unittest.main()
