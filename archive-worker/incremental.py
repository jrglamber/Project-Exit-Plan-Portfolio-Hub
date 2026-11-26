"""Content-addressed SQL recovery archives, derived only from verified v1 dumps.

Full custom dumps remain the authoritative backup. Nothing is deleted.
Each v2 manifest reconstructs an independently verified, portable SQL stream.
"""
import gzip
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from archive import DBS, file_hash
from restore_verify import latest_manifest, dump_counts, restore_and_count

PREFIX = "project-exit-plan/v2"
MIN_CHUNK = 512 * 1024
MAX_CHUNK = 8 * 1024 * 1024
MAX_MANIFEST = 16 * 1024 * 1024
MASK = (1 << 11) - 1
HEX = re.compile(r"^[0-9a-f]{64}$")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def chunks(lines, minimum=MIN_CHUNK, maximum=MAX_CHUNK, mask=MASK):
    """Bounded, row-aware content-defined chunks; re-sync on stable line hashes."""
    buf = bytearray()
    for line in lines:
        if len(line) > maximum:
            if buf:
                yield bytes(buf)
                buf.clear()
            for i in range(0, len(line), maximum):
                yield line[i:i+maximum]
            continue
        if buf and len(buf) + len(line) > maximum:
            yield bytes(buf)
            buf.clear()
        buf.extend(line)
        if len(buf) >= minimum and zlib.crc32(line) & mask == 0:
            yield bytes(buf)
            buf.clear()
    if buf:
        yield bytes(buf)


def body_bytes(s3, bucket, key, limit):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        raw = body.read(limit + 1)
        if len(raw) > limit:
            raise RuntimeError("object exceeds expected bound")
        return raw
    finally:
        body.close()


def missing(exc):
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code")) in (
        "NoSuchKey", "NotFound", "404")


def manifest_read(s3, bucket, key):
    try:
        return json.loads(body_bytes(s3, bucket, key, MAX_MANIFEST))
    except Exception as exc:
        if missing(exc):
            return None
        raise


def manifest_write(s3, bucket, key, data):
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > MAX_MANIFEST:
        raise RuntimeError("manifest exceeds bound")
    s3.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="application/json")
    if body_bytes(s3, bucket, key, MAX_MANIFEST) != payload:
        raise RuntimeError("manifest readback mismatch")


def chunk_key(sha):
    if not HEX.fullmatch(sha):
        raise RuntimeError("invalid chunk SHA")
    return f"{PREFIX}/chunks/{sha[:2]}/{sha}.gz"


def restore_chunk(s3, bucket, entry):
    if not 0 < entry["raw_bytes"] <= MAX_CHUNK or not 0 < entry["stored_bytes"] <= MAX_CHUNK + 65536:
        raise RuntimeError("invalid chunk length")
    compressed = body_bytes(s3, bucket, chunk_key(entry["sha256"]), entry["stored_bytes"])
    if len(compressed) != entry["stored_bytes"]:
        raise RuntimeError("stored chunk length mismatch")
    # Limit decompressed read, even if a corrupt object expands unexpectedly.
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as reader:
        raw = reader.read(entry["raw_bytes"] + 1)
    if len(raw) != entry["raw_bytes"] or digest(raw) != entry["sha256"]:
        raise RuntimeError("chunk SHA or length mismatch")
    return raw


def ensure_chunk(s3, bucket, raw):
    sha = digest(raw)
    key = chunk_key(sha)
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    entry = {"sha256": sha, "raw_bytes": len(raw), "stored_bytes": len(compressed)}
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if not missing(exc):
            raise
        s3.put_object(Bucket=bucket, Key=key, Body=compressed,
                      ContentType="application/gzip", Metadata={"sha256": sha})
        new = True
    else:
        # Different gzip library versions can encode identical raw data differently.
        entry["stored_bytes"] = head["ContentLength"]
        new = False
    # Do not trust HEAD metadata alone: verify bytes for both reused and new objects.
    recovered = restore_chunk(s3, bucket, entry)
    if recovered != raw:
        raise RuntimeError("chunk readback mismatch")
    return entry, new


def reconstruct(s3, bucket, manifest, sink=None):
    h = hashlib.sha256()
    n = 0
    for entry in manifest["chunks"]:
        raw = restore_chunk(s3, bucket, entry)
        h.update(raw)
        n += len(raw)
        if sink is not None:
            sink.write(raw)
    if n != manifest["sql_bytes"] or h.hexdigest() != manifest["sql_sha256"]:
        raise RuntimeError("reconstructed stream mismatch")
    return n


def encode_stream(s3, bucket, stream):
    entries, raw_size, new_bytes, reused_bytes = [], 0, 0, 0
    h = hashlib.sha256()
    for raw in chunks(stream):
        h.update(raw)
        raw_size += len(raw)
        entry, new = ensure_chunk(s3, bucket, raw)
        entries.append(entry)
        if new:
            new_bytes += entry["stored_bytes"]
        else:
            reused_bytes += entry["stored_bytes"]
    if not entries:
        raise RuntimeError("empty SQL stream")
    return {"chunks": entries, "sql_bytes": raw_size, "sql_sha256": h.hexdigest(),
            "new_chunk_bytes": new_bytes, "reused_chunk_bytes": reused_bytes}


