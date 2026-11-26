"""Bounded read-only live catalog inventory for archive planning.

No row deletion, table scans, broker access, or public HTTP server. Database
credentials are only private Railway references. Reports contain metadata and
aggregate storage/row estimates, never trade payloads.
"""
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

from archive import DBS, connection_env, run
from incremental import manifest_write

PREFIX = "project-exit-plan/inventory/v1"

CATALOG_SQL = """
SELECT json_build_object(
 'read_only', current_setting('transaction_read_only'),
 'db_bytes', pg_database_size(current_database()),
 'stats_reset_utc', (SELECT stats_reset FROM pg_stat_database WHERE datname=current_database()),
 'tables', COALESCE((
  SELECT json_agg(row_to_json(t) ORDER BY t.total_bytes DESC)
  FROM (
   SELECT s.schemaname AS schema, s.relname AS name, pg_total_relation_size(s.relid) AS total_bytes,
       pg_table_size(s.relid) AS heap_toast_bytes, pg_indexes_size(s.relid) AS index_bytes,
       s.n_live_tup AS live_rows_estimate, s.n_dead_tup AS dead_rows_estimate,
       s.n_tup_ins AS inserts_since_reset, s.n_tup_upd AS updates_since_reset,
       s.n_tup_del AS deletes_since_reset, s.seq_scan, s.idx_scan,
       s.last_autovacuum, s.last_autoanalyze,
       (SELECT json_agg(json_build_object('name',a.attname,'type',format_type(a.atttypid,a.atttypmod),'not_null',a.attnotnull)
                        ORDER BY a.attnum)
        FROM pg_attribute a WHERE a.attrelid=s.relid AND a.attnum>0 AND NOT a.attisdropped) AS columns,
       (SELECT json_agg(json_build_object('name',ic.relname,'definition',pg_get_indexdef(i.indexrelid),
                        'primary',i.indisprimary,'unique',i.indisunique,'valid',i.indisvalid)
                        ORDER BY ic.relname)
        FROM pg_index i JOIN pg_class ic ON ic.oid=i.indexrelid WHERE i.indrelid=s.relid) AS indexes,
       (SELECT json_agg(json_build_object('direction',CASE WHEN con.conrelid=s.relid THEN 'outgoing' ELSE 'incoming' END,
                   'name',con.conname,'definition',pg_get_constraintdef(con.oid),
                   'source',con.conrelid::regclass::text,'target',con.confrelid::regclass::text))
        FROM pg_constraint con WHERE con.contype='f' AND (con.conrelid=s.relid OR con.confrelid=s.relid)) AS foreign_keys
   FROM pg_stat_user_tables s
   WHERE s.schemaname NOT IN ('pg_catalog','information_schema')
  ) t
 ),'[]'::json)
)
"""

HOUSEKEEPING_SQL = """
SELECT json_build_object('oldest_id',min(id),'latest_id',max(id))
FROM public.metals_xau_live_manager_reviews
"""


def read_json(env, sql):
    result = run(["psql", "-X", "-A", "-t", "-q", "--no-password",
                  "-v", "ON_ERROR_STOP=1", "-c", sql], env=env, timeout=40)
    return json.loads(result)


def inventory(asset, url):
    env = connection_env(url)
    env["PGAPPNAME"] = "pep-readonly-archive-inventory"
    env["PGOPTIONS"] = "-c default_transaction_read_only=on -c statement_timeout=25000 -c lock_timeout=1000"
    data = read_json(env, CATALOG_SQL)
    if data.get("read_only") != "on" or not data.get("tables"):
        raise RuntimeError("read-only catalog proof missing")
    data.update(asset=asset, captured_utc=datetime.now(timezone.utc).isoformat(),
                source_deleted=False, mode="catalog_metadata_only")
    if asset == "metals":
        # Only use min/max on an existing valid single-column primary key.
        table = next((t for t in data["tables"] if t["schema"]=="public"
                      and t["name"]=="metals_xau_live_manager_reviews"), None)
        import re
        if table and any(i["primary"] and i["valid"] and re.search(r"USING btree \(id\)$",i["definition"])
                         for i in table["indexes"] or []):
            data["housekeeping_id_bounds"] = read_json(env,HOUSEKEEPING_SQL)
    return data


def log_summary(data):
    print("INVENTORY_ASSET " + json.dumps({k:data[k] for k in (
        "asset","captured_utc","db_bytes","stats_reset_utc","read_only","source_deleted")},separators=(",",":")),flush=True)
    for t in data["tables"]:
        dates = [c["name"] for c in t["columns"] or [] if any(s in c["name"].lower() for s in
                 ("timestamp","time_utc","_at","date","candle","_time","_utc"))]
        row = {k:t[k] for k in ("schema","name","total_bytes","heap_toast_bytes","index_bytes",
               "live_rows_estimate","dead_rows_estimate","inserts_since_reset","updates_since_reset","deletes_since_reset")}
        row.update(asset=data["asset"],date_columns=dates,foreign_keys=len(t["foreign_keys"] or []),
                   primary_keys=[i["definition"] for i in t["indexes"] or [] if i["primary"]])
        print("INVENTORY_TABLE " + json.dumps(row,separators=(",",":")),flush=True)
    if "housekeeping_id_bounds" in data:
        print("INVENTORY_HOUSEKEEPING " + json.dumps(data["housekeeping_id_bounds"],separators=(",",":")),flush=True)


def main():
    import boto3
    from botocore.config import Config
    endpoint=os.environ["AWS_ENDPOINT_URL"]
    if urlsplit(endpoint).scheme!="https":
        raise ValueError("bucket endpoint must be HTTPS")
    bucket=os.environ["AWS_S3_BUCKET_NAME"]
    s3=boto3.client("s3",endpoint_url=endpoint,region_name=os.environ["AWS_DEFAULT_REGION"],
                    config=Config(signature_version="s3v4",s3={"addressing_style":"virtual"},
                                  connect_timeout=15,read_timeout=60,retries={"max_attempts":3,"mode":"standard"}))
    failures=[]
    now=datetime.now(timezone.utc)
    for asset,var in DBS.items():
        try:
            data=inventory(asset,os.environ[var])
            key=f"{PREFIX}/{asset}/{now:%Y/%m/%d}/{now:%H%M%S%f}.json"
            manifest_write(s3,bucket,key,data)
            manifest_write(s3,bucket,f"{PREFIX}/{asset}/latest.json",data)
            log_summary(data)
            print(f"INVENTORY_VERIFIED asset={asset} tables={len(data['tables'])} key={key}",flush=True)
        except Exception as exc:
            print(f"INVENTORY_FAILED asset={asset} type={type(exc).__name__}",flush=True)
            failures.append(asset)
    if failures:
        return 1
    print("INVENTORY_JOB_COMPLETE assets=indices,metals",flush=True)
    return 0


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"INVENTORY_STARTUP_FAILED type={type(exc).__name__}",flush=True)
        raise SystemExit(1)
