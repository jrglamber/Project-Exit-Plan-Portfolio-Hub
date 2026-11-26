"""Daily storage metadata and read-only review-retention eligibility audit."""
import inventory
import retention_audit
import cleanup_audit
import bucket_inventory

def main():
    failures=[]
    for name,func in (("inventory",inventory.main),("retention_audit",retention_audit.main),("cleanup_audit",cleanup_audit.main),("bucket_inventory",bucket_inventory.main)):
        try:
            if func():
                failures.append(name)
        except Exception as exc:
            print(f"INVENTORY_STEP_FAILED step={name} type={type(exc).__name__}",flush=True)
            failures.append(name)
    print("INVENTORY_JOBS_COMPLETE status="+("failed:"+",".join(failures) if failures else "verified"),flush=True)
    return 1 if failures else 0

if __name__=="__main__":
    raise SystemExit(main())