def verify_sql_restore(s3, bucket, manifest, path, root, table_entries):
    expected, blocks = dump_counts(path)
    if blocks != table_entries:
        raise RuntimeError("source COPY count mismatch")
    def restore_sql(env):
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", "restored"],
                                    env=env, stdin=subprocess.PIPE, stdout=out, stderr=err)
            try:
                reconstruct(s3, bucket, manifest, proc.stdin)
                proc.stdin.close()
                if proc.wait(timeout=1800):
                    raise RuntimeError("SQL restore failed")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if not proc.stdin.closed:
                    proc.stdin.close()
    actual = restore_and_count(path, expected, root, restore_sql=restore_sql)
    if actual != expected:
        raise RuntimeError("restored SQL row counts differ")
    return {"tables_checked": len(expected), "restored_rows": sum(actual.values()),
            "source_rows": sum(expected.values()), "full_sql_restore_tested": True}


def archive_incremental(s3, bucket, asset):
    source_key, source = latest_manifest(s3, bucket, asset)
    if not HEX.fullmatch(source.get("sha256", "")):
        raise RuntimeError("source SHA missing")
    day = source_key.rsplit("/", 1)[0].split("/")[-3:]
    key = f"{PREFIX}/{asset}/{'/'.join(day)}/{source['sha256']}.json"
    old = manifest_read(s3, bucket, key)
    if old is not None:
        if old.get("source_sha256") != source["sha256"] or old.get("status") != "verified":
            raise RuntimeError("invalid existing v2 manifest")
        reconstruct(s3, bucket, old)
        print(f"INCREMENTAL_ALREADY_VERIFIED asset={asset} chunks={len(old['chunks'])}", flush=True)
        return old
    with tempfile.TemporaryDirectory(prefix=f"pep-incr-{asset}-") as d:
        path = Path(d) / "archive.dump"
        s3.download_file(bucket, source["object_key"], str(path))
        if path.stat().st_size != source["bytes"] or file_hash(path) != source["sha256"]:
            raise RuntimeError("source dump checksum mismatch")
        print(f"INCREMENTAL_SOURCE_VERIFIED asset={asset} bytes={source['bytes']}", flush=True)
        # Portable SQL equivalent of the verified restore procedure. Original
        # custom dump preserves the original owners/ACLs/tablespaces.
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(["pg_restore", "--no-owner", "--no-privileges",
                                     "--no-tablespaces", "--file=-", str(path)],
                                    stdout=subprocess.PIPE, stderr=err)
            try:
                result = encode_stream(s3, bucket, proc.stdout)
                if proc.wait(timeout=1800):
                    raise RuntimeError("pg_restore decode failed")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stdout.close()
        manifest = {**result, "format": "portable-postgresql-sql-chunks-v2",
                    "status": "verified", "asset": asset, "source_manifest": source_key,
                    "source_sha256": source["sha256"], "source_bytes": source["bytes"],
                    "verified_utc": datetime.now(timezone.utc).isoformat(),
                    "verification": "all chunks remote readback plus full ordered reconstruction SHA-256",
                    "full_sql_restore_tested": False, "source_deleted": False}
        # Test initial recovery using only reconstructed bucket SQL and a disposable
        # socket-only database. Future runs still verify every chunk and overall SHA.
        if os.getenv("INCREMENTAL_RESTORE_TEST", "1") == "1":
            manifest.update(verify_sql_restore(s3, bucket, manifest, path, Path(d),
                                               source["table_data_entries"]))
            print(f"INCREMENTAL_RESTORE_VERIFIED asset={asset} tables={manifest['tables_checked']} "
                  f"rows={manifest['restored_rows']}", flush=True)
        else:
            reconstruct(s3, bucket, manifest)
    manifest_write(s3, bucket, key, manifest)
    print(f"INCREMENTAL_VERIFIED asset={asset} chunks={len(result['chunks'])} "
          f"sql_bytes={result['sql_bytes']} new_bytes={result['new_chunk_bytes']} "
          f"reused_bytes={result['reused_chunk_bytes']} source_bytes={source['bytes']} "
          f"manifest={key}", flush=True)
    return manifest


def main():
    import boto3
    from botocore.config import Config
    if any(k.endswith("DATABASE_URL") for k in os.environ):
        raise RuntimeError("incremental worker must not receive production DB credentials")
    endpoint = os.environ["AWS_ENDPOINT_URL"]
    if urlsplit(endpoint).scheme != "https":
        raise ValueError("S3 endpoint must use HTTPS")
    bucket = os.environ["AWS_S3_BUCKET_NAME"]
    s3 = boto3.client("s3", endpoint_url=endpoint,
                      region_name=os.environ["AWS_DEFAULT_REGION"],
                      config=Config(signature_version="s3v4",
                                    s3={"addressing_style": os.getenv("S3_ADDRESSING_STYLE", "virtual")},
                                    connect_timeout=15, read_timeout=120,
                                    retries={"max_attempts": 3, "mode": "standard"}))
    failures = []
    for asset in DBS:
        try:
            archive_incremental(s3, bucket, asset)
        except Exception as exc:
            print(f"INCREMENTAL_FAILED asset={asset} type={type(exc).__name__}", flush=True)
            failures.append(asset)
    if failures:
        return 1
    print("INCREMENTAL_JOB_COMPLETE assets=indices,metals", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"INCREMENTAL_STARTUP_FAILED type={type(exc).__name__}", flush=True)
        raise SystemExit(1)
