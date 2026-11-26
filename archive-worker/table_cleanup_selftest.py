"""Real PostgreSQL safety test, on a disposable socket-only database."""
import tempfile
from pathlib import Path

from archive import file_hash
from restore_verify import restore_and_count
from table_archive import TABLE, SCHEMA_SQL, PAGE_IDS
from table_cleanup import connect, cleanup_transaction, export_current, Ineligible, LINKS

LO, HI = 1, PAGE_IDS
HOT = 1_000_000 + HI + 1

def selftest():
    with tempfile.TemporaryDirectory(prefix="pep-cleanup-test-") as directory:
        root = Path(directory)
        def test(env):
            assert env["PGHOST"] == str(root/"socket")
            # No production credentials or network hostname passed to connect().
            conn = connect(dict(env, PGDATABASE="restored", PGSSLMODE="disable"))
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(f"CREATE TABLE public.{LINKS}(id bigint PRIMARY KEY, status text, closed_at_utc text)")
                        cur.execute(f"CREATE TABLE public.{TABLE}(id bigint PRIMARY KEY, link_id bigint, details text)")
                        cur.execute(f"INSERT INTO public.{LINKS} VALUES (1,'CLOSED_MANAGER','2026-01-01T00:00:00Z'), (2,'CLOSED_BROKER','2026-01-01T00:00:00Z')")
                        cur.execute(f"INSERT INTO public.{TABLE} SELECT id, 1, 'review-'||id FROM generate_series(1,%s) id", (HI,))
                        cur.execute(f"INSERT INTO public.{TABLE} VALUES (%s,2,'hot sentinel')",(HOT,))
                        cur.execute(SCHEMA_SQL)
                        columns = cur.fetchone()[0]
                        raw = root/"verified.csv"
                        export_current(cur, LO, HI, raw)
                receipt = dict(first_id_requested=LO,last_id_requested=HI,
                    first_present_id=LO,last_present_id=HI,row_count=HI,
                    csv_bytes=raw.stat().st_size,csv_sha256=file_hash(raw),columns=columns)
                def mutate(sql, args=()):
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute(sql,args)
                def verify_count(expected):
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute(f"SELECT count(*) FROM public.{TABLE}")
                            if cur.fetchone() != (expected,):
                                raise RuntimeError("selftest row count changed unexpectedly")
                            cur.execute(f"SELECT count(*) FROM public.{LINKS}")
                            if cur.fetchone() != (2,):
                                raise RuntimeError("selftest trade links changed")
                def blocked(reason, r=receipt, archive=raw):
                    try:
                        cleanup_transaction(conn,r,archive,root)
                    except Ineligible as exc:
                        if str(exc) != reason:
                            raise RuntimeError(f"selftest expected {reason} got {exc}") from None
                    else:
                        raise RuntimeError("selftest unsafe deletion was not blocked")
                    verify_count(HI+1)
                mutate(f"UPDATE public.{TABLE} SET id=%s WHERE id=%s",(HOT-1,HOT))
                blocked("partial_hot_window")
                mutate(f"UPDATE public.{TABLE} SET id=%s WHERE id=%s",(HOT,HOT-1))
                blocked("not_a_full_nonempty_page",dict(receipt,last_id_requested=HI-1))
                blocked("source_row_count_changed",dict(receipt,row_count=HI-1))
                mutate(f"UPDATE public.{LINKS} SET status='OPEN' WHERE id=1")
                blocked("open_unknown_or_unreconciled_trade_links")
                mutate(f"UPDATE public.{LINKS} SET status='CLOSED_MANAGER', closed_at_utc=NULL WHERE id=1")
                blocked("open_unknown_or_unreconciled_trade_links")
                mutate(f"UPDATE public.{LINKS} SET closed_at_utc='2026-01-01T00:00:00Z' WHERE id=1")
                mutate(f"UPDATE public.{TABLE} SET link_id=999 WHERE id=1")
                blocked("open_unknown_or_unreconciled_trade_links")
                mutate(f"UPDATE public.{TABLE} SET link_id=1, details='changed' WHERE id=1")
                blocked("source_content_not_identical")
                mutate(f"UPDATE public.{TABLE} SET details='review-1' WHERE id=1")
                corrupt=root/"corrupt.csv"
                corrupt.write_bytes(raw.read_bytes()+b"CORRUPT")
                blocked("local_archive_mismatch",archive=corrupt)
                mutate(f"CREATE TABLE public.dependent_review(id bigint REFERENCES public.{TABLE}(id) ON DELETE CASCADE)")
                blocked("unexpected_delete_side_effect_or_structure")
                mutate("DROP TABLE public.dependent_review")
                result=cleanup_transaction(conn,receipt,raw,root)
                if result["rows_deleted"]!=HI or result["post_delete_target_rows"]!=0:
                    raise RuntimeError("selftest exact delete failed")
                verify_count(1)
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT id,details FROM public.{TABLE}")
                        if cur.fetchall()!=[(HOT,"hot sentinel")]:
                            raise RuntimeError("selftest hot row not preserved")
            finally:
                conn.close()
        actual=restore_and_count(None,{f"public.{TABLE}":1,f"public.{LINKS}":2},root,restore_sql=test)
        if actual != {f"public.{TABLE}":1,f"public.{LINKS}":2}:
            raise RuntimeError("selftest final count mismatch")
    print("CLEANUP_SELFTEST_PASSED isolated_postgres=true hot_boundary=true partial_page=true "
          "count_guard=true open_and_missing_links=true source_byte_guard=true archive_byte_guard=true "
          "cascade_guard=true exact_delete_rows=5000 hot_row_retained=true trade_links_retained=true",flush=True)
