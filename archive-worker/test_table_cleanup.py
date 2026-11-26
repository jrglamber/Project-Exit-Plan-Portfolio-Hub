import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from table_cleanup import cleanup_transaction, Ineligible, clean

class TestCleanup(unittest.TestCase):
    def test_disabled_does_not_touch_source(self):
        with patch.dict(os.environ, {"XAU_VERIFIED_CLEANUP_ENABLED":"0"}), patch("table_cleanup.connection_env", side_effect=AssertionError):
            self.assertIsNone(clean(None,None,None))

    def test_requires_full_page(self):
        with self.assertRaisesRegex(Ineligible,"not_a_full_nonempty_page"):
            cleanup_transaction(None,{"first_id_requested":1,"last_id_requested":42,"row_count":42},None,None)

    def test_corrupt_raw_rejected_before_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"page.csv"
            p.write_bytes(b"wrong")
            with self.assertRaisesRegex(Ineligible,"local_archive_mismatch"):
                cleanup_transaction(None,{"first_id_requested":1,"last_id_requested":5000,"row_count":5000,
                    "csv_bytes":5,"csv_sha256":hashlib.sha256(b"right").hexdigest()},p,Path(d))

if __name__=="__main__":
    unittest.main()
