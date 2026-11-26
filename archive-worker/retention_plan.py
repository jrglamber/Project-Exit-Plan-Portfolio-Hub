"""Conservative, copy-only archive retention inventory. Never deletes objects."""
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict
import re

from archive import DBS, PREFIX, read_manifest, write_manifest

DAILY_DAYS = 14
WEEKLY_WEEKS = 8


def classify(dates, today, protected=()):
    """Keep recent days, one snapshot per recent ISO week, and one per month.

    The first verified snapshot and all restore-proven checkpoints are always
    retained. Dates omitted from keep are review candidates, not deleted.
    """
    dates = sorted(set(dates))
    keep = set(protected) & set(dates)
    if dates:
        keep.update((dates[0], dates[-1]))
    keep.update(d for d in dates if d >= today - timedelta(days=DAILY_DAYS - 1))
    weeks = defaultdict(list)
    months = defaultdict(list)
    for d in dates:
        weeks[d.isocalendar()[:2]].append(d)
        months[(d.year, d.month)].append(d)
    for week in sorted(weeks, reverse=True)[:WEEKLY_WEEKS]:
        keep.add(max(weeks[week]))
    keep.update(max(month) for month in months.values())
    return keep


def produce(s3, bucket, keys_for, now=None):
    now = now or datetime.now(timezone.utc)
    summary = {}
    for asset in DBS:
        prefix = f"{PREFIX}/{asset}/"
        regex = re.compile(re.escape(prefix) + r"(\d{4})/(\d{2})/(\d{2})/verified\.json$")
        snapshots = {}
        for k in keys_for(s3, bucket, prefix):
            m = regex.fullmatch(k)
            if not m:
                continue
            d = date(*map(int, m.groups()))
            manifest = read_manifest(s3, bucket, k)
            if not manifest or manifest.get("status") != "verified" or manifest.get("asset") != asset:
                raise RuntimeError("invalid archive manifest encountered")
            snapshots[d] = (k, manifest)
        protected = set()
        proof_prefix = f"{PREFIX}/restore-proof/{asset}/"
        for k in keys_for(s3, bucket, proof_prefix):
            if not k.endswith(".json"):
                continue
            proof = read_manifest(s3, bucket, k)
            match = regex.fullmatch((proof or {}).get("snapshot_manifest", ""))
            if match and proof.get("status") == "verified":
                protected.add(date(*map(int, match.groups())))
        keep = classify(snapshots, now.date(), protected)
        candidates = [{"date": d.isoformat(), "manifest": snapshots[d][0],
                       "object_key": snapshots[d][1]["object_key"], "bytes": snapshots[d][1]["bytes"]}
                      for d in sorted(set(snapshots) - keep)]
        summary[asset] = {
            "verified_snapshots": len(snapshots), "retained_snapshots": len(keep),
            "protected_restore_checkpoints": len(protected),
            "candidate_snapshots": candidates, "candidate_bytes": sum(r["bytes"] for r in candidates),
        }
        print(f"RETENTION_PLAN asset={asset} keep={len(keep)} review_candidates={len(candidates)}", flush=True)
    report = {"mode": "PLAN_ONLY", "generated_utc": now.isoformat(),
              "policy": {"daily_days": DAILY_DAYS, "weekly_weeks": WEEKLY_WEEKS,
                         "monthly": "one per month indefinitely", "protect_first_latest_restore": True},
              "deletion_enabled": False, "assets": summary}
    write_manifest(s3, bucket, f"{PREFIX}/retention/latest.json", report)
    return report
