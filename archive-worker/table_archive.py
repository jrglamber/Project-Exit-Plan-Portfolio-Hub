"""Archive-only replacement for the legacy XAU manager-review deletion job.

Rows are read in small ordered ID intervals from the append-only review table,
stored as compressed PostgreSQL COPY CSV, read back and checked. The private
checkpoint advances only after a verified data object and immutable manifest.
There is deliberately no DELETE, UPDATE, TRUNCATE, or DDL path.
"""
import csv
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit

from archive import connection_env, file_hash, object_hash, run
from incremental import manifest_read, manifest_write

TABLE="metals_xau_live_manager_reviews"
PREFIX=f"project-exit-plan/table-archive/v1/metals/{TABLE}"
HOT_ID_WINDOW=750_000  # archive ahead of the former one-million-ID deletion boundary
PAGE_IDS=5000
MAX_PAGES=8
MAX_OUTPUT_BYTES=256*1024*1024
BOUNDS_SQL=f"""SELECT json_build_object('read_only',current_setting('transaction_read_only'),
 'oldest',min(id),'latest',max(id)) FROM public.{TABLE}"""
SCHEMA_SQL=f"""SELECT json_agg(json_build_object('name',attname,
 'type',format_type(atttypid,atttypmod)) ORDER BY attnum)
 FROM pg_attribute WHERE attrelid='public.{TABLE}'::regclass
 AND attnum>0 AND NOT attisdropped"""


def read_json(env,sql):
    return json.loads(run(["psql","-X","-Atq","--no-password","-v","ON_ERROR_STOP=1",
                           "-c",sql],env,timeout=40))


def validate_csv(path,columns,start,end):
    csv.field_size_limit(64*1024*1024)
    count=0
    first=last=None
    with open(path,encoding="utf-8",newline="") as f:
        reader=csv.reader(f)
        header=next(reader)
        if header!=[c["name"] for c in columns] or header.count("id")!=1:
            raise RuntimeError("COPY CSV columns differ from source schema")
        pos=header.index("id")
        for row in reader:
            if len(row)!=len(header):
                raise RuntimeError("COPY CSV field count mismatch")
            ident=int(row[pos])
            if not start<=ident<=end or (last is not None and ident<=last):
                raise RuntimeError("COPY CSV IDs invalid or repeated")
            first=ident if first is None else first
            last=ident
            count+=1
    return count,first,last


def dump_page(env,start,end,path):
    if not (0<start<=end and end-start<PAGE_IDS):
        raise ValueError("invalid archive interval")
    sql=(f"COPY (SELECT * FROM public.{TABLE} WHERE id >= {start} AND id <= {end} "
         "ORDER BY id) TO STDOUT WITH (FORMAT CSV, HEADER TRUE, NULL '\\N')")
    with open(path,"wb") as out, tempfile.TemporaryFile() as err:
        p=subprocess.run(["psql","-X","-Atq","--no-password","-v","ON_ERROR_STOP=1","-c",sql],
                         env=env,stdout=out,stderr=err,timeout=40)
    if p.returncode:
        raise RuntimeError("COPY read-only page failed")
    if path.stat().st_size>MAX_OUTPUT_BYTES:
        raise RuntimeError("COPY page exceeds safety byte limit")


def page_receipt(s3,bucket,env,columns,start,end):
    with tempfile.TemporaryDirectory(prefix="pep-xau-review-") as d:
        raw=Path(d)/"rows.csv"
        compressed=Path(d)/"rows.csv.gz"
        dump_page(env,start,end,raw)
        count,first,last=validate_csv(raw,columns,start,end)
        plain_sha=file_hash(raw)
        with open(raw,"rb") as src,open(compressed,"wb") as out:
            with gzip.GzipFile(fileobj=out,mode="wb",filename="",mtime=0,compresslevel=6) as z:
                shutil.copyfileobj(src,z,1024*1024)
        sha=file_hash(compressed)
        key=f"{PREFIX}/pages/{start:020d}-{end:020d}-{sha[:20]}.csv.gz"
        size=compressed.stat().st_size
        s3.upload_file(str(compressed),bucket,key,ExtraArgs={
            "ContentType":"application/gzip","Metadata":{"sha256":sha,"table":TABLE}})
        actual_sha,actual_size=object_hash(s3,bucket,key)
        if (actual_sha,actual_size)!=(sha,size):
            raise RuntimeError("bucket page failed byte verification")
        receipt={"format":"postgresql-copy-csv-gzip-v1","asset":"metals","table":TABLE,
            "status":"verified","object_key":key,"sha256":sha,"bytes":size,
            "csv_sha256":plain_sha,"csv_bytes":raw.stat().st_size,
            "first_id_requested":start,"last_id_requested":end,"first_present_id":first,
            "last_present_id":last,"row_count":count,"columns":columns,"copy_null":"\\N",
            "source_deleted":False,"verified_utc":datetime.now(timezone.utc).isoformat()}
        manifest=f"{PREFIX}/manifests/{start:020d}-{end:020d}-{sha[:20]}.json"
        manifest_write(s3,bucket,manifest,receipt)
        return receipt,manifest


