import unittest
from financial_review import financial_review_payload


class MetalsLaneVisibilityTests(unittest.TestCase):
    def test_missing_lane_data_is_unknown_not_zero(self):
        snapshot = {'sources': {'metals': {'data': {'mode': 'live', 'accounting': {'realised_week_gbp': 5}}}}}
        result = financial_review_payload(snapshot)
        self.assertIsNone(result['metals_live_lanes']['xau_long']['realised_week_gbp'])
        self.assertEqual(result['metals_live_lanes']['xag_short']['reporting_status'], 'awaiting_reconciliation')
        self.assertFalse(result['metals_lane_reconciled'])

    def test_unverified_producer_values_remain_hidden(self):
        record = dict(realised_week_gbp=9, realised_month_gbp=9,
                      realised_all_time_gbp=9, unrealised_gbp=0, open_trades=1)
        snapshot = {'sources': {'metals': {'data': {'mode':'live', 'accounting': {
            'live_lanes': {'xau_long': record}, 'reconciled': False,
            'coverage_verified': False}}}}}
        result = financial_review_payload(snapshot)
        self.assertIsNone(result['metals_live_lanes']['xau_long']['realised_week_gbp'])
        self.assertEqual(result['metals_live_lanes']['xau_long']['reporting_status'], 'awaiting_reconciliation')

    def test_three_lanes_reconcile_with_producer_verified_scope(self):
        def lane(week):
            return dict(realised_week_gbp=week, realised_month_gbp=week,
                        realised_all_time_gbp=week, unrealised_gbp=0, open_trades=0)
        data = {'mode': 'live', 'accounting': {
            'realised_week_gbp': 6, 'realised_month_gbp': 6,
            'realised_all_time_gbp': 6, 'reconciled': True, 'coverage_verified': True,
            'live_lanes': {'xau_long': lane(1), 'xau_short': lane(2), 'xag_short': lane(3)}},
            'basket': {'pnl_gbp': 0}}
        # Legacy XAU-only summary is not the three-lane comparison baseline.
        data['accounting']['realised_week_gbp'] = 3
        data['accounting']['realised_month_gbp'] = 3
        data['accounting']['realised_all_time_gbp'] = 3
        result = financial_review_payload({'sources': {'metals': {'data': data}}})
        self.assertTrue(result['metals_lane_reconciled'])
        self.assertEqual(result['metals_live_lanes']['xag_short']['realised_week_gbp'], 3)


if __name__ == '__main__':
    unittest.main()
