import io
import unittest
from unittest.mock import patch
from incremental import chunks, digest, encode_stream, reconstruct, chunk_key, manifest_read, manifest_write, archive_incremental


class Missing(Exception):
    response = {"Error": {"Code": "404"}}


class Bucket:
    def __init__(self):
        self.objects = {}
    def head_object(self, Bucket, Key):
        try:
            return {"ContentLength": len(self.objects[Key])}
        except KeyError:
            raise Missing()
    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body
    def get_object(self, Bucket, Key):
        try:
            return {"Body": io.BytesIO(self.objects[Key])}
        except KeyError:
            raise Missing()


class IncrementalTests(unittest.TestCase):
    def test_chunks_round_trip_and_bound(self):
        rows = [b"abc\n", b"x" * 400, b"hello\n", b"z\n"] * 30
        result = list(chunks(rows, minimum=8, maximum=128, mask=3))
        self.assertEqual(b"".join(result), b"".join(rows))
        self.assertTrue(all(0 < len(x) <= 128 for x in result))

    def test_recovery_reuses_identical_content(self):
        s3 = Bucket()
        sql = b"CREATE TABLE t (i integer);\n" + b"1\n" * 300000
        first = encode_stream(s3, "bucket", io.BytesIO(sql))
        second = encode_stream(s3, "bucket", io.BytesIO(sql))
        self.assertGreater(first["new_chunk_bytes"], 0)
        self.assertEqual(second["new_chunk_bytes"], 0)
        self.assertEqual(second["reused_chunk_bytes"], first["new_chunk_bytes"])
        manifest_write(s3, "bucket", "manifest", second)
        sink = io.BytesIO()
        self.assertEqual(reconstruct(s3, "bucket", manifest_read(s3, "bucket", "manifest"), sink), len(sql))
        self.assertEqual(sink.getvalue(), sql)

    def test_corrupt_chunk_blocks_manifest(self):
        s3 = Bucket()
        first = encode_stream(s3, "bucket", io.BytesIO(b"abc\n"))
        key = chunk_key(first["chunks"][0]["sha256"])
        s3.objects[key] = b"corrupt"
        with self.assertRaises(Exception):
            reconstruct(s3, "bucket", first)
        with self.assertRaises(Exception):
            encode_stream(s3, "bucket", io.BytesIO(b"abc\n"))

    def test_scheduled_run_rejects_stale_source(self):
        with patch.dict("os.environ", {"INCREMENTAL_REQUIRE_TODAY": "1"}), \
             patch("incremental.latest_manifest", return_value=("project-exit-plan/v1/metals/2000/01/01/verified.json", {})):
            with self.assertRaisesRegex(RuntimeError, "today's verified"):
                archive_incremental(Bucket(), "bucket", "metals")

    def test_insert_resynchronizes_boundaries(self):
        rows = [f"record-{i:06d}\n".encode() for i in range(20000)]
        a = set(map(digest, chunks(rows, 128, 8192, 31)))
        b = set(map(digest, chunks(rows[:100] + [b"new-record\n"] + rows[100:], 128, 8192, 31)))
        self.assertGreater(len(a & b), len(a) * .90)


if __name__ == "__main__":
    unittest.main()
