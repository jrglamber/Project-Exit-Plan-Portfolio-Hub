"""Copy always; delete only with explicit authorization and independent proof."""
import os
import table_archive
import table_cleanup
from table_cleanup_selftest import selftest

def main():
    if os.getenv("XAU_CLEANUP_SELFTEST")=="1":
        selftest()
    table_archive.main()
    table_cleanup.main()
    return 0

if __name__=="__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"HOUSEKEEPING_FAILED type={type(exc).__name__}",flush=True)
        raise SystemExit(1)
