"""Delete at most one fully archived, independently restored, cold XAU review page.

The default is OFF. No broker/trading changes, no bucket deletions, no DDL.
Any uncertain archive, source data, trade state, schema, or lock fails closed.
"""
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from archive import connection_env, file_hash
from incremental import manifest_read, manifest_write
from retention_audit import HOT_ID_WINDOW, proof_pages
from table_archive import TABLE, PREFIX, PAGE_IDS, BOUNDS_SQL, SCHEMA_SQL, read_json
from table_restore import canonical_sha, validate_receipt, obtain_page

LINKS = "metals_xau_live_trade_links"
CLEANUP_PREFIX = f"{PREFIX}/cleanup-proof"
OPTIONS = ("-c lock_timeout=1000 -c statement_timeout=20000 "
           "-c idle_in_transaction_session_timeout=30000 -c timezone=UTC "
           "-c client_encoding=UTF8")

class Ineligible(Exception):
    """Normal fail-closed outcome, with a fixed non-sensitive reason."""


def connect(env):
    import psycopg2
    conn = psycopg2.connect(host=env["PGHOST"], port=env["PGPORT"],
        user=env["PGUSER"], password=env.get("PGPASSWORD", ""),
        dbname=env["PGDATABASE"], sslmode=env.get("PGSSLMODE", "prefer"),
        connect_timeout=15, options=OPTIONS, application_name="pep-verified-xau-cleanup")
    conn.set_session(isolation_level="REPEATABLE READ", readonly=False, autocommit=False)
    return conn


def verify_structure(cur, receipt):
    # A DELETE must not cascade or invoke user-defined triggers/rules, and
    # must target one ordinary table rather than an inherited/partition tree.
    cur.execute(f"""
        SELECT c.relkind = 'r'
          AND NOT EXISTS (SELECT 1 FROM pg_inherits WHERE inhparent=c.oid OR inhrelid=c.oid)
          AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE contype='f' AND confrelid=c.oid)
          AND NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=c.oid AND (tgtype::int & 8) <> 0)
          AND NOT EXISTS (SELECT 1 FROM pg_rewrite WHERE ev_class=c.oid)
          AND NOT c.relrowsecurity
        FROM pg_class c WHERE c.oid='public.{TABLE}'::regclass
    """)
    if cur.fetchone() != (True,):
        raise Ineligible("unexpected_delete_side_effect_or_structure")
    cur.execute(SCHEMA_SQL)
    if cur.fetchone()[0] != receipt["columns"]:
        raise Ineligible("source_schema_changed")


def export_current(cur, lo, hi, path):
    sql = cur.mogrify(
        f"COPY (SELECT * FROM public.{TABLE} WHERE id >= %s AND id <= %s "
        "ORDER BY id) TO STDOUT WITH (FORMAT CSV, HEADER TRUE, NULL '\\N')",
        (lo, hi))
    with open(path, "wb") as out:
        cur.copy_expert(sql.decode("utf-8"), out)


