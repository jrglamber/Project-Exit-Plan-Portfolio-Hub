FROM postgres:18
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-boto3 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/archive
COPY archive.py /opt/archive/archive.py
COPY restore_verify.py /opt/archive/restore_verify.py
COPY retention_plan.py /opt/archive/retention_plan.py
COPY incremental.py /opt/archive/incremental.py
COPY inventory.py /opt/archive/inventory.py
COPY table_archive.py /opt/archive/table_archive.py
COPY table_restore.py /opt/archive/table_restore.py
COPY restore_jobs.py /opt/archive/restore_jobs.py
COPY retention_audit.py /opt/archive/retention_audit.py
COPY inventory_jobs.py /opt/archive/inventory_jobs.py
USER postgres
ENTRYPOINT []
CMD ["python3", "-u", "/opt/archive/archive.py"]
