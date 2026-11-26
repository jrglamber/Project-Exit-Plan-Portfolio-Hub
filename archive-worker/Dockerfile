FROM postgres:18
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-boto3 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/archive
COPY archive.py /opt/archive/archive.py
COPY restore_verify.py /opt/archive/restore_verify.py
COPY retention_plan.py /opt/archive/retention_plan.py
USER postgres
ENTRYPOINT []
CMD ["python3", "-u", "/opt/archive/archive.py"]
