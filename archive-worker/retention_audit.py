"""Read-only retention eligibility audit for archived XAU manager review rows.

The historical one-million-ID hot window is preserved. A complete page may
become a future deletion candidate only if an isolated restore proved exact
contents, current source CSV matches the receipt byte-for-byte, all links
are definitively closed, and the entire interval is outside the hot window.
This module contains no deletion, update, or DDL operations.
"""
import os
from datetime import datetime,timezone
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from archive import connection_env,file_hash
from incremental import manifest_read,manifest_write
from table_archive import TABLE,PREFIX,BOUNDS_SQL,read_json,dump_page
from table_restore import PROOF_PREFIX,canonical_sha,validate_receipt

HOT_ID_WINDOW=1_000_000
MAX_PAGES=8
PLAN_KEY=f"{PREFIX}/retention-plan/latest.json"


def proof_pages(s3,bucket,oldest,cutoff):
    latest=manifest_read(s3,bucket,f"{PROOF_PREFIX}/latest.json")
    if not latest:
        return [],None
    proof=latest
    upper=latest["next_id"]
    matched=[]
    seen=set()
    while proof:
        key=proof.get("proof_key","")
        if (not key.startswith(PROOF_PREFIX+"/batches/") or key in seen
                or proof.get("status")!="verified" or proof.get("table")!=TABLE
                or proof.get("full_byte_roundtrip") is not True
                or proof.get("production_connections") is not False
                or proof.get("source_deleted") is not False
                or proof.get("next_id")!=upper
                or manifest_read(s3,bucket,key)!=proof):
            raise RuntimeError("restore proof chain is invalid")
        seen.add(key)
        current=proof["batch_first_id"]
        for page in proof["pages"]:
            if page["first_id"]!=current or page["last_id"]<current:
                raise RuntimeError("restore proof has a gap or overlap")
            current=page["last_id"]+1
            if page["last_id"]>=oldest and page["first_id"]<cutoff:
                matched.append(page)
        if current!=upper:
            raise RuntimeError("restore proof end mismatch")
        upper=proof["batch_first_id"]
        if upper<=oldest:
            break
        previous=proof.get("previous_proof")
        if previous and not previous.startswith(PROOF_PREFIX+"/batches/"):
            raise RuntimeError("invalid prior proof key")
        proof=manifest_read(s3,bucket,previous) if previous else None
        if previous and not proof:
            raise RuntimeError("missing previous restore proof")
    return sorted(matched,key=lambda x:x["first_id"])[:MAX_PAGES],latest


def count_page(env,lo,hi,cutoff):
    # Only bounded, indexed review ID ranges. Require explicit terminal link
    # state; null/missing links and everything else are protected.
    sql=f"""
    SELECT json_build_object(
        'source_rows',count(*),
        'outside_hot_window',count(*) FILTER (WHERE r.id < {cutoff}),
        'protected_rows',count(*) FILTER (WHERE t.id IS NULL OR t.status IS NULL
            OR t.status NOT IN ('CLOSED_BROKER','CLOSED_MANAGER')
            OR t.closed_at_utc IS NULL OR t.closed_at_utc=''),
        'closed_cold_rows',count(*) FILTER (WHERE r.id < {cutoff}
            AND t.status IN ('CLOSED_BROKER','CLOSED_MANAGER')
            AND t.closed_at_utc IS NOT NULL AND t.closed_at_utc<>''))
    FROM public.{TABLE} r
    LEFT JOIN public.metals_xau_live_trade_links t ON t.id=r.link_id
    WHERE r.id >= {lo} AND r.id <= {hi}
    """
    return read_json(env,sql)


def assess_page(page,receipt,counts,source_match,cutoff):
    complete_cold=page["last_id"]<cutoff
    blockers=[]
    if not complete_cold:
        blockers.append("partial_hot_window")
    if not source_match or counts["source_rows"]!=receipt["row_count"]:
        blockers.append("source_content_not_identical")
    if counts["protected_rows"]:
        blockers.append("open_unknown_or_unreconciled_trade_links")
    return {"first_id":page["first_id"],"last_id":page["last_id"],
            "archive_manifest":page["manifest"],"archive_sha256":page["sha256"],
            "source_matches_archive":source_match,"source_rows":counts["source_rows"],
            "outside_hot_window":counts["outside_hot_window"],
            "protected_rows":counts["protected_rows"],"closed_cold_rows":counts["closed_cold_rows"],
            "future_candidate_rows":counts["source_rows"] if not blockers else 0,
            "blockers":blockers}