def cleanup_transaction(conn, receipt, raw_archive, root):
    """Equal byte content + locks + exact DELETE, all within a single transaction.

    Every Ineligible or DB error exits the connection context via rollback.
    External object reads/writes happen outside this transaction.
    """
    lo, hi = receipt["first_id_requested"], receipt["last_id_requested"]
    count = receipt["row_count"]
    if (type(lo) is not int or type(hi) is not int or not 0 < lo <= hi
            or hi-lo+1 != PAGE_IDS or not 0 < count <= PAGE_IDS):
        raise Ineligible("not_a_full_nonempty_page")
    # Verify the already downloaded raw archive; no network access under locks.
    if raw_archive.stat().st_size != receipt["csv_bytes"] or file_hash(raw_archive) != receipt["csv_sha256"]:
        raise Ineligible("local_archive_mismatch")
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(73195946, 50711)")
            if cur.fetchone() != (True,):
                raise Ineligible("another_cleanup_running")
            cur.execute(f"LOCK TABLE ONLY public.{TABLE} IN ROW EXCLUSIVE MODE")
            verify_structure(cur, receipt)
            cur.execute(f"SELECT max(id) FROM ONLY public.{TABLE}")
            latest = cur.fetchone()[0]
            if latest is None or hi >= latest-HOT_ID_WINDOW:
                raise Ineligible("partial_hot_window")
            # Lock exactly the old source rows. No hot rows are locked or changed.
            cur.execute(f"SELECT id, link_id FROM ONLY public.{TABLE} "
                        "WHERE id >= %s AND id <= %s ORDER BY id FOR UPDATE", (lo, hi))
            rows = cur.fetchall()
            if len(rows) != count:
                raise Ineligible("source_row_count_changed")
            ids = [r[0] for r in rows]
            if (ids[0], ids[-1]) != (receipt["first_present_id"], receipt["last_present_id"]):
                raise Ineligible("source_row_ids_changed")
            if any(link is None for _, link in rows):
                raise Ineligible("unknown_trade_link")
            links = sorted({link for _, link in rows})
            # FOR SHARE prevents reconciliation/update/delete of these links
            # until the transaction ends. Missing and all nonterminal statuses
            # are protected. No link rows are modified.
            cur.execute(f"SELECT id, status, closed_at_utc FROM public.{LINKS} "
                        "WHERE id = ANY(%s) ORDER BY id FOR SHARE", (links,))
            states = cur.fetchall()
            if ([r[0] for r in states] != links or
                    any(status not in ("CLOSED_BROKER", "CLOSED_MANAGER") or
                        closed is None or not str(closed).strip() for _,status,closed in states)):
                raise Ineligible("open_unknown_or_unreconciled_trade_links")
            current = root/"current.csv"
            export_current(cur, lo, hi, current)
            if current.stat().st_size != receipt["csv_bytes"] or file_hash(current) != receipt["csv_sha256"]:
                raise Ineligible("source_content_not_identical")
            # Exact locked ID list; a newly inserted row outside that list
            # cannot be removed by this statement.
            cur.execute(f"DELETE FROM ONLY public.{TABLE} WHERE id = ANY(%s) RETURNING id", (ids,))
            removed = sorted(r[0] for r in cur.fetchall())
            if removed != ids:
                raise RuntimeError("deleted IDs mismatch; rollback")
            cur.execute(f"SELECT count(*) FROM ONLY public.{TABLE} WHERE id = ANY(%s)", (ids,))
            if cur.fetchone() != (0,):
                raise RuntimeError("post-delete verification failed; rollback")
            result = {"first_id": lo, "last_id": hi, "rows_deleted": len(ids),
                      "latest_id_at_check": latest, "exclusive_cutoff_id": latest-HOT_ID_WINDOW,
                      "byte_match": True, "closed_links_locked": len(links),
                      "post_delete_target_rows": 0}
        # Commit occurs on exit; the caller cannot publish a successful receipt
        # unless commit has returned without exception.
    return result


