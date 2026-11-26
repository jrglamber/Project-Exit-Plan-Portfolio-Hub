"""Daily proof of newly archived XAU reviews; full database restores monthly."""
from datetime import datetime, timezone
import restore_verify
import table_restore

def main():
    failures=[]
    try:
        result=table_restore.main()
        if result:
            failures.append("xau_review")
    except Exception as exc:
        print(f"TABLE_RESTORE_DAILY_FAILED type={type(exc).__name__}",flush=True)
        failures.append("xau_review")
    if datetime.now(timezone.utc).day==1:
        try:
            if restore_verify.main():
                failures.append("full_databases")
        except Exception as exc:
            print(f"FULL_RESTORE_MONTHLY_FAILED type={type(exc).__name__}",flush=True)
            failures.append("full_databases")
    print("RESTORE_JOBS_COMPLETE status="+("failed:"+",".join(failures) if failures else "verified"),flush=True)
    return 1 if failures else 0

if __name__=="__main__":
    raise SystemExit(main())
