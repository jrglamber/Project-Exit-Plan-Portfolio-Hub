"""Read-only archive health metadata with a small, independent cache."""
import json
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

PREFIX = "project-exit-plan/v1"
_lock = threading.Lock()
_cache = None
_checked = 0.0


def age_hours(value, now):
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (now - t.astimezone(timezone.utc)).total_seconds() / 3600 if t.tzinfo else None
    except (ValueError, TypeError):
        return None


def read_json(s3, bucket, key):
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"]
        try:
            return json.loads(body.read(1024 * 1024))
        finally:
            body.close()
    except Exception as exc:
        if str(getattr(exc, "response", {}).get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def asset_status(s3, bucket, asset, now):
    prefix = f"{PREFIX}/{asset}/"
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(r["Key"] for r in page.get("Contents", []) if r["Key"].endswith("/verified.json"))
    latest = read_json(s3, bucket, max(keys)) if keys else None
    proof = read_json(s3, bucket, f"{PREFIX}/restore-proof/{asset}/latest.json")
    age = age_hours((latest or {}).get("verified_utc"), now)
    backup_ok = False
    if latest and latest.get("asset") == asset and latest.get("status") == "verified" and latest.get("object_key", "").startswith(prefix):
        head = s3.head_object(Bucket=bucket, Key=latest["object_key"])
        backup_ok = (head.get("ContentLength") == latest.get("bytes") and
                     head.get("Metadata", {}).get("sha256") == latest.get("sha256") and
                     isinstance(age, (float, int)) and 0 <= age <= 36)
    restore_age = age_hours((proof or {}).get("verified_utc"), now)
    restore_ok = bool(proof and proof.get("asset") == asset and proof.get("status") == "verified"
                      and proof.get("full_restore_completed") is True
                      and proof.get("table_counts_match") is True
                      and isinstance(restore_age, (float, int)) and 0 <= restore_age <= 40 * 24)
    return {"backup_ok": backup_ok, "restore_ok": restore_ok,
            "backup_verified_utc": (latest or {}).get("verified_utc"),
            "archive_bytes": (latest or {}).get("bytes") if backup_ok else None,
            "restore_verified_utc": (proof or {}).get("verified_utc"),
            "restored_rows": (proof or {}).get("restored_rows") if restore_ok else None,
            "tables_checked": (proof or {}).get("tables_checked") if restore_ok else None,
            "verified_snapshots": len(keys)}


def build_status(s3, bucket, now):
    assets = {k: asset_status(s3, bucket, k, now) for k in ("indices", "metals")}
    retention = read_json(s3, bucket, f"{PREFIX}/retention/latest.json") or {}
    complete = all(a["backup_ok"] and a["restore_ok"] for a in assets.values())
    return {"status": "HEALTHY" if complete else "REVIEW",
            "checked_utc": now.isoformat(), "assets": assets,
            "retention_mode": retention.get("mode"),
            "retention_generated_utc": retention.get("generated_utc"),
            "retention_review_candidates": {k: len(v.get("candidate_snapshots") or [])
                                            for k, v in (retention.get("assets") or {}).items()},
            "execution_authority": False}


def archive_health():
    global _cache, _checked
    if _cache is not None and time.monotonic() - _checked < 300:
        return _cache
    if not _lock.acquire(blocking=False):
        return {"status": "CHECKING", "assets": {}}
    try:
        if _cache is not None and time.monotonic() - _checked < 300:
            return _cache
        now = datetime.now(timezone.utc)
        try:
            import boto3
            from botocore.config import Config
            endpoint = os.environ["ARCHIVE_S3_ENDPOINT"]
            if urlsplit(endpoint).scheme != "https":
                raise ValueError("unexpected endpoint")
            s3 = boto3.client("s3", endpoint_url=endpoint,
                              region_name=os.environ["ARCHIVE_S3_REGION"],
                              aws_access_key_id=os.environ["ARCHIVE_S3_ACCESS_KEY_ID"],
                              aws_secret_access_key=os.environ["ARCHIVE_S3_SECRET_ACCESS_KEY"],
                              config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"},
                                            connect_timeout=3, read_timeout=5,
                                            retries={"max_attempts": 1, "mode": "standard"}))
            _cache = build_status(s3, os.environ["ARCHIVE_S3_BUCKET"], now)
        except Exception:
            # Never log credentials or present past success as current health.
            _cache = {"status": "UNAVAILABLE", "checked_utc": now.isoformat(), "assets": {}}
        _checked = time.monotonic()
        return _cache
    finally:
        _lock.release()


def install_archive_health_route(app):
    """Put the exact read-only route before the Hub's catch-all mount.

    Starlette matches routes in order. Appending this route to the final
    exported app is insufficient when an earlier mount consumes /api/*.
    Keep registration idempotent and preserve all other route ordering.
    """
    path = "/api/archive-health"
    def matches(route):
        return (getattr(route, "path", None) == path
                and "GET" in (getattr(route, "methods", None) or set()))
    existing = [route for route in app.routes if matches(route)]
    if len(existing) > 1:
        raise RuntimeError("Duplicate archive health routes")
    if not existing:
        app.add_api_route(path, archive_health, methods=["GET"], name="archive_health")
        existing = [route for route in app.routes if matches(route)]
    if len(existing) != 1 or getattr(existing[0], "endpoint", None) is not archive_health:
        raise RuntimeError("Archive health endpoint registration mismatch")
    route = existing[0]
    app.routes.remove(route)
    app.routes.insert(0, route)
    return route