def clean(s3, bucket, url):
    if os.getenv("XAU_VERIFIED_CLEANUP_ENABLED") != "1":
        print("CLEANUP_DISABLED source_deleted=false", flush=True)
        return
    env = connection_env(url)
    env["PGOPTIONS"] = "-c default_transaction_read_only=on -c statement_timeout=15000 -c lock_timeout=1000"
    bounds = read_json(env, BOUNDS_SQL)
    if bounds.get("read_only") != "on" or bounds["oldest"] is None or bounds["latest"] is None:
        raise RuntimeError("source bounds unavailable")
    cutoff = bounds["latest"]-HOT_ID_WINDOW
    pages, proof = proof_pages(s3, bucket, bounds["oldest"], cutoff)
    candidates = [p for p in pages if p["last_id"] < cutoff
                  and p["last_id"]-p["first_id"]+1 == PAGE_IDS and p["rows"] > 0]
    if not candidates:
        print(f"CLEANUP_NO_ELIGIBLE_PAGE oldest_id={bounds['oldest']} cutoff={cutoff} "
              f"restored_rows={proof['total_restored_rows'] if proof else 0} rows_deleted=0", flush=True)
        return
    checkpoint = manifest_read(s3, bucket, f"{PREFIX}/checkpoint.json")
    if (not checkpoint or checkpoint.get("status") != "verified" or checkpoint.get("table") != TABLE
            or checkpoint.get("source_deleted") is not False
            or checkpoint.get("first_archived_id") != proof["first_archived_id"]
            or checkpoint.get("next_id", 0) < proof["next_id"]):
        raise RuntimeError("archive checkpoint inconsistent with restore proof")
    for page in candidates:
        lo, hi = page["first_id"], page["last_id"]
        receipt = validate_receipt(manifest_read(s3, bucket, page["manifest"]), lo, hi, page["manifest"])
        if (canonical_sha(receipt) != page["manifest_sha256"] or receipt["sha256"] != page["sha256"]
                or receipt["csv_sha256"] != page["csv_sha256"] or receipt["row_count"] != page["rows"]):
            raise RuntimeError("archive receipt differs from independently restored proof")
        with tempfile.TemporaryDirectory(prefix="pep-xau-cleanup-") as directory:
            root = Path(directory)
            raw = obtain_page(s3, bucket, receipt, root)
            now = datetime.now(timezone.utc)
            key = f"{CLEANUP_PREFIX}/{now:%Y/%m/%d}/{lo:020d}-{hi:020d}-{uuid.uuid4().hex}"
            intent = {"status":"intent_only", "table":TABLE, "archive_manifest":page["manifest"],
                      "archive_object":receipt["object_key"], "restore_proof":proof["proof_key"],
                      "archive_sha256":receipt["sha256"], "csv_sha256":receipt["csv_sha256"],
                      "first_id":lo, "last_id":hi, "expected_rows":receipt["row_count"],
                      "created_utc":now.isoformat(), "archive_retained":True}
            # Durable intent before any source write; this remains truthful even
            # if the process is interrupted immediately after the DB commit.
            manifest_write(s3, bucket, key+"/intent.json", intent)
            if manifest_read(s3, bucket, key+"/intent.json") != intent:
                raise RuntimeError("cleanup intent not verified")
            conn = connect(env)
            try:
                try:
                    result = cleanup_transaction(conn, receipt, raw, root)
                except Ineligible as exc:
                    print(f"CLEANUP_PAGE_PROTECTED first_id={lo} last_id={hi} reason={exc} rows_deleted=0", flush=True)
                    manifest_write(s3, bucket, key+"/outcome.json", dict(intent,status="skipped",
                        reason=str(exc),rows_deleted=0))
                    continue
                except Exception as exc:
                    # Lock/serialization timeouts are safe rollbacks, not errors
                    # requiring somebody to intervene. No retry inside a run.
                    if getattr(exc,"pgcode",None) in ("55P03","40001","40P01","57014"):
                        print(f"CLEANUP_BUSY first_id={lo} last_id={hi} rows_deleted=0",flush=True)
                        manifest_write(s3,bucket,key+"/outcome.json",dict(intent,status="skipped",reason="database_busy",rows_deleted=0))
                        return
                    raise
            finally:
                conn.close()
            completion = dict(intent, status="committed", committed_utc=datetime.now(timezone.utc).isoformat())
            completion.update(result)
            try:
                manifest_write(s3, bucket, key+"/outcome.json", completion)
                if manifest_read(s3, bucket, key+"/outcome.json") != completion:
                    raise RuntimeError("cleanup outcome readback mismatch")
            except Exception:
                print(f"CLEANUP_COMMITTED_RECEIPT_FAILED first_id={lo} last_id={hi} "
                      f"rows_deleted={result['rows_deleted']} intent={key}/intent.json",flush=True)
                raise
            print(f"CLEANUP_COMMITTED_VERIFIED first_id={lo} last_id={hi} "
                  f"rows_deleted={result['rows_deleted']} exclusive_cutoff={result['exclusive_cutoff_id']} "
                  f"archive_retained=true receipt={key}/outcome.json",flush=True)
            return  # Hard cap: one successful 5,000-ID page per execution.
    print("CLEANUP_NO_SAFE_PAGE rows_deleted=0",flush=True)

def main():
    import boto3
    from botocore.config import Config
    endpoint = os.environ["AWS_ENDPOINT_URL"]
    if urlsplit(endpoint).scheme != "https":
        raise ValueError("bucket endpoint must use HTTPS")
    s3 = boto3.client("s3",endpoint_url=endpoint,region_name=os.environ["AWS_DEFAULT_REGION"],
        config=Config(signature_version="s3v4",s3={"addressing_style":"virtual"},
                      connect_timeout=15,read_timeout=90,retries={"max_attempts":3,"mode":"standard"}))
    clean(s3,os.environ["AWS_S3_BUCKET_NAME"],os.environ["DATABASE_URL"])
    return 0
