"""Amazon Bedrock Knowledge Base (managed RAG over S3 Vectors) for the Gas Ops document library.

The custom pipeline (ingest.py -> Aurora pgvector) and the Knowledge Base index the *same* S3 objects
(s3://<docs bucket>/uploads/<doc id>/<file>). The KB is kept in sync by starting an ingestion job after
every upload, re-index and delete; Bedrock then chunks (fixed 300 tokens, 20 % overlap), embeds with
Titan v2 (1024-d) and writes vectors to the S3 Vectors index. Ingestion is incremental: unchanged
files are skipped and deleted files have their vectors removed."""
import os
import time

import boto3

from common import REGION, log, sql

KB_ID = os.environ.get("KB_ID", "")
_DS_ID = os.environ.get("KB_DATA_SOURCE_ID", "")
agent = boto3.client("bedrock-agent", region_name=REGION)
agent_rt = boto3.client("bedrock-agent-runtime", region_name=REGION)


def data_source_id():
    global _DS_ID
    if not _DS_ID and KB_ID:
        _DS_ID = agent.list_data_sources(knowledgeBaseId=KB_ID, maxResults=5)["dataSourceSummaries"][0]["dataSourceId"]
    return _DS_ID


def _running():
    return agent.list_ingestion_jobs(
        knowledgeBaseId=KB_ID, dataSourceId=data_source_id(), maxResults=5,
        filters=[{"attribute": "STATUS", "operator": "EQ", "values": ["STARTING", "IN_PROGRESS"]}],
    )["ingestionJobSummaries"]


def sync(reason, wait_seconds=600):
    """Start an ingestion job. Only one job may run per data source, so if one is already running
    (it may have started before our change landed in S3) wait for it, then start a fresh one."""
    if not KB_ID:
        return None
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not _running():
            try:
                job = agent.start_ingestion_job(knowledgeBaseId=KB_ID, dataSourceId=data_source_id(),
                                                description=reason[:200])["ingestionJob"]
                log.info("kb ingestion started", extra={"knowledgeBaseId": KB_ID, "ingestionJobId": job["ingestionJobId"],
                                                        "reason": reason})
                return job["ingestionJobId"]
            except agent.exceptions.ConflictException:
                pass
        time.sleep(8)
    log.warning("kb ingestion not started: another job kept running", extra={"reason": reason})
    return None


def status():
    if not KB_ID:
        return {"enabled": False}
    kb = agent.get_knowledge_base(knowledgeBaseId=KB_ID)["knowledgeBase"]
    jobs = agent.list_ingestion_jobs(knowledgeBaseId=KB_ID, dataSourceId=data_source_id(), maxResults=8,
                                     sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"})["ingestionJobSummaries"]
    return {
        "enabled": True, "knowledgeBaseId": KB_ID, "name": kb["name"], "status": kb["status"],
        "dataSourceId": data_source_id(), "vectorStore": "Amazon S3 Vectors",
        "embeddingModel": "amazon.titan-embed-text-v2:0 (1024-d)", "chunking": "fixed size, 300 tokens, 20% overlap",
        "jobs": [{"id": j["ingestionJobId"], "status": j["status"], "startedAt": j["startedAt"].isoformat(),
                  "updatedAt": j["updatedAt"].isoformat(), "description": j.get("description"),
                  "stats": j.get("statistics", {})} for j in jobs],
    }


def retrieve(question, k):
    """Same output shape as api.retrieve() (pgvector) so the generation step is identical."""
    r = agent_rt.retrieve(knowledgeBaseId=KB_ID, retrievalQuery={"text": question},
                          retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": k}})
    rows = []
    for x in r["retrievalResults"]:
        uri = x.get("location", {}).get("s3Location", {}).get("uri", "")
        key = uri.split("/", 3)[3] if uri.count("/") >= 3 else ""
        parts = key.split("/")
        meta = x.get("metadata", {})
        page = meta.get("x-amz-bedrock-kb-document-page-number")
        rows.append({"doc_id": parts[1] if len(parts) > 2 else None, "doc_name": parts[-1] if parts else uri,
                     "category": None, "chunk_index": meta.get("x-amz-bedrock-kb-chunk-id", "")[:8],
                     "page": int(page) if page is not None else None,
                     "content": x["content"].get("text", ""), "score": float(x.get("score", 0))})
    ids = sorted({h["doc_id"] for h in rows if h["doc_id"]})
    if ids:
        params = {f"i{n}": v for n, v in enumerate(ids)}
        docs = {d["id"]: d for d in sql(f"SELECT id, name, category FROM documents WHERE id IN ({', '.join(':' + p for p in params)})", params)}
        for h in rows:
            d = docs.get(h["doc_id"])
            if d:
                h["doc_name"], h["category"] = d["name"], d["category"]
            else:   # vector left over from a deleted doc that the next sync will remove
                h["stale"] = True
    return [h for h in rows if not h.get("stale")]
