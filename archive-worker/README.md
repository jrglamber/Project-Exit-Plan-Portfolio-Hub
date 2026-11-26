# Project Exit Plan archive worker

Separate one-shot worker, scheduled nightly in Railway, for the Indices and
Metals PostgreSQL 18 services. Uses database connection references and a private
S3 bucket. No live trading logic, order permissions, database mutations, source
pruning, or archive delete operations.

The worker takes a consistent PostgreSQL custom-format compressed logical
snapshot for each database, sequentially. It verifies a full archive decode
and reads back the uploaded object to compare SHA-256 plus bytes before
publishing a daily verified manifest. Dates are UTC; every day is partitioned
under its month, with distinct timestamped snapshot objects. A verified daily
snapshot is skipped on reruns once its object metadata matches. Unverified
uploads never acquire a verified manifest. The snapshots remain retained until
an explicit, tested retention policy is approved.

Required Railway variables:
`INDICES_DATABASE_URL`, `METALS_DATABASE_URL`, `AWS_ENDPOINT_URL`,
`AWS_S3_BUCKET_NAME`, `AWS_DEFAULT_REGION`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `S3_ADDRESSING_STYLE`.

Recovery: download the dump identified by a verified manifest, check SHA-256,
and restore to a separate PostgreSQL 18 test instance using `pg_restore`.
Never restore directly over a live trading database. The current verification
proves decoding and storage integrity, not a full restore or row-level replay.
A separately validated restore and retention plan must precede any pruning.

### Content-addressed recovery archive (v2)

The separate incremental worker reads the verified v1 full dumps only. It
decodes portable PostgreSQL SQL (owner, ACL, and tablespace assignments omitted),
chunks it at content-defined boundaries, and stores gzip objects by their raw
SHA-256. Every reused or uploaded chunk is read back, and the ordered SQL stream
is independently reconstructed and hashed. The initial recovery test restores
that reconstructed SQL into a disposable socket-only cluster and matches all
source COPY row counts. Future manifests reference shared chunks, not delta
chains. v1 remains authoritative for original metadata. No objects or database
rows are deleted; no savings are assumed until cross-day reuse is measured.

### XAU manager-review telemetry archive and guarded cleanup

The Metals DB Housekeeping job copies bounded ID intervals from
`public.metals_xau_live_manager_reviews` to private, checksummed CSV/gzip
objects. A separate worker with no production database credentials restores
each archived page to a temporary PostgreSQL cluster and compares the
round-trip CSV byte-for-byte. It publishes an independent restore proof.

`housekeeping_jobs.py` always copies first. When
`XAU_VERIFIED_CLEANUP_ENABLED=1` is explicitly set on that service only, it
may remove at most one complete 5,000-ID page per run after downloading and
revalidating its archived object and matching its independent restore proof.
Within a bounded database transaction it locks the targeted rows and linked
closed trade records, verifies the exact existing CSV, preserves the original
one-million-ID hot window and rejects missing/open/unreconciled links,
schema drift, cascades, triggers, rules and conflicts. It deletes only the
exact locked review IDs and confirms their absence before commit. All other
tables, broker transactions, trading logic and archive objects are untouched.

An intent record is retained before any write, and the completion receipt is
published and read back after commit. An interrupted or failed receipt write
must be investigated using the intent and database state; it is not evidence
of a committed deletion. A skipped page is not a failed trading operation.
Turning the flag off restores copy-only operation. Routine PostgreSQL vacuum
can reuse deleted space; this is not a physical volume compaction procedure.
