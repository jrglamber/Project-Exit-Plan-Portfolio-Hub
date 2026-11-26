"""Independent, read-only verification of XAU cleanup receipts and storage.

No broker, production DB writes, archive object deletion, or trading code.
Only indexed count queries on the exact archived ID intervals. Audit records
are appended to the bucket; original cleanup receipts are never rewritten.
"""
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from archive import connection_env, file_hash
from incremental import manifest_read, manifest_write
from restore_verify import list_keys
from retention_audit import HOT_ID_WINDOW
from table_archive import TABLE, PREFIX, PAGE_IDS, BOUNDS_SQL, read_json, dump_page
from table_restore import PROOF_PREFIX, canonical_sha, validate_receipt, obtain_page

CLEANUP_PREFIX = f"{PREFIX}/cleanup-proof"
AUDIT_PREFIX = f"{PREFIX}/cleanup-audit"
STATE_KEY = f"{AUDIT_PREFIX}/state.json"
AUDIT_START = date(2026,11,26)
MAX_DAYS = 7  # Catch up in bounded batches if daily jobs were missed.
MAX_INTENTS_PER_DAY = 1000
FRESH_INTENT_MINUTES = 30
KEY = re.compile(re.escape(CLEANUP_PREFIX)+r"/\d{4}/\d{2}/\d{2}/(\d{20})-(\d{20})-[0-9a-f]{32}/(intent|outcome)\.json$")


def timestamp(value):
    t = datetime.fromisoformat(str(value).replace("Z","+00:00"))
    if t.tzinfo is None:
        raise RuntimeError("missing receipt timezone")
    return t.astimezone(timezone.utc)


def intent_records(s3,bucket,day):
    prefix=f"{CLEANUP_PREFIX}/{day:%Y/%m/%d}/"
    bases={}
    for key in list_keys(s3,bucket,prefix):
        m=KEY.fullmatch(key)
        if not m:
            raise RuntimeError("unexpected cleanup receipt key")
        base=key.rsplit("/",1)[0]
        bases.setdefault(base,set()).add(m[3])
        if len(bases)>MAX_INTENTS_PER_DAY:
            raise RuntimeError("cleanup receipt daily bound exceeded")
    for base,parts in sorted(bases.items()):
        if "intent" not in parts:
            raise RuntimeError("cleanup outcome without durable intent")
        key=base+"/intent.json"
        intent=manifest_read(s3,bucket,key)
        lo,hi=map(int,KEY.fullmatch(key).groups()[:2])
        if (not intent or intent.get("status")!="intent_only" or intent.get("table")!=TABLE
                or intent.get("first_id")!=lo or intent.get("last_id")!=hi
                or not 0<lo<=hi or hi-lo+1!=PAGE_IDS
                or type(intent.get("expected_rows")) is not int or not 0<intent["expected_rows"]<=PAGE_IDS
                or intent.get("archive_retained") is not True
                or timestamp(intent.get("created_utc")).date()!=day
                or not str(intent.get("restore_proof","")).startswith(PROOF_PREFIX+"/batches/")):
            raise RuntimeError("invalid cleanup intent identity")
        outcome=manifest_read(s3,bucket,base+"/outcome.json") if "outcome" in parts else None
        if "outcome" in parts and not outcome:
            raise RuntimeError("listed cleanup outcome missing")
        if outcome:
            for field,val in intent.items():
                if field!="status" and outcome.get(field)!=val:
                    raise RuntimeError("cleanup outcome differs from intent")
            if outcome.get("status")=="committed":
                if (outcome.get("rows_deleted")!=intent["expected_rows"]
                        or outcome.get("post_delete_target_rows")!=0 or outcome.get("byte_match") is not True
                        or type(outcome.get("latest_id_at_check")) is not int
                        or outcome.get("exclusive_cutoff_id")!=outcome["latest_id_at_check"]-HOT_ID_WINDOW
                        or hi>=outcome["exclusive_cutoff_id"]
                        or type(outcome.get("closed_links_locked")) is not int
                        or not 0<outcome["closed_links_locked"]<=intent["expected_rows"]
                        or timestamp(outcome.get("committed_utc"))<timestamp(intent["created_utc"])):
                    raise RuntimeError("invalid committed cleanup receipt")
            elif outcome.get("status")!="skipped" or outcome.get("rows_deleted")!=0:
                raise RuntimeError("invalid cleanup outcome")
        yield base,intent,outcome


