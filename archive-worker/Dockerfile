FROM postgres:18
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-boto3 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/archive
COPY archive.py /opt/archive/archive.py
USER postgres
ENTRYPOINT []
CMD ["python3", "-u", "/opt/archive/archive.py"]
