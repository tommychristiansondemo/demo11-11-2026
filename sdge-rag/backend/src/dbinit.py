"""CloudFormation custom resource: create the pgvector extension and schema."""
import json
import time
import urllib.request

from common import log, sql

SCHEMA = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """CREATE TABLE IF NOT EXISTS documents (
         id           TEXT PRIMARY KEY,
         name         TEXT NOT NULL,
         category     TEXT NOT NULL DEFAULT 'General',
         s3_key       TEXT,
         content_type TEXT,
         size_bytes   BIGINT,
         status       TEXT NOT NULL DEFAULT 'uploading',
         error        TEXT,
         chunk_count  INT NOT NULL DEFAULT 0,
         page_count   INT,
         uploaded_by  TEXT,
         uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
         indexed_at   TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS chunks (
         id          BIGSERIAL PRIMARY KEY,
         doc_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
         chunk_index INT NOT NULL,
         page        INT,
         content     TEXT NOT NULL,
         embedding   vector(1024) NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks(doc_id)",
    "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)",
    """CREATE TABLE IF NOT EXISTS chat_log (
         id          BIGSERIAL PRIMARY KEY,
         username    TEXT,
         question    TEXT,
         answer      TEXT,
         sources     JSONB,
         latency_ms  INT,
         created_at  TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS model TEXT",
    "ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS retriever TEXT",
]


def _respond(event, status, reason=""):
    body = json.dumps({
        "Status": status, "Reason": reason[:500] or "see CloudWatch logs",
        "PhysicalResourceId": "sdge-rag-schema", "StackId": event["StackId"],
        "RequestId": event["RequestId"], "LogicalResourceId": event["LogicalResourceId"],
    }).encode()
    req = urllib.request.Request(event["ResponseURL"], data=body, method="PUT",
                                 headers={"Content-Type": "", "Content-Length": str(len(body))})
    urllib.request.urlopen(req, timeout=30)


def handler(event, context):
    log.info("dbinit event", extra={"requestType": event.get("RequestType")})
    try:
        if event["RequestType"] in ("Create", "Update"):
            # Data API may briefly report the cluster as unavailable right after creation
            for attempt in range(30):
                try:
                    sql("SELECT 1")
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning("db not ready", extra={"attempt": attempt, "error": str(e)})
                    time.sleep(10)
            for stmt in SCHEMA:
                sql(stmt)
        _respond(event, "SUCCESS")
    except Exception as e:  # noqa: BLE001
        log.exception("dbinit failed")
        _respond(event, "FAILED", str(e))