def plan_pages(oldest,latest,checkpoint,window=HOT_ID_WINDOW,max_pages=MAX_PAGES):
    if oldest is None or latest is None:
        return []
    start=oldest if checkpoint is None else checkpoint["next_id"]
    if checkpoint and (checkpoint.get("table")!=TABLE or checkpoint.get("status")!="verified"
                       or not isinstance(start,int) or start<=0
                       or checkpoint.get("source_deleted") is not False):
        raise RuntimeError("invalid archive checkpoint")
    if checkpoint and oldest>start:
        # Never silently jump over possible deletions since the previous page.
        raise RuntimeError("source history advanced beyond verified checkpoint")
    cutoff=latest-window
    pages=[]
    while start<=cutoff and len(pages)<max_pages:
        end=min(start+PAGE_IDS-1,cutoff)
        # Archive complete bounded intervals; leave a small hot margin until
        # enough rows arrive. On the first pass allow a short initial interval.
        if end-start+1<PAGE_IDS and checkpoint is not None:
            break
        pages.append((start,end))
        start=end+1
    return pages


def archive_reviews(s3,bucket,url):
    env=connection_env(url)
    env["PGAPPNAME"]="pep-xau-review-archive-only"
    env["PGOPTIONS"]="-c default_transaction_read_only=on -c statement_timeout=25000 -c lock_timeout=1000"
    bounds=read_json(env,BOUNDS_SQL)
    if bounds.get("read_only")!="on":
        raise RuntimeError("read-only source proof missing")
    columns=read_json(env,SCHEMA_SQL)
    if not columns or sum(c["name"]=="id" for c in columns)!=1:
        raise RuntimeError("source schema invalid")
    checkpoint_key=f"{PREFIX}/checkpoint.json"
    checkpoint=manifest_read(s3,bucket,checkpoint_key)
    if checkpoint:
        prior=manifest_read(s3,bucket,checkpoint["last_manifest"])
        if not prior or prior.get("status")!="verified" or prior.get("last_id_requested")!=checkpoint["next_id"]-1:
            raise RuntimeError("previous archive receipt missing")
        if object_hash(s3,bucket,prior["object_key"])!=(prior["sha256"],prior["bytes"]):
            raise RuntimeError("previous archive page integrity failure")
    pages=plan_pages(bounds["oldest"],bounds["latest"],checkpoint)
    print(f"TABLE_ARCHIVE_BOUNDS oldest={bounds['oldest']} newest={bounds['latest']} "
          f"planned_pages={len(pages)} mode=copy_only",flush=True)
    for start,end in pages:
        receipt,key=page_receipt(s3,bucket,env,columns,start,end)
        checkpoint={"status":"verified","format":"xau-review-archive-checkpoint-v1",
                    "table":TABLE,"source_deleted":False,"first_archived_id":(
                        checkpoint["first_archived_id"] if checkpoint else start),
                    "next_id":end+1,"total_verified_rows":(
                        (checkpoint["total_verified_rows"] if checkpoint else 0)+receipt["row_count"]),
                    "total_verified_bytes":(
                        (checkpoint["total_verified_bytes"] if checkpoint else 0)+receipt["bytes"]),
                    "last_manifest":key,"updated_utc":datetime.now(timezone.utc).isoformat()}
        manifest_write(s3,bucket,checkpoint_key,checkpoint)
        print(f"TABLE_ARCHIVE_PAGE_VERIFIED first_id={start} last_id={end} rows={receipt['row_count']} "
              f"bytes={receipt['bytes']} cumulative_rows={checkpoint['total_verified_rows']}",flush=True)
    print(f"TABLE_ARCHIVE_COMPLETE pages={len(pages)} source_deleted=false "
          f"total_verified_rows={checkpoint['total_verified_rows'] if checkpoint else 0}",flush=True)


def main():
    import boto3
    from botocore.config import Config
    endpoint=os.environ["AWS_ENDPOINT_URL"]
    if urlsplit(endpoint).scheme!="https":
        raise ValueError("bucket endpoint must be HTTPS")
    bucket=os.environ["AWS_S3_BUCKET_NAME"]
    s3=boto3.client("s3",endpoint_url=endpoint,region_name=os.environ["AWS_DEFAULT_REGION"],
                   config=Config(signature_version="s3v4",s3={"addressing_style":"virtual"},
                                 connect_timeout=15,read_timeout=90,retries={"max_attempts":3,"mode":"standard"}))
    archive_reviews(s3,bucket,os.environ["DATABASE_URL"])
    return 0


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"TABLE_ARCHIVE_FAILED type={type(exc).__name__}",flush=True)
        raise SystemExit(1)
