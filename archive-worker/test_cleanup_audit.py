import gzip
import hashlib
import unittest
from datetime import date,datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch

from cleanup_audit import inspect, scan_dates, intent_records, STATE_KEY, CLEANUP_PREFIX, AUDIT_START
from table_archive import PREFIX,TABLE
from table_restore import canonical_sha,PROOF_PREFIX
from incremental import manifest_write,manifest_read
from bucket_inventory import inventory
from test_incremental import Bucket

NOW=datetime(2026,11,26,16,30,tzinfo=timezone.utc)
URL="postgresql://u:p@db.railway.internal:5432/db"

class Store(Bucket):
    def download_file(self,bucket,key,filename):
        Path(filename).write_bytes(self.objects[key])
    def get_paginator(self,name):
        class Pages:
            def __init__(self,store): self.store=store
            def paginate(self,Bucket,Prefix):
                yield {"Contents":[{"Key":k,"Size":len(v)} for k,v in self.store.objects.items() if k.startswith(Prefix)]}
        return Pages(self)

class CleanupAuditTests(unittest.TestCase):
    def setUp(self):
        self.s3=Store()
        raw=b"id,link_id,details\n1,7,a\n5000,7,b\n"
        packed=gzip.compress(raw)
        sha=hashlib.sha256(packed).hexdigest()
        stem=f"{1:020d}-{5000:020d}-{sha[:20]}"
        self.object=f"{PREFIX}/pages/{stem}.csv.gz"
        self.manifest=f"{PREFIX}/manifests/{stem}.json"
        self.s3.objects[self.object]=packed
        self.receipt={"sha256":sha,"csv_sha256":hashlib.sha256(raw).hexdigest(),"status":"verified",
            "table":TABLE,"asset":"metals","format":"postgresql-copy-csv-gzip-v1","source_deleted":False,
            "copy_null":r"\N","first_id_requested":1,"last_id_requested":5000,"first_present_id":1,
            "last_present_id":5000,"row_count":2,"bytes":len(packed),"csv_bytes":len(raw),
            "columns":[{"name":"id","type":"bigint"},{"name":"link_id","type":"bigint"},{"name":"details","type":"text"}],
            "object_key":self.object}
        manifest_write(self.s3,"b",self.manifest,self.receipt)
        proof_key=PROOF_PREFIX+"/batches/test.json"
        proof={"status":"verified","table":TABLE,"first_archived_id":1,"batch_first_id":1,
            "next_id":5001,"total_restored_rows":2,"full_byte_roundtrip":True,
            "production_connections":False,"source_deleted":False,"proof_key":proof_key,
            "pages":[{"manifest":self.manifest,"manifest_sha256":canonical_sha(self.receipt),
                      "sha256":sha,"csv_sha256":self.receipt["csv_sha256"],"rows":2,"first_id":1,"last_id":5000}]}
        manifest_write(self.s3,"b",proof_key,proof)
        manifest_write(self.s3,"b",PROOF_PREFIX+"/latest.json",proof)
        manifest_write(self.s3,"b",PREFIX+"/checkpoint.json",{"status":"verified","table":TABLE,"source_deleted":False,
            "first_archived_id":1,"next_id":5001,"total_verified_rows":2,"total_verified_bytes":len(packed)})
        self.base=f"{CLEANUP_PREFIX}/{NOW:%Y/%m/%d}/{1:020d}-{5000:020d}-"+("a"*32)
        self.intent={"status":"intent_only","table":TABLE,"archive_manifest":self.manifest,
            "archive_object":self.object,"restore_proof":proof_key,"archive_sha256":sha,
            "csv_sha256":self.receipt["csv_sha256"],"first_id":1,"last_id":5000,"expected_rows":2,
            "created_utc":(NOW-timedelta(hours=1)).isoformat(),"archive_retained":True}
        self.completion=dict(self.intent,status="committed",rows_deleted=2,post_delete_target_rows=0,
            byte_match=True,latest_id_at_check=1005001,exclusive_cutoff_id=5001,closed_links_locked=1,
            committed_utc=(NOW-timedelta(minutes=59)).isoformat())

    def save(self,complete=True):
        manifest_write(self.s3,"b",self.base+"/intent.json",self.intent)
        if complete:
            manifest_write(self.s3,"b",self.base+"/outcome.json",self.completion)

    def run_audit(self,rows=0):
        with patch("cleanup_audit.read_json",return_value={"read_only":"on","oldest":5001,"latest":1005100}), \
                patch("cleanup_audit.current_rows",return_value=rows):
            return inspect(self.s3,"b",URL,NOW)

    def test_committed_page_verified_and_checkpoint(self):
        self.save()
        r=self.run_audit()
        self.assertEqual(r["status"],"verified")
        self.assertEqual(r["committed_rows_verified"],2)
        self.assertEqual(manifest_read(self.s3,"b",STATE_KEY)["next_day"],NOW.date().isoformat())

    def test_committed_source_reappears_review_no_advance(self):
        self.save()
        r=self.run_audit(1)
        self.assertEqual(r["status"],"review")
        self.assertIsNone(manifest_read(self.s3,"b",STATE_KEY))

    def test_corrupt_archive_fails_closed(self):
        self.save()
        self.s3.objects[self.object]=b"corrupt"
        with self.assertRaisesRegex(RuntimeError,"checksum mismatch"):
            self.run_audit()
        self.assertIsNone(manifest_read(self.s3,"b",STATE_KEY))

    def test_invalid_outcome_cutoff(self):
        self.completion["exclusive_cutoff_id"]=5000
        self.save()
        with self.assertRaisesRegex(RuntimeError,"invalid committed"):
            list(intent_records(self.s3,"b",NOW.date()))

    def test_stale_unresolved_absent_requires_review(self):
        self.save(complete=False)
        r=self.run_audit()
        self.assertEqual(r["status"],"review")
        self.assertEqual(r["committed_rows_verified"],0)

    def test_no_receipts_is_normal(self):
        r=self.run_audit()
        self.assertEqual(r["status"],"verified")
        self.assertEqual(r["committed_rows_verified"],0)

    def test_bounded_missed_day_catchup(self):
        dates=scan_dates(None,NOW+timedelta(days=20))
        self.assertEqual(len(dates),7)
        self.assertEqual(dates[0],AUDIT_START)
        self.assertEqual(scan_dates({"next_day":"2026-11-28"},NOW+timedelta(days=4))[0],date(2026,11,28))

    def test_bucket_totals_read_only(self):
        self.s3.objects["project-exit-plan/v1/metals/example.dump"]=b"abc"
        total=sum(len(v) for v in self.s3.objects.values())
        r=inventory(self.s3,"b")
        self.assertEqual(r["bytes"],total)
        self.assertEqual(r["status"],"verified")
        self.assertEqual(r["categories"]["full_backup_and_proofs"]["bytes"],3)

if __name__=="__main__":
    unittest.main()
