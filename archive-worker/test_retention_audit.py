import unittest
from retention_audit import assess_page

class RetentionAuditTests(unittest.TestCase):
    def setUp(self):
        self.page={"first_id":1,"last_id":5000,"manifest":"m","sha256":"abc"}
        self.receipt={"row_count":5000}
        self.counts={"source_rows":5000,"outside_hot_window":5000,"protected_rows":0,"closed_cold_rows":5000}
    def test_complete_cold_closed_identical_page(self):
        result=assess_page(self.page,self.receipt,self.counts,True,5001)
        self.assertEqual(result["future_candidate_rows"],5000)
    def test_strict_boundary_preserves_hot_page(self):
        result=assess_page(self.page,self.receipt,self.counts,True,5000)
        self.assertEqual(result["future_candidate_rows"],0)
        self.assertIn("partial_hot_window",result["blockers"])
    def test_mismatch_or_protected_link_blocks(self):
        for counts,match in ((self.counts,False),(dict(self.counts,protected_rows=1),True),
                             (dict(self.counts,source_rows=4999),True)):
            self.assertEqual(assess_page(self.page,self.receipt,counts,match,5001)["future_candidate_rows"],0)
if __name__=="__main__":
    unittest.main()
