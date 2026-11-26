import unittest
from unittest.mock import patch
from inventory import inventory, CATALOG_SQL

class InventoryTests(unittest.TestCase):
    URL="postgresql://user:password@db.railway.internal:5432/db"

    @patch("inventory.read_json")
    def test_metadata_read_only(self, read):
        read.return_value={"read_only":"on","db_bytes":42,"tables":[{"schema":"public","name":"x"}]}
        result=inventory("indices",self.URL)
        self.assertEqual(result["db_bytes"],42)
        self.assertFalse(result["source_deleted"])
        env,sql=read.call_args.args
        self.assertIn("default_transaction_read_only=on",env["PGOPTIONS"])
        self.assertIn("lock_timeout=1000",env["PGOPTIONS"])
        self.assertEqual(sql,CATALOG_SQL)

    @patch("inventory.read_json")
    def test_reject_writable_connection(self,read):
        read.return_value={"read_only":"off","tables":[{}]}
        with self.assertRaises(RuntimeError):
            inventory("indices",self.URL)

    @patch("inventory.read_json")
    def test_housekeeping_uses_indexed_id_bounds(self,read):
        read.side_effect=[{"read_only":"on","tables":[{"schema":"public","name":"metals_xau_live_manager_reviews",
             "indexes":[{"primary":True,"valid":True,"definition":"CREATE UNIQUE INDEX pkey ON public.metals_xau_live_manager_reviews USING btree (id)"}]}]},
             {"oldest_id":1,"latest_id":1000}]
        data=inventory("metals",self.URL)
        self.assertEqual(data["housekeeping_id_bounds"]["latest_id"],1000)
        self.assertEqual(read.call_count,2)

if __name__=="__main__":
    unittest.main()