def verified_page(s3,bucket,intent,cache):
    lo,hi=intent["first_id"],intent["last_id"]
    receipt=validate_receipt(manifest_read(s3,bucket,intent["archive_manifest"]),lo,hi,intent["archive_manifest"])
    if (receipt["sha256"]!=intent["archive_sha256"] or receipt["csv_sha256"]!=intent["csv_sha256"]
            or receipt["object_key"]!=intent["archive_object"] or receipt["row_count"]!=intent["expected_rows"]):
        raise RuntimeError("cleanup intent archive mismatch")
    key=intent["restore_proof"]
    upper=None
    seen=set()
    while key:
        if not key.startswith(PROOF_PREFIX+"/batches/") or key in seen:
            raise RuntimeError("invalid cleanup restore proof chain")
        seen.add(key)
        if key not in cache:
            cache[key]=manifest_read(s3,bucket,key)
        proof=cache[key]
        if (not proof or proof.get("proof_key")!=key or proof.get("status")!="verified"
                or proof.get("table")!=TABLE or proof.get("full_byte_roundtrip") is not True
                or proof.get("production_connections") is not False or proof.get("source_deleted") is not False
                or (upper is not None and proof["next_id"]!=upper)):
            raise RuntimeError("cleanup restore proof invalid")
        start=proof["batch_first_id"]
        next_id=proof["next_id"]
        if lo>=next_id:
            raise RuntimeError("cleanup interval beyond restore proof")
        cursor=start
        match=None
        for page in proof["pages"]:
            if page["first_id"]!=cursor or page["last_id"]<cursor:
                raise RuntimeError("restore proof page gap or overlap")
            cursor=page["last_id"]+1
            if page["first_id"]==lo and page["last_id"]==hi:
                match=page
        if cursor!=next_id:
            raise RuntimeError("restore proof bounds invalid")
        if match:
            if (match["manifest"]!=intent["archive_manifest"] or match["manifest_sha256"]!=canonical_sha(receipt)
                    or match["sha256"]!=receipt["sha256"] or match["csv_sha256"]!=receipt["csv_sha256"]
                    or match["rows"]!=receipt["row_count"]):
                raise RuntimeError("cleanup receipt differs from restore proof")
            with tempfile.TemporaryDirectory(prefix="pep-audit-archive-") as d:
                obtain_page(s3,bucket,receipt,Path(d))
            return receipt
        if lo>=start:
            raise RuntimeError("cleanup page not in restore proof")
        upper=start
        key=proof.get("previous_proof")
    raise RuntimeError("cleanup page missing from restore history")


def current_rows(env,lo,hi):
    if type(lo) is not int or type(hi) is not int or not 0<lo<=hi or hi-lo+1!=PAGE_IDS:
        raise RuntimeError("unsafe read-only audit interval")
    data=read_json(env,f"""SELECT json_build_object('read_only',current_setting('transaction_read_only'),
       'rows',count(*)) FROM ONLY public.{TABLE} WHERE id>={lo} AND id<={hi}""")
    if data.get("read_only")!="on":
        raise RuntimeError("read-only cleanup audit proof missing")
    return data["rows"]


def scan_dates(prior,now):
    start=AUDIT_START if prior is None else date.fromisoformat(prior["next_day"])
    if start<AUDIT_START or start>now.date():
        raise RuntimeError("invalid cleanup audit checkpoint")
    end=min(now.date(),start+timedelta(days=MAX_DAYS-1))
    return [start+timedelta(days=i) for i in range((end-start).days+1)]


