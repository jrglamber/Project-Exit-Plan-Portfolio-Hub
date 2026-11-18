"""Independent Metals broker risk/position comparison regression tests."""
import unittest
from hub_metals_snapshot_coherence import population_consistent


def fixture():
    return ({
        "ok": True, "count": 3, "xau_count": 2, "xag_short_count": 1,
        "trades": [{"broker_trade_id": "12"}, {"broker_trade_id": "10"},
                   {"broker_trade_id": "11"}],
    }, {
        "ok": True, "open_trade_count": 3, "broker_trade_ids": ["10", "11", "12"],
        "by_instrument": {"XAU_USD": {"open_trade_count": 2},
                          "XAG_USD": {"open_trade_count": 1}},
    })


class MetalsPopulationTests(unittest.TestCase):
    def test_same_broker_ids_different_order_matches(self):
        p, r = fixture()
        self.assertIs(population_consistent(p, r), True)

    def test_wrong_count_is_inconsistent_not_missing_stops(self):
        p, r = fixture()
        p["trades"] = p["trades"][:2]
        p["count"] = 2
        p["xau_count"] = 1
        self.assertIs(population_consistent(p, r), False)

    def test_wrong_trade_ids_rejected(self):
        p, r = fixture()
        p["trades"][0]["broker_trade_id"] = "99"
        self.assertIs(population_consistent(p, r), False)

    def test_risk_endpoint_unavailable_does_not_erase_independent_position_proof(self):
        p, r = fixture()
        r["ok"] = False
        self.assertIsNone(population_consistent(p, r))

    def test_legacy_xau_only_risk_is_not_comparable(self):
        p, r = fixture()
        r["open_trade_count"] = 2
        r["by_instrument"].pop("XAG_USD")
        self.assertIsNone(population_consistent(p, r))

    def test_duplicate_ids_are_rejected(self):
        p, r = fixture()
        r["broker_trade_ids"][2] = "11"
        self.assertIs(population_consistent(p, r), False)


if __name__ == "__main__":
    unittest.main()
