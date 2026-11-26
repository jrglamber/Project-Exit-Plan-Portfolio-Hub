import io
import json
import unittest
from datetime import datetime, timezone, timedelta
from archive_health import PREFIX, build_status


class Missing(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self, now):
        self.objects = {}
        self.heads = {}
        for asset in ("indices", "metals"):
            root = f"{PREFIX}/{asset}/2026/11/26/"
            key = root + "sample.dump"
            self.objects[root + "verified.json"] = {"asset": asset, "status": "verified",
                 "object_key": key, "bytes": 42, "sha256": "trusted", "verified_utc": now.isoformat()}
            self.heads[key] = {"ContentLength": 42, "Metadata": {"sha256": "trusted"}}
            self.objects[f"{PREFIX}/restore-proof/{asset}/latest.json"] = {
                "asset": asset, "status": "verified", "verified_utc": now.isoformat(),
                "full_restore_completed": True, "table_counts_match": True, "restored_rows": 123}
        self.objects[f"{PREFIX}/retention/latest.json"] = {"mode": "PLAN_ONLY", "assets": {}}

    def get_paginator(self, name):
        return self

    def paginate(self, Bucket, Prefix):
        return [{"Contents": [{"Key": key} for key in self.objects if key.startswith(Prefix)]}]

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Missing()
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}

    def head_object(self, Bucket, Key):
        return self.heads[Key]


class ArchiveHealthTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 26, tzinfo=timezone.utc)
        self.s3 = FakeS3(self.now)

    def test_healthy(self):
        result = build_status(self.s3, "bucket", self.now)
        self.assertEqual(result["status"], "HEALTHY")
        self.assertEqual(result["retention_mode"], "PLAN_ONLY")
        self.assertEqual(result["assets"]["metals"]["restored_rows"], 123)

    def test_stale_backup_fails_closed(self):
        result = build_status(self.s3, "bucket", self.now + timedelta(hours=37))
        self.assertEqual(result["status"], "REVIEW")
        self.assertFalse(result["assets"]["indices"]["backup_ok"])

    def test_missing_restore_proof(self):
        del self.s3.objects[f"{PREFIX}/restore-proof/metals/latest.json"]
        result = build_status(self.s3, "bucket", self.now)
        self.assertEqual(result["status"], "REVIEW")
        self.assertFalse(result["assets"]["metals"]["restore_ok"])

    def test_metadata_mismatch(self):
        self.s3.heads[f"{PREFIX}/indices/2026/11/26/sample.dump"]["ContentLength"] = 41
        result = build_status(self.s3, "bucket", self.now)
        self.assertEqual(result["status"], "REVIEW")
        self.assertFalse(result["assets"]["indices"]["backup_ok"])


if __name__ == "__main__":
    unittest.main()
