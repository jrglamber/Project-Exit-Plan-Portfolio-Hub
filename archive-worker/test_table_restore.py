import unittest
from table_restore import create_sql,select_keys,validate_receipt,PREFIX,TABLE

class RestoreTests(unittest.TestCase):
    def key(self,lo,hi,sha="a"*20):
        return f"{PREFIX}/manifests/{lo:020d}-{hi:020d}-{sha}.json"
    def test_contiguous_and_bounded(self):
        keys=[self.key(1,5000),self.key(5001,10000),self.key(10001,15000)]
        self.assertEqual(len(select_keys(keys,1,15001,max_pages=2)),2)
        self.assertEqual(len(select_keys(keys,5001,10001)),1)
    def test_gap_overlap_ambiguous(self):
        cases=[[self.key(1,5000),self.key(5002,10000)],
               [self.key(1,5000),self.key(4000,8500)],
               [self.key(1,5000),self.key(1,5000,"b"*20)]]
        for keys in cases:
            with self.assertRaises(RuntimeError):
                select_keys(keys,1,10001)
    def test_reject_untrusted_schema(self):
        self.assertIn("PRIMARY KEY",create_sql([{"name":"id","type":"bigint"},{"name":"amount","type":"double precision"}]))
        for columns in [[{"name":"id","type":"bigint); DROP TABLE x;--"}],
                        [{"name":"id","type":"text"}],
                        [{"name":"id","type":"bigint"},{"name":"x;--","type":"text"}]]:
            with self.assertRaises(RuntimeError):
                create_sql(columns)
    def test_manifest_identity(self):
        lo,hi=1,5000
        manifest={"sha256":"a"*64,"csv_sha256":"b"*64,"status":"verified","table":TABLE,"asset":"metals",
                  "format":"postgresql-copy-csv-gzip-v1","source_deleted":False,"copy_null":r"\N",
                  "first_id_requested":lo,"last_id_requested":hi,"row_count":5000,"bytes":100,
                  "csv_bytes":1000,"columns":[{"name":"id","type":"bigint"}],
                  "object_key":f"{PREFIX}/pages/{lo:020d}-{hi:020d}-{'a'*20}.csv.gz"}
        self.assertEqual(validate_receipt(manifest,lo,hi,self.key(lo,hi)),manifest)
        manifest["object_key"]="some-other-key"
        with self.assertRaises(RuntimeError):
            validate_receipt(manifest,lo,hi,self.key(lo,hi))

if __name__=="__main__":
    unittest.main()
