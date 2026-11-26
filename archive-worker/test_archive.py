import io
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import archive


class Missing(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self, corrupt=False):
        self.items = {}
        self.corrupt = corrupt
        self.uploads = 0

    def get_object(self, Bucket, Key):
        if Key not in self.items:
            raise Missing()
        return {"Body": io.BytesIO(self.items[Key][0])}

    def head_object(self, Bucket, Key):
        data, metadata = self.items[Key]
        return {"ContentLength": len(data), "Metadata": metadata}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.items[Key] = (Body, {})

    def upload_file(self, path, bucket, key, ExtraArgs, **kwargs):
        self.uploads += 1
        self.items[key] = (Path(path).read_bytes() + (b"x" if self.corrupt else b""), ExtraArgs["Metadata"])


def fake_run(args, env=None, timeout=1200):
    if args[0] == "pg_dump":
        assert "default_transaction_read_only=on" in env["PGOPTIONS"]
        Path(args[-1]).write_bytes(b"PGDMP" + b"y" * 2000)
    elif "--list" in args:
        return b"123 0 44 TABLE DATA public test owner"
    return b""


NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)
URL = "postgresql://test:p%24ss@postgres.railway.internal:5432/railway"


class ArchiveTests(unittest.TestCase):
    def test_private_connection(self):
        env = archive.connection_env(URL)
        self.assertEqual(env["PGPASSWORD"], "p$ss")
        self.assertIn("default_transaction_read_only=on", env["PGOPTIONS"])
        with self.assertRaises(ValueError):
            archive.connection_env("postgresql://test:pass@public.example/railway")

    @patch.object(archive, "run", fake_run)
    def test_verified_and_idempotent(self):
        s3 = FakeS3()
        first = archive.archive(s3, "test", "metals", URL, NOW)
        second = archive.archive(s3, "test", "metals", URL, NOW)
        self.assertEqual(first, second)
        self.assertEqual(s3.uploads, 1)
        self.assertEqual(len(s3.items), 2)
        self.assertFalse(first["restoration_tested"])
        self.assertFalse(first["source_deleted"])

    @patch.object(archive, "run", fake_run)
    def test_corruption_fails_closed(self):
        s3 = FakeS3(corrupt=True)
        with self.assertRaisesRegex(RuntimeError, "byte verification"):
            archive.archive(s3, "test", "indices", URL, NOW)
        self.assertFalse(any(k.endswith("verified.json") for k in s3.items))


if __name__ == "__main__":
    unittest.main()
