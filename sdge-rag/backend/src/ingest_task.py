"""Fargate entry point for document ingestion (one ECS task per uploaded document).

Launched by an EventBridge rule (S3 "Object Created" under uploads/) with S3_BUCKET/S3_KEY
container overrides, or by the API (ecs:RunTask) for re-index (S3_*) and KB re-sync (ACTION=kb-sync).
No 15-minute limit, more memory if needed, and the task exits when the document is indexed."""
import os
import sys
import urllib.parse

import runtime

runtime.setup_logging()
import boto3  # noqa: E402
import ingest  # noqa: E402
from common import log  # noqa: E402


def main():
    action = os.environ.get("ACTION", "ingest")
    if action == "kb-sync":
        ingest.handler({"action": "kb-sync", "reason": os.environ.get("REASON", "sync")}, None)
        return 0
    bucket, key = os.environ["S3_BUCKET"], os.environ["S3_KEY"]
    s3 = boto3.client("s3")
    try:                       # EventBridge sends the key URL-encoded; API launches send it raw
        s3.head_object(Bucket=bucket, Key=key)
    except s3.exceptions.ClientError:
        key = urllib.parse.unquote_plus(key)
    log.info("ingest task started", extra={"bucket": bucket, "key": key,
                                           "taskArn": os.environ.get("ECS_CONTAINER_METADATA_URI_V4", "")[-40:]})
    # ingest.handler expects S3-notification-style (URL-encoded) keys
    ingest.handler({"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": urllib.parse.quote_plus(key)}}}],
                    "reason": os.environ.get("REASON")}, None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
