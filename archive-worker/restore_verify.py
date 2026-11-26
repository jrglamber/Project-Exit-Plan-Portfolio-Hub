"""Test real archived dumps in disposable, socket-only PostgreSQL clusters."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from archive import DBS, PREFIX, file_hash, read_manifest, write_manifest

IDENT = rb'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+")'
COPY_HEADER = re.compile(rb'^COPY (' + IDENT + rb'\.' + IDENT + rb') \(.*\) FROM stdin;\r?\n$')


def command(args, env=None, timeout=1800, input_data=None):
    r = subprocess.run(args, env=env, input=input_data, capture_output=True, timeout=timeout)
    if r.returncode:
        # Log only a safe class/message, never source SQL or database values.
        raise RuntimeError(f"{args[0]} exited {r.returncode}")
    return r.stdout


def list_keys(s3, bucket, prefix):
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            yield item["Key"]


def latest_manifest(s3, bucket, asset):
    expected_prefix = f"{PREFIX}/{asset}/"
    pattern = re.compile(re.escape(expected_prefix) + r"(\d{4}/\d{2}/\d{2})/verified\.json$")
    keys = sorted(k for k in list_keys(s3, bucket, expected_prefix) if pattern.fullmatch(k))
    if not keys:
        raise RuntimeError("no verified daily snapshot")
    key = keys[-1]
    m = read_manifest(s3, bucket, key)
    if not m or m.get("status") != "verified" or m.get("asset") != asset:
        raise RuntimeError("invalid archive manifest")
    if not m.get("object_key", "").startswith(key.rsplit("/", 1)[0] + "/"):
        raise RuntimeError("archive manifest points outside its date prefix")
    return key, m


def parse_copy_counts(lines):
    current = None
    counts = {}
    blocks = 0
    for line in lines:
        if current is not None:
            if line in (b"\\.\n", b"\\.\r\n"):
                current = None
            else:
                counts[current] += 1
        elif line.startswith(b"COPY "):
            m = COPY_HEADER.fullmatch(line)
            if not m:
                raise RuntimeError("unsupported COPY header")
            current = m.group(1).decode("utf-8")
            counts.setdefault(current, 0)
            blocks += 1
    if current is not None or not blocks:
        raise RuntimeError("incomplete or missing COPY data")
    return counts, blocks


def dump_counts(path):
    # Count source rows from the dump's COPY streams without materializing the
    # uncompressed SQL on disk or holding the entire dump in memory.
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(["pg_restore", "--data-only", "--file=-", str(path)], stdout=subprocess.PIPE, stderr=err)
        try:
            result = parse_copy_counts(proc.stdout)
            if proc.wait(timeout=1800) != 0:
                raise RuntimeError("pg_restore data decode failed")
            return result
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()


def restored_counts(expected, env):
    tables = sorted(expected)
    queries = "\n".join(f"SELECT {i},count(*) FROM ONLY {name};" for i, name in enumerate(tables))
    output = command(["psql", "-X", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-d", "restored"],
                     env, input_data=queries.encode())
    result = {}
    for line in output.decode().splitlines():
        i, count = line.split("\t")
        result[tables[int(i)]] = int(count)
    return result


def restore_and_count(path, expected, root, restore_sql=None):
    data = root / "pgdata"
    socket_dir = root / "socket"
    socket_dir.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG") and not k.endswith("DATABASE_URL")}
    env.update(PGHOST=str(socket_dir), PGPORT="5432", PGUSER="postgres",
               PGDATABASE="postgres", PGCONNECT_TIMEOUT="10")
    command(["initdb", "-D", str(data), "-U", "postgres", "-A", "trust",
             "--locale=C.UTF-8", "--encoding=UTF8"], env, timeout=120)
    # PostgreSQL listens only on the temporary UNIX socket. No service can
    # connect via the network, and the worker has no production DB URL.
    options = (f"-h '' -k {socket_dir} -p 5432 -c fsync=off "
               "-c synchronous_commit=off -c full_page_writes=off "
               "-c shared_buffers=128MB -c maintenance_work_mem=128MB")
    started = False
    try:
        command(["pg_ctl", "-D", str(data), "-l", str(root / "postgres.log"),
                 "-o", options, "-w", "start"], env, timeout=120)
        started = True
        command(["createdb", "restored"], env, timeout=120)
        if restore_sql is None:
            command(["pg_restore", "--exit-on-error", "--no-owner", "--no-privileges", "--no-tablespaces",
                     "--dbname", "restored", str(path)], env)
        else:
            restore_sql(env)
        return restored_counts(expected, env)
    finally:
        if started:
            command(["pg_ctl", "-D", str(data), "-m", "fast", "-w", "stop"], env, timeout=120)


def verify(s3, bucket, asset):
    manifest_key, manifest = latest_manifest(s3, bucket, asset)
    print(f"RESTORE_DOWNLOAD asset={asset} bytes={manifest['bytes']}", flush=True)
    with tempfile.TemporaryDirectory(prefix=f"pep-restore-{asset}-") as directory:
        root = Path(directory)
        path = root / "archive.dump"
        s3.download_file(bucket, manifest["object_key"], str(path))
        if path.stat().st_size != manifest["bytes"] or file_hash(path) != manifest["sha256"]:
            raise RuntimeError("downloaded archive checksum mismatch")
        expected, blocks = dump_counts(path)
        if blocks != manifest["table_data_entries"]:
            raise RuntimeError("COPY blocks do not match archived table-data entries")
        print(f"RESTORE_SOURCE_COUNTS asset={asset} tables={len(expected)} rows={sum(expected.values())}", flush=True)
        actual = restore_and_count(path, expected, root)
        mismatches = sum(expected.get(k) != actual.get(k) for k in set(expected) | set(actual))
        if mismatches:
            raise RuntimeError(f"restored table counts differ: {mismatches}")
        print(f"RESTORE_COUNTS_MATCH asset={asset} tables={len(expected)} rows={sum(actual.values())}", flush=True)
    now = datetime.now(timezone.utc)
    proof = {
        "status": "verified", "asset": asset, "snapshot_manifest": manifest_key,
        "snapshot_sha256": manifest["sha256"], "snapshot_bytes": manifest["bytes"],
        "verified_utc": now.isoformat(), "source_rows": sum(expected.values()),
        "restored_rows": sum(actual.values()), "tables_checked": len(expected),
        "table_counts_match": True, "full_restore_completed": True,
        "production_connections": False, "source_deleted": False,
    }
    key = f"{PREFIX}/restore-proof/{asset}/{now:%Y/%m/%d}/{now:%H%M%S%f}.json"
    write_manifest(s3, bucket, key, proof)
    write_manifest(s3, bucket, f"{PREFIX}/restore-proof/{asset}/latest.json", proof)
    print(f"RESTORE_VERIFIED asset={asset} tables={len(expected)} rows={sum(actual.values())} proof={key}", flush=True)
    return proof


def main():
    import boto3
    from botocore.config import Config
    from urllib.parse import urlsplit

    # Fail closed if the worker ever receives production DB credentials.
    if any(k.endswith("DATABASE_URL") for k in os.environ):
        raise RuntimeError("restore worker must not have production database credentials")
    endpoint = os.environ["AWS_ENDPOINT_URL"]
    if urlsplit(endpoint).scheme != "https":
        raise ValueError("S3 endpoint must use HTTPS")
    bucket = os.environ["AWS_S3_BUCKET_NAME"]
    s3 = boto3.client("s3", endpoint_url=endpoint, region_name=os.environ["AWS_DEFAULT_REGION"],
                      config=Config(signature_version="s3v4", s3={"addressing_style": os.getenv("S3_ADDRESSING_STYLE", "virtual")},
                                    connect_timeout=15, read_timeout=120,
                                    retries={"max_attempts": 3, "mode": "standard"}))
    failures = []
    for asset in DBS:
        try:
            verify(s3, bucket, asset)
        except Exception as exc:
            print(f"RESTORE_FAILED asset={asset} type={type(exc).__name__} stage={str(exc) if isinstance(exc, RuntimeError) else 'unclassified'}", flush=True)
            failures.append(asset)
    if failures:
        print("RESTORE_JOB_FAILED assets=" + ",".join(failures), flush=True)
        return 1
    from retention_plan import produce
    produce(s3, bucket, list_keys)
    print("RESTORE_JOB_COMPLETE assets=indices,metals", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"RESTORE_STARTUP_FAILED type={type(exc).__name__}", flush=True)
        raise SystemExit(1)