def audit(s3,bucket,url):
    env=connection_env(url)
    env["PGAPPNAME"]="pep-readonly-retention-audit"
    env["PGOPTIONS"]="-c default_transaction_read_only=on -c statement_timeout=25000 -c lock_timeout=1000"
    bounds=read_json(env,BOUNDS_SQL)
    if bounds.get("read_only")!="on" or bounds["oldest"] is None or bounds["latest"] is None:
        raise RuntimeError("read-only source bounds unavailable")
    cutoff=bounds["latest"]-HOT_ID_WINDOW
    pages,latest=proof_pages(s3,bucket,bounds["oldest"],cutoff)
    assessments=[]
    for page in pages:
        lo,hi=page["first_id"],page["last_id"]
        receipt=validate_receipt(manifest_read(s3,bucket,page["manifest"]),lo,hi,page["manifest"])
        if (canonical_sha(receipt)!=page["manifest_sha256"] or receipt["sha256"]!=page["sha256"]
                or receipt["csv_sha256"]!=page["csv_sha256"] or receipt["row_count"]!=page["rows"]):
            raise RuntimeError("archive receipt differs from restored proof")
        counts=count_page(env,lo,hi,cutoff)
        with tempfile.TemporaryDirectory(prefix="pep-retention-plan-") as d:
            path=Path(d)/"current.csv"
            dump_page(env,lo,hi,path)
            match=path.stat().st_size==receipt["csv_bytes"] and file_hash(path)==receipt["csv_sha256"]
        assessments.append(assess_page(page,receipt,counts,match,cutoff))
    now=datetime.now(timezone.utc)
    plan={"status":"verified_read_only_plan","table":TABLE,"source_deleted":False,
          "oldest_id":bounds["oldest"],"latest_id":bounds["latest"],"exclusive_cutoff_id":cutoff,
          "hot_id_window":HOT_ID_WINDOW,"max_pages_checked":MAX_PAGES,"pages":assessments,
          "restore_proof":latest["proof_key"] if latest else None,
          "total_restored_rows":latest["total_restored_rows"] if latest else 0,
          "outside_hot_rows_checked":sum(x["outside_hot_window"] for x in assessments),
          "closed_cold_rows_checked":sum(x["closed_cold_rows"] for x in assessments),
          "future_candidate_rows":sum(x["future_candidate_rows"] for x in assessments),
          "created_utc":now.isoformat(),"requires_separate_approval_and_fresh_transactional_proof":True}
    key=f"{PREFIX}/retention-plan/{now:%Y/%m/%d}/{now:%H%M%S%f}.json"
    manifest_write(s3,bucket,key,plan)
    manifest_write(s3,bucket,PLAN_KEY,plan)
    print(f"RETENTION_AUDIT_VERIFIED oldest_id={bounds['oldest']} latest_id={bounds['latest']} "
          f"exclusive_cutoff={cutoff} pages_checked={len(assessments)} "
          f"old_rows={plan['outside_hot_rows_checked']} closed_old_rows={plan['closed_cold_rows_checked']} "
          f"candidate_rows={plan['future_candidate_rows']} source_deleted=false",flush=True)
    for a in assessments:
        print(f"RETENTION_AUDIT_PAGE first_id={a['first_id']} last_id={a['last_id']} "
              f"source_match={str(a['source_matches_archive']).lower()} protected={a['protected_rows']} "
              f"blockers={','.join(a['blockers']) or 'none'}",flush=True)
    return plan


def main():
    import boto3
    from botocore.config import Config
    endpoint=os.environ["AWS_ENDPOINT_URL"]
    if urlsplit(endpoint).scheme!="https":
        raise ValueError("S3 endpoint must use HTTPS")
    s3=boto3.client("s3",endpoint_url=endpoint,region_name=os.environ["AWS_DEFAULT_REGION"],
                   config=Config(signature_version="s3v4",s3={"addressing_style":"virtual"},
                                 connect_timeout=15,read_timeout=90,retries={"max_attempts":3,"mode":"standard"}))
    audit(s3,os.environ["AWS_S3_BUCKET_NAME"],os.environ["METALS_DATABASE_URL"])
    return 0

if __name__=="__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"RETENTION_AUDIT_FAILED type={type(exc).__name__} stage={str(exc) if isinstance(exc,RuntimeError) else 'unclassified'}",flush=True)
        raise SystemExit(1)
