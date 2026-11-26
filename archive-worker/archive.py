"""Read-only logical snapshots of live databases into a private S3 bucket.

No broker credentials, database writes, archive deletions or public HTTP server.
Each successful snapshot is immutable; a manifest is written only after checking
the PostgreSQL archive and the SHA-256 of the object retrieved from storage.
"""
import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs

PREFIX = "project-exit-plan/v1"
DBS = {"indices": "INDICES_DATABASE_URL", "metals": "METALS_DATABASE_URL"}


def connection_env(url):
    u = urlsplit(url)
    if u.scheme not in ("postgres", "postgresql") or not u.hostname:
        raise ValueError("invalid postgres connection reference")
    if not u.hostname.endswith(".railway.internal"):
        raise ValueError("archive must use the private Railway network")
    if not u.username or not u.password or not u.path.strip("/"):
        raise ValueError("incomplete postgres connection reference")
    sslmode = parse_qs(u.query).get("sslmode", ["prefer"])[0]
    if sslmode not in ("prefer", "require", "verify-ca", "verify-full", "allow", "disable"):
        raise ValueError("unsupported SSL mode")
    # Restrict the archive's connection to a read-only, bounded transaction.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update(PGHOST=u.hostname, PGPORT=str(u.port or 5432),
               PGUSER=unquote(u.username), PGPASSWORD=unquote(u.password),
               PGDATABASE=unquote(u.path[1:]), PGSSLMODE=sslmode,
               PGCONNECT_TIMEOUT="15", PGAPPNAME="pep-readonly-archive",
               PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=900000 -c lock_timeout=5000")
    return env


def run(args, env=None, timeout=1200):
    # No raw command output or connection details are emitted to platform logs.
    r = subprocess.run(args, env=env, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{args[0]} exited {r.returncode}")
    return r.stdout


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def object_hash(s3, bucket, key):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    h = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    finally:
        body.close()
    return h.hexdigest(), size


def read_manifest(s3, bucket, key):
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"]
        try:
            return json.loads(body.read(64 * 1024))
        finally:
            body.close()
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code")
        if str(code) in ("NoSuchKey", "NotFound", "404"):
            return None
        raise


def write_manifest(s3, bucket, key, manifest):
    encoded = json.dumps(manifest, sort_keys=True, indent=2).encode()
    s3.put_object(Bucket=bucket, Key=key, Body=encoded, ContentType="application/json")
    if read_manifest(s3, bucket, key) != manifest:
        raise RuntimeError("manifest read-back mismatch")


def archive(s3, bucket, asset, url, now, upload_config=None):
    if asset not in DBS:
        raise ValueError("unrecognized asset")
    base = f"{PREFIX}/{asset}/{now:%Y/%m}"
    daily = f"{base}/{now:%d}/verified.json"
    previous = read_manifest(s3, bucket, daily)
    if previous:
        # Avoid repeating an already verified daily snapshot. Check that the
        # manifest really points to the expected asset/date and object length.
        key = previous.get("object_key", "")
        if (previous.get("asset") != asset or not key.startswith(f"{base}/{now:%d}/")
                or previous.get("status") != "verified" or not previous.get("sha256")):
            raise RuntimeError("existing daily manifest is invalid")
        head = s3.head_object(Bucket=bucket, Key=key)
        if head.get("ContentLength") != previous.get("bytes") or head.get("Metadata", {}).get("sha256") != previous["sha256"]:
            raise RuntimeError("existing archive metadata mismatch")
        print(f"ARCHIVE_ALREADY_VERIFIED asset={asset} bytes={previous['bytes']} key={key}", flush=True)
        return previous

    connection = connection_env(url)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    with tempfile.TemporaryDirectory(prefix=f"pep-{asset}-") as directory:
        path = Path(directory) / "archive.dump"
        run(["pg_dump", "--format=custom", "--compress=6", "--lock-wait-timeout=5s",
             "--no-password", "--file", str(path)], connection)
        size = path.stat().st_size
        if size < 1024:
            raise RuntimeError("archive unexpectedly small")
        toc = run(["pg_restore", "--list", str(path)])
        tables = sum(b" TABLE DATA " in line for line in toc.splitlines())
        if not tables:
            raise RuntimeError("archive contains no table data entries")
        # Decode the entire archive into a discard sink, rather than checking
        # only the header/TOC. This does not connect to any database.
        run(["pg_restore", "--file=/dev/null", str(path)])
        sha = file_hash(path)
        key = f"{base}/{now:%d}/{stamp}-{sha[:16]}.dump"
        kwargs = {"ExtraArgs": {"ContentType": "application/octet-stream",
                                 "Metadata": {"sha256": sha, "asset": asset}}}
        if upload_config is not None:
            kwargs["Config"] = upload_config
        s3.upload_file(str(path), bucket, key, **kwargs)
        actual_sha, actual_size = object_hash(s3, bucket, key)
        if actual_sha != sha or actual_size != size:
            raise RuntimeError("uploaded archive failed complete byte verification")
        manifest = {
            "format": "postgresql-custom-v1",
            "asset": asset, "status": "verified", "object_key": key,
            "captured_utc": now.isoformat(), "verified_utc": datetime.now(timezone.utc).isoformat(),
            "bytes": size, "sha256": sha, "table_data_entries": tables,
            "verification": "full pg_restore decode and remote SHA-256 readback",
            "restoration_tested": False, "source_deleted": False,
        }
        write_manifest(s3, bucket, daily, manifest)
        print(f"ARCHIVE_VERIFIED asset={asset} bytes={size} table_data_entries={tables} sha256={sha} key={key}", flush=True)
        return manifest


def main():
    import boto3
    from botocore.config import Config
    from boto3.s3.transfer import TransferConfig

    endpoint = os.environ["AWS_ENDPOINT_URL"]
    if urlsplit(endpoint).scheme != "https":
        raise ValueError("S3 endpoint must use HTTPS")
    bucket = os.environ["AWS_S3_BUCKET_NAME"]
    s3 = boto3.client("s3", endpoint_url=endpoint,
                      region_name=os.environ["AWS_DEFAULT_REGION"],
                      config=Config(signature_version="s3v4", s3={"addressing_style": os.getenv("S3_ADDRESSING_STYLE", "path")},
                                    connect_timeout=15, read_timeout=120,
                                    retries={"max_attempts": 3, "mode": "standard"}))
    transfer = TransferConfig(multipart_threshold=32 * 1024 * 1024,
                              multipart_chunksize=16 * 1024 * 1024, max_concurrency=2)
    failures = []
    for asset, variable in DBS.items():
        for attempt in range(2):
            try:
                archive(s3, bucket, asset, os.environ[variable], datetime.now(timezone.utc), transfer)
                break
            except Exception as exc:
                print(f"ARCHIVE_ATTEMPT_FAILED asset={asset} attempt={attempt+1} type={type(exc).__name__}", flush=True)
                if attempt == 0:
                    time.sleep(45)
                else:
                    failures.append(asset)
    if failures:
        print("ARCHIVE_JOB_FAILED assets=" + ",".join(failures), flush=True)
        return 1
    print("ARCHIVE_JOB_COMPLETE assets=indices,metals", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # No secret-bearing exception representations in logs.
        print(f"ARCHIVE_STARTUP_FAILED type={type(exc).__name__}", flush=True)
        raise SystemExit(1)