def inspect(s3,bucket,url,now=None):
    now=now or datetime.now(timezone.utc)
    env=connection_env(url)
    env["PGAPPNAME"]="pep-readonly-cleanup-audit"
    env["PGOPTIONS"]="-c default_transaction_read_only=on -c statement_timeout=15000 -c lock_timeout=1000"
    bounds=read_json(env,BOUNDS_SQL)
    if bounds.get("read_only")!="on" or bounds.get("oldest") is None or bounds.get("latest") is None:
        raise RuntimeError("cleanup audit source bounds missing")
    prior=manifest_read(s3,bucket,STATE_KEY)
    if prior is not None and (prior.get("status")!="verified" or prior.get("source_deleted") is not False):
        raise RuntimeError("invalid prior cleanup audit")
    days=scan_dates(prior,now)
    records=[r for d in days for r in intent_records(s3,bucket,d)]
    cache={}
    committed={}
    verified=[]
    issues=[]
    skipped=pending=unchanged=0
    pending_days=[]
    for base,intent,outcome in records:
        if outcome and outcome["status"]=="committed":
            receipt=verified_page(s3,bucket,intent,cache)
            ident=(intent["first_id"],intent["last_id"],intent["archive_sha256"])
            if ident in committed:
                issues.append({"intent":base,"reason":"duplicate_committed_page"})
                continue
            n=current_rows(env,ident[0],ident[1])
            if n!=0:
                issues.append({"intent":base,"reason":"committed_page_still_has_source_rows","rows":n})
                continue
            committed[ident]=base
            verified.append({"receipt":base+"/outcome.json","first_id":ident[0],"last_id":ident[1],
                "rows":receipt["row_count"],"archive_object":receipt["object_key"],"archive_sha256":ident[2],
                "source_rows_remaining":0})
        elif outcome:
            skipped+=1
    for base,intent,outcome in records:
        if outcome:
            continue
        if now-timestamp(intent["created_utc"])<timedelta(minutes=FRESH_INTENT_MINUTES):
            pending+=1
            pending_days.append(timestamp(intent["created_utc"]).date())
            continue
        ident=(intent["first_id"],intent["last_id"],intent["archive_sha256"])
        if ident in committed:
            unchanged+=1  # A later verified retry superseded this incomplete intent.
            continue
        receipt=verified_page(s3,bucket,intent,cache)
        n=current_rows(env,ident[0],ident[1])
        match=False
        if n==receipt["row_count"]:
            with tempfile.TemporaryDirectory(prefix="pep-audit-source-") as d:
                path=Path(d)/"live.csv"
                dump_page(env,ident[0],ident[1],path)
                match=path.stat().st_size==receipt["csv_bytes"] and file_hash(path)==receipt["csv_sha256"]
        if match:
            unchanged+=1  # No deletion observed; don't manufacture a commit receipt.
        else:
            issues.append({"intent":base,"reason":"unresolved_intent_source_changed_or_absent","rows":n})
    archive=manifest_read(s3,bucket,f"{PREFIX}/checkpoint.json")
    restore=manifest_read(s3,bucket,f"{PROOF_PREFIX}/latest.json")
    if (not archive or not restore or archive.get("status")!="verified" or restore.get("status")!="verified"
            or archive.get("table")!=TABLE or restore.get("table")!=TABLE
            or restore.get("full_byte_roundtrip") is not True
            or restore.get("production_connections") is not False
            or restore.get("first_archived_id")!=archive.get("first_archived_id")
            or not restore["next_id"]<=archive["next_id"]
            or restore["total_restored_rows"]>archive["total_verified_rows"]):
        raise RuntimeError("archive and restore checkpoints inconsistent")
    if bounds["oldest"]>archive["next_id"]:
        issues.append({"reason":"source_advanced_beyond_archive_checkpoint"})
    cutoff=bounds["latest"]-HOT_ID_WINDOW
    summary={"status":"review" if issues else "verified","table":TABLE,"source_deleted":False,
        "checked_utc":now.isoformat(),"first_day":days[0].isoformat(),"last_day":days[-1].isoformat(),
        "oldest_live_id":bounds["oldest"],"latest_live_id":bounds["latest"],"exclusive_cutoff":cutoff,
        "cold_id_span":max(0,cutoff-bounds["oldest"]), "archive_next_id":archive["next_id"],
        "restore_next_id":restore["next_id"],"archived_rows":archive["total_verified_rows"],
        "archived_bytes":archive["total_verified_bytes"],"restored_rows":restore["total_restored_rows"],
        "committed_pages_verified":len(verified),"committed_rows_verified":sum(v["rows"] for v in verified),
        "verified_commits":verified,"skipped_intents":skipped,"pending_recent_intents":pending,
        "no_deletion_or_superseded_intents":unchanged,"issues":issues}
    key=f"{AUDIT_PREFIX}/{now:%Y/%m/%d}/{now:%H%M%S%f}.json"
    manifest_write(s3,bucket,key,summary)
    manifest_write(s3,bucket,f"{AUDIT_PREFIX}/latest.json",summary)
    if not issues:
        # Re-scan today's open interval next time, including receipts completed
        # just after this listing. Never skip days after a missed run.
        next_day=min([now.date(),days[-1]+timedelta(days=1)]+pending_days)
        manifest_write(s3,bucket,STATE_KEY,{"status":"verified","source_deleted":False,
            "next_day":next_day.isoformat(),"report":key})
    print(f"CLEANUP_AUDIT_{'REVIEW' if issues else 'VERIFIED'} first_day={days[0]} last_day={days[-1]} "
          f"committed_pages={len(verified)} committed_rows={summary['committed_rows_verified']} "
          f"archived_rows={summary['archived_rows']} restored_rows={summary['restored_rows']} "
          f"cold_id_span={summary['cold_id_span']} skipped={skipped} pending={pending} "
          f"issues={len(issues)} source_deleted=false",flush=True)
    return summary


def main():
    import boto3
    from botocore.config import Config
    endpoint=os.environ["AWS_ENDPOINT_URL"]
    if urlsplit(endpoint).scheme!="https":
        raise ValueError("bucket endpoint must use HTTPS")
    s3=boto3.client("s3",endpoint_url=endpoint,region_name=os.environ["AWS_DEFAULT_REGION"],
        config=Config(signature_version="s3v4",s3={"addressing_style":"virtual"},connect_timeout=15,
                      read_timeout=90,retries={"max_attempts":3,"mode":"standard"}))
    report=inspect(s3,os.environ["AWS_S3_BUCKET_NAME"],os.environ["METALS_DATABASE_URL"])
    return int(bool(report["issues"]))
