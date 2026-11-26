"""Catalog-only storage usage by archive prefix. Never fetches payloads or deletes."""
import os
from datetime import datetime,timezone
from urllib.parse import urlsplit
from incremental import manifest_write

PREFIX="project-exit-plan/"
REPORT_PREFIX=PREFIX+"inventory/v1/bucket"
MAX_OBJECTS=250_000

def category(key):
    for path,label in (("v1/","full_backup_and_proofs"),("v2/","incremental_archive"),
                       ("table-archive/","table_archive_and_proofs"),("inventory/","inventory")):
        if key.startswith(PREFIX+path):
            return label
    return "other_project_archive"

def inventory(s3,bucket):
    groups={}
    total_objects=total_bytes=0
    complete=True
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket,Prefix=PREFIX):
        for obj in page.get("Contents",[]):
            if total_objects>=MAX_OBJECTS:
                complete=False
                break
            group=groups.setdefault(category(obj["Key"]),{"objects":0,"bytes":0})
            group["objects"]+=1
            group["bytes"]+=obj["Size"]
            total_objects+=1
            total_bytes+=obj["Size"]
        if not complete:
            break
    now=datetime.now(timezone.utc)
    report={"status":"verified" if complete else "bounded_partial","complete":complete,
            "reported_utc":now.isoformat(),"prefix":PREFIX,"objects":total_objects,
            "bytes":total_bytes,"categories":groups,"source_deleted":False,"bucket_objects_deleted":False}
    manifest_write(s3,bucket,f"{REPORT_PREFIX}/{now:%Y/%m/%d}/{now:%H%M%S%f}.json",report)
    manifest_write(s3,bucket,f"{REPORT_PREFIX}/latest.json",report)
    print(f"BUCKET_INVENTORY status={report['status']} objects={total_objects} bytes={total_bytes} "
          +" ".join(f"{k}_bytes={v['bytes']}" for k,v in sorted(groups.items())),flush=True)
    return report
def main():
    import boto3
    from botocore.config import Config
    endpoint=os.environ["AWS_ENDPOINT_URL"]
    if urlsplit(endpoint).scheme!="https":
        raise ValueError("bucket endpoint must use HTTPS")
    s3=boto3.client("s3",endpoint_url=endpoint,region_name=os.environ["AWS_DEFAULT_REGION"],
        config=Config(signature_version="s3v4",s3={"addressing_style":"virtual"},connect_timeout=15,
                      read_timeout=90,retries={"max_attempts":3,"mode":"standard"}))
    inventory(s3,os.environ["AWS_S3_BUCKET_NAME"])
    return 0
