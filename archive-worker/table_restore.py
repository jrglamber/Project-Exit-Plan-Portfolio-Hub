"""Restore newly archived review pages to a disposable socket-only database.

Every page is independently downloaded, decompressed, validated, COPY-restored
with its original column types and re-exported. Only byte-identical round trips
get proofs. No production credentials and no live database connections.
"""
import gzip
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime,timezone
from pathlib import Path

from archive import file_hash
from incremental import manifest_read, manifest_write
from restore_verify import command, list_keys, restore_and_count
from table_archive import TABLE, PREFIX, PAGE_IDS, MAX_OUTPUT_BYTES, validate_csv, dump_page

PROOF_PREFIX=f"{PREFIX}/restore-proof"
MAX_PAGES_PER_RUN=200
IDENT=re.compile(r"^[a-z_][a-z0-9_]*$")
SHA=re.compile(r"^[0-9a-f]{64}$")
KEY=re.compile(re.escape(PREFIX)+r"/manifests/(\d{20})-(\d{20})-([0-9a-f]{20})\.json$")
TYPES={"smallint","integer","bigint","real","double precision","numeric","text",
       "boolean","date","timestamp without time zone","timestamp with time zone"}


def canonical_sha(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def create_sql(columns):
    if not columns or not isinstance(columns,list):
        raise RuntimeError("missing source columns")
    names=[c["name"] for c in columns]
    if len(set(names))!=len(names) or names.count("id")!=1:
        raise RuntimeError("duplicate or missing source column")
    parts=[]
    for c in columns:
        name,kind=c["name"],c["type"]
        if not IDENT.fullmatch(name) or kind not in TYPES:
            raise RuntimeError("unsupported source column or type")
        if name=="id" and kind not in {"smallint","integer","bigint"}:
            raise RuntimeError("invalid review ID type")
        parts.append(f'"{name}" {kind}'+(" PRIMARY KEY" if name=="id" else ""))
    return f"CREATE TABLE public.{TABLE} ("+", ".join(parts)+");"


def select_keys(keys,start,stop,max_pages=MAX_PAGES_PER_RUN):
    """Select one continuous archived ID prefix; never infer coverage across gaps."""
    by_start={}
    for key in keys:
        m=KEY.fullmatch(key)
        if not m:
            continue
        lo,hi=int(m[1]),int(m[2])
        if hi<start or lo>=stop:
            continue
        if lo<start or hi>=stop or not 0<=hi-lo<PAGE_IDS:
            raise RuntimeError("archive interval crosses checkpoint boundary")
        by_start.setdefault(lo,[]).append((hi,key))
    current=start
    result=[]
    while current<stop and len(result)<max_pages:
        choices=by_start.get(current,[])
        if len(choices)!=1:
            raise RuntimeError("archive gap or ambiguous page")
        hi,key=choices[0]
        if any(current<lo<=hi for lo in by_start):
            raise RuntimeError("overlapping archive pages")
        result.append((current,hi,key))
        current=hi+1
    return result


def validate_receipt(receipt,start,end,key):
    if not isinstance(receipt,dict):
        raise RuntimeError("archive receipt missing")
    sha=receipt.get("sha256","")
    if (not SHA.fullmatch(sha or "") or not SHA.fullmatch(receipt.get("csv_sha256","") or "")
            or receipt.get("status")!="verified" or receipt.get("table")!=TABLE
            or receipt.get("asset")!="metals" or receipt.get("format")!="postgresql-copy-csv-gzip-v1"
            or receipt.get("source_deleted") is not False or receipt.get("copy_null")!="\\N"
            or receipt.get("first_id_requested")!=start or receipt.get("last_id_requested")!=end
            or not isinstance(receipt.get("row_count"),int) or not 0<=receipt["row_count"]<=end-start+1
            or not isinstance(receipt.get("bytes"),int) or not 0<receipt["bytes"]<=MAX_OUTPUT_BYTES
            or not isinstance(receipt.get("csv_bytes"),int) or not 0<receipt["csv_bytes"]<=MAX_OUTPUT_BYTES):
        raise RuntimeError("invalid archived page receipt")
    stem=f"{start:020d}-{end:020d}-{sha[:20]}"
    if (key!=f"{PREFIX}/manifests/{stem}.json" or
            receipt.get("object_key")!=f"{PREFIX}/pages/{stem}.csv.gz"):
        raise RuntimeError("archive object does not match receipt identity")
    create_sql(receipt.get("columns"))
    return receipt


def obtain_page(s3,bucket,r,root):
    packed=root/"page.csv.gz"
    raw=root/"page.csv"
    s3.download_file(bucket,r["object_key"],str(packed))
    if packed.stat().st_size!=r["bytes"] or file_hash(packed)!=r["sha256"]:
        raise RuntimeError("archive page compressed checksum mismatch")
    total=0
    with gzip.open(packed,"rb") as src,open(raw,"wb") as out:
        while True:
            block=src.read(1024*1024)
            if not block:
                break
            total+=len(block)
            if total>r["csv_bytes"] or total>MAX_OUTPUT_BYTES:
                raise RuntimeError("archive page expands beyond verified size")
            out.write(block)
    if total!=r["csv_bytes"] or file_hash(raw)!=r["csv_sha256"]:
        raise RuntimeError("archive page raw checksum mismatch")
    count,first,last=validate_csv(raw,r["columns"],r["first_id_requested"],r["last_id_requested"])
    if (count,first,last)!=(r["row_count"],r["first_present_id"],r["last_present_id"]):
        raise RuntimeError("archive page ID/count mismatch")
    return raw


def restore_page(env,raw,receipt,root):
    columns=", ".join('"'+c["name"]+'"' for c in receipt["columns"])
    sql=f"COPY public.{TABLE} ({columns}) FROM STDIN WITH (FORMAT CSV, HEADER TRUE, NULL '\\N')"
    with open(raw,"rb") as inp,tempfile.TemporaryFile() as err:
        proc=subprocess.run(["psql","-X","-Atq","-v","ON_ERROR_STOP=1","-c",sql],
                            env=env,stdin=inp,stdout=subprocess.DEVNULL,stderr=err,timeout=90)
    if proc.returncode:
        raise RuntimeError("CSV restore failed")
    output=root/"roundtrip.csv"
    dump_page(env,receipt["first_id_requested"],receipt["last_id_requested"],output)
    if output.stat().st_size!=receipt["csv_bytes"] or file_hash(output)!=receipt["csv_sha256"]:
        raise RuntimeError("restored content differs from original CSV")
    return receipt["row_count"]


def verify_table(s3,bucket):
    archive=manifest_read(s3,bucket,f"{PREFIX}/checkpoint.json")
    if not archive:
        raise RuntimeError("no archived XAU review checkpoint")
    if (archive.get("status")!="verified" or archive.get("table")!=TABLE
            or archive.get("source_deleted") is not False
            or not isinstance(archive.get("first_archived_id"),int)
            or not isinstance(archive.get("next_id"),int)
            or archive["next_id"]<=archive["first_archived_id"]):
        raise RuntimeError("invalid XAU review checkpoint")
    prior=manifest_read(s3,bucket,f"{PROOF_PREFIX}/latest.json")
    first=archive["first_archived_id"]
    start=first
    restored_before=0
    if prior:
        if (prior.get("table")!=TABLE or prior.get("status")!="verified"
                or prior.get("first_archived_id")!=first
                or not isinstance(prior.get("next_id"),int) or not first<prior["next_id"]<=archive["next_id"]
                or prior.get("full_byte_roundtrip") is not True
                or prior.get("production_connections") is not False
                or not str(prior.get("proof_key","")).startswith(PROOF_PREFIX+"/batches/")):
            raise RuntimeError("invalid prior restore proof")
        if manifest_read(s3,bucket,prior["proof_key"])!=prior:
            raise RuntimeError("restore proof pointer mismatch")
        start=prior["next_id"]
        restored_before=prior["total_restored_rows"]
    if start==archive["next_id"]:
        if restored_before!=archive["total_verified_rows"]:
            raise RuntimeError("restore and archive checkpoint totals differ")
        print(f"TABLE_RESTORE_ALREADY_VERIFIED rows={restored_before} next_id={start}",flush=True)
        return prior
    selected=select_keys(list_keys(s3,bucket,f"{PREFIX}/manifests/"),start,archive["next_id"])
    receipts=[(lo,hi,key,validate_receipt(manifest_read(s3,bucket,key),lo,hi,key))
              for lo,hi,key in selected]
    if not receipts:
        raise RuntimeError("no continuous verified archive pages")
    cols=receipts[0][3]["columns"]
    if any(r["columns"]!=cols for _,_,_,r in receipts):
        raise RuntimeError("source schema changed within restore batch")
    expected=sum(r["row_count"] for _,_,_,r in receipts)
    stop=receipts[-1][1]+1
    if stop==archive["next_id"] and (restored_before+expected!=archive["total_verified_rows"]
            or receipts[-1][2]!=archive["last_manifest"]):
        raise RuntimeError("archive checkpoint totals or last page mismatch")
    print(f"TABLE_RESTORE_STARTED pages={len(receipts)} first_id={start} last_id={stop-1} rows={expected}",flush=True)
    with tempfile.TemporaryDirectory(prefix="pep-review-restore-") as d:
        root=Path(d)
        def copy_restore(env):
            env=dict(env,PGDATABASE="restored",PGOPTIONS="-c statement_timeout=90000 -c timezone=UTC")
            command(["psql","-X","-Atq","-v","ON_ERROR_STOP=1","-c",create_sql(cols)],env,timeout=90)
            for i,(_,_,key,r) in enumerate(receipts,1):
                raw=obtain_page(s3,bucket,r,root)
                restore_page(env,raw,r,root)
                if i%10==0 or i==len(receipts):
                    print(f"TABLE_RESTORE_PROGRESS pages={i}/{len(receipts)}",flush=True)
        actual=restore_and_count(None,{f"public.{TABLE}":expected},root,restore_sql=copy_restore)
        if actual.get(f"public.{TABLE}")!=expected:
            raise RuntimeError("restored row count mismatch")
    now=datetime.now(timezone.utc)
    key=f"{PROOF_PREFIX}/batches/{start:020d}-{stop-1:020d}-{now:%Y%m%dT%H%M%S%f}.json"
    proofs=[{"manifest":k,"manifest_sha256":canonical_sha(r),"sha256":r["sha256"],
             "csv_sha256":r["csv_sha256"],"first_id":lo,"last_id":hi,"rows":r["row_count"]}
            for lo,hi,k,r in receipts]
    proof={"status":"verified","format":"xau-review-restore-v1","table":TABLE,
           "first_archived_id":first,"batch_first_id":start,"next_id":stop,
           "batch_restored_rows":expected,"total_restored_rows":restored_before+expected,
           "pages_verified":len(receipts),"pages":proofs,"verified_utc":now.isoformat(),
           "archive_checkpoint_sha256":canonical_sha(archive),"previous_proof":prior.get("proof_key") if prior else None,
           "proof_key":key,"full_byte_roundtrip":True,"production_connections":False,"source_deleted":False}
    manifest_write(s3,bucket,key,proof)
    manifest_write(s3,bucket,f"{PROOF_PREFIX}/latest.json",proof)
    print(f"TABLE_RESTORE_VERIFIED pages={len(receipts)} batch_rows={expected} "
          f"total_rows={proof['total_restored_rows']} last_id={stop-1} full_byte_roundtrip=true",flush=True)
    return proof


def main():
    import boto3
    from botocore.config import Config
    from urllib.parse import urlsplit
    if any(k.endswith("DATABASE_URL") for k in os.environ):
        raise RuntimeError("table restore cannot have production database credentials")
    endpoint=os.environ["AWS_ENDPOINT_URL"]
    if urlsplit(endpoint).scheme!="https":
        raise ValueError("S3 endpoint must be HTTPS")
    s3=boto3.client("s3",endpoint_url=endpoint,region_name=os.environ["AWS_DEFAULT_REGION"],
                   config=Config(signature_version="s3v4",s3={"addressing_style":"virtual"},
                                 connect_timeout=15,read_timeout=90,retries={"max_attempts":3,"mode":"standard"}))
    verify_table(s3,os.environ["AWS_S3_BUCKET_NAME"])
    return 0


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"TABLE_RESTORE_FAILED type={type(exc).__name__} stage={str(exc) if isinstance(exc,RuntimeError) else 'unclassified'}",flush=True)
        raise SystemExit(1)
