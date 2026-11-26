import io
import json
import unittest
from datetime import datetime, timezone, timedelta
from archive_health import PREFIX, build_status, install_archive_health_route


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


class ArchiveHealthRouteTests(unittest.TestCase):
    def test_route_precedes_catchall_mount_and_preserves_other_routes(self):
        from fastapi import FastAPI
        import asyncio
        from unittest.mock import patch
        outer, inner = FastAPI(), FastAPI()

        @inner.get("/api/other")
        def other():
            return {"existing": True}

        outer.mount("/", inner)
        def get(path):
            # Exercise the actual ASGI router without an extra test dependency.
            messages = []
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(message):
                messages.append(message)
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                     "http_version": "1.1", "method": "GET", "scheme": "http",
                     "path": path, "raw_path": path.encode(), "root_path": "",
                     "query_string": b"", "headers": [],
                     "client": ("127.0.0.1", 1234), "server": ("test", 80)}
            asyncio.run(outer(scope, receive, send))
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            body = b"".join(m["body"] for m in messages if m["type"] == "http.response.body")
            return status, json.loads(body)
        self.assertEqual(get("/api/archive-health")[0], 404)
        def health():
            return {"status": "HEALTHY", "execution_authority": False}
        with patch("archive_health.archive_health", health):
            old_order = list(outer.routes)
            route = install_archive_health_route(outer)
            self.assertIs(outer.routes[0], route)
            self.assertEqual(outer.routes[1:], old_order)
            self.assertEqual(get("/api/archive-health"), (200, health()))
            self.assertEqual(get("/api/other"), (200, {"existing": True}))
            self.assertIs(install_archive_health_route(outer), route)
            self.assertEqual(outer.routes[1:], old_order)

if __name__ == "__main__":
    unittest.main()
