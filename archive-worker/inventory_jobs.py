"""Daily storage metadata and read-only review-retention eligibility audit."""
import inventory
import retention_audit

def main():
    failures=[]
    for name,func in (("inventory",inventory.main),("retention_audit",retention_audit.main)):
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
