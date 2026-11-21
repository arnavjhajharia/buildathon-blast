# Blast Radius entry: picks the sqlglot tests a patch can break, from a
# coverage map built at the pinned sqlglot commit (data/covmap.json.gz).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
ENV PYTHONDONTWRITEBYTECODE=1 BLAST_SRC=/opt/sqlglot

# The pinned sqlglot sources, read to see what a patch changes.
RUN python - <<PY
import io, tarfile, urllib.request
url = "https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tf:
    for m in tf.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) < 2 or not (parts[1].startswith("sqlglot/") or parts[1].startswith("tests/")):
            continue
        if not (m.isfile() or m.isdir()):
            continue
        m.name = parts[1]
        tf.extract(m, "/opt/sqlglot", filter="data")
PY
RUN test -f /opt/sqlglot/sqlglot/parser.py && chmod -R a+rX /opt/sqlglot

COPY selector.py /app/selector.py
COPY data /app/data
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
