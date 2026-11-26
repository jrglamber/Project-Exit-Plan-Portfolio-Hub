import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from table_archive import plan_pages,validate_csv,TABLE,PAGE_IDS,archive_reviews,PREFIX
from test_incremental import Bucket
from incremental import manifest_read

class TableArchiveTests(unittest.TestCase):
    def checkpoint(self,next_id):
        return {"table":TABLE,"status":"verified","source_deleted":False,"next_id":next_id}

    def test_bounded_oldest_intervals(self):
        pages=plan_pages(100,1_000_000,None)
        self.assertEqual(len(pages),8)
        self.assertEqual(pages[0],(100,5099))
        self.assertTrue(all(b-a+1<=PAGE_IDS for a,b in pages))
        self.assertEqual(plan_pages(100,1_000_000,self.checkpoint(40100))[0],(40100,45099))

    def test_source_gap_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError,"source history"):
            plan_pages(101,1_000_000,self.checkpoint(100))

    def test_hot_rows_never_archived(self):
        self.assertEqual(plan_pages(10,750009,None),[])
        self.assertEqual(plan_pages(10,750020,self.checkpoint(10)),[])

    @patch("table_archive.dump_page")
    @patch("table_archive.read_json")
    def test_verified_receipt_then_checkpoint_and_corrupt_prior_blocks(self,read,dump):
        class Store(Bucket):
            def upload_file(self,filename,Bucket,Key,ExtraArgs=None):
                self.objects[Key]=Path(filename).read_bytes()
        s3=Store()
        read.side_effect=lambda env,sql: ({"read_only":"on","oldest":100,"latest":755099}
            if "min(id)" in sql else [{"name":"id","type":"bigint"},{"name":"raw_json","type":"text"}])
        def fake_dump(env,start,end,path):
            path.write_text(f"id,raw_json\n{start},one\n{end},two\n")
        dump.side_effect=fake_dump
        url="postgresql://u:p@db.railway.internal:5432/db"
        archive_reviews(s3,"b",url)
        chk=manifest_read(s3,"b",PREFIX+"/checkpoint.json")
        self.assertEqual(chk["next_id"],5100)
        self.assertEqual(chk["total_verified_rows"],2)
        prior=manifest_read(s3,"b",chk["last_manifest"])
        s3.objects[prior["object_key"]]=b"damaged"
        with self.assertRaisesRegex(RuntimeError,"integrity failure"):
            archive_reviews(s3,"b",url)
        self.assertEqual(manifest_read(s3,"b",PREFIX+"/checkpoint.json"),chk)

    def test_csv_multiline_null_and_id(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"rows.csv"
            with open(path,"w",newline="",encoding="utf-8") as f:
                writer=csv.writer(f)
                writer.writerows([["id","raw_json"],[10,"a\nb"],[12,r"\N"]])
            columns=[{"name":"id"},{"name":"raw_json"}]
            self.assertEqual(validate_csv(path,columns,10,20),(2,10,12))
            with open(path,"a",encoding="utf-8") as f:
                f.write("12,duplicate\n")
            with self.assertRaises(RuntimeError):
                validate_csv(path,columns,10,20)

if __name__=="__main__":
    unittest.main()
