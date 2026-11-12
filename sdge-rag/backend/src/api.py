"""REST API (API Gateway proxy -> Lambda). All routes require a Cognito ID
token (validated by the API Gateway Cognito authorizer) and the API key.

  GET    /api/me
  POST   /api/chat                       {question, history?}
  GET    /api/documents
  POST   /api/documents                  {name, contentType, size, category}  (admins)
  GET    /api/documents/{id}/download
  POST   /api/documents/{id}/reindex                                          (admins)
  DELETE /api/documents/{id}                                                  (admins)
  GET    /api/stats                                                           (admins)
  GET    /api/kb                         Bedrock Knowledge Base status + recent ingestion (sync) jobs
  GET    /api/models                     Model Lab: weights, active model, ranking
  POST   /api/models/evaluate            {weights:{quality,cost,latency}} -> benchmark, rank, maybe switch
"""
import json
import os
import re
import time
import uuid

import boto3
from botocore.config import Config

import kb
import model_lab
from common import REGION, bedrock, config, embed, log, metric, sql, vec_literal
from prompts import SYSTEM_PROMPT

DOCS_BUCKET = os.environ["DOCS_BUCKET"]
s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                  config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))

ALLOWED_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
    "text/markdown": ".md",
}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
CATEGORIES = ["Gas Operations", "Safety Procedures", "Maintenance Protocols",
              "Regulatory Compliance", "Company Policies", "General"]
MIN_SCORE = 0.15



class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def resp(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=str),
    }


def user_from(event):
    claims = (event.get("requestContext", {}).get("authorizer") or {}).get("claims") or {}
    groups_raw = claims.get("cognito:groups", "") or ""
    groups = [g for g in re.split(r"[\[\],\s]+", groups_raw) if g]
    return {
        "username": claims.get("cognito:username", "unknown"),
        "name": claims.get("name") or claims.get("cognito:username", ""),
        "role": claims.get("custom:role", "employee"),
        "groups": groups,
        "isAdmin": "admins" in groups,
    }


def require_admin(user):
    if not user["isAdmin"]:
        raise HttpError(403, "Administrator role required")


# ------------------------------------------------------------------ chat
def retrieve(question, k):
    qvec = vec_literal(embed(question))
    return sql(
        """SELECT c.doc_id, d.name AS doc_name, d.category, c.chunk_index, c.page, c.content,
                  1 - (c.embedding <=> CAST(:q AS vector)) AS score
             FROM chunks c JOIN documents d ON d.id = c.doc_id
            WHERE d.status = 'ready'
            ORDER BY c.embedding <=> CAST(:q AS vector)
            LIMIT :k""",
        {"q": qvec, "k": k})


def chat(user, body):
    question = (body.get("question") or "").strip()
    if not question:
        raise HttpError(400, "question is required")
    if len(question) > 4000:
        raise HttpError(400, "question is too long (4000 character max)")
    history = body.get("history") or []
    t0 = time.time()

    k = int(config("retrieval-top-k", "6"))
    retriever = body.get("retriever") or "pgvector"
    if retriever not in ("pgvector", "bedrock-kb"):
        raise HttpError(400, "retriever must be 'pgvector' or 'bedrock-kb'")
    if retriever == "bedrock-kb" and not kb.KB_ID:
        raise HttpError(400, "Bedrock Knowledge Base is not configured")
    raw_hits = kb.retrieve(question, k) if retriever == "bedrock-kb" else retrieve(question, k)
    hits = [h for h in raw_hits if float(h["score"]) >= MIN_SCORE]
    t_retrieval = int((time.time() - t0) * 1000)

    if hits:
        context = "\n\n".join(
            f"[{i}] (Source: {h['doc_name']}{', page ' + str(h['page']) if h.get('page') else ''})\n{h['content']}"
            for i, h in enumerate(hits, 1))
    else:
        context = "(No relevant excerpts were found in the document repository.)"

    messages = []
    for turn in history[-6:]:
        role = turn.get("role")
        text = (turn.get("content") or "")[:4000]
        if role in ("user", "assistant") and text:
            if messages and messages[-1]["role"] == role:
                continue
            messages.append({"role": role, "content": [{"text": text}]})
    if messages and messages[0]["role"] != "user":
        messages = messages[1:]
    if messages and messages[-1]["role"] == "user":
        messages = messages[:-1]
    messages.append({"role": "user", "content": [{"text":
        f"Context excerpts from internal documents:\n\n{context}\n\n---\nEmployee question: {question}"}]})

    # Active model comes from AppConfig (Model Lab); request shape adapts to the model family.
    model = model_lab.active_model()
    model_id = model["id"]
    max_out = int(config("max-output-tokens", "1200"))
    budget = int(config("reasoning-budget-tokens", "1024"))
    req = model_lab.build_request(model, SYSTEM_PROMPT, messages, max_out=max_out, thinking_budget=budget)
    t1 = time.time()
    out = bedrock.converse(**req)
    llm_ms = int((time.time() - t1) * 1000)
    answer, reasoning = model_lab.split_output(out["output"]["message"]["content"])
    usage = out.get("usage", {})

    sources = [{
        "n": i, "docId": h["doc_id"], "docName": h["doc_name"], "category": h["category"],
        "page": h.get("page"), "chunk": h["chunk_index"], "score": round(float(h["score"]), 3),
        "excerpt": h["content"][:700],
    } for i, h in enumerate(hits, 1)]

    total_ms = int((time.time() - t0) * 1000)
    metric("api", ChatRequests=1, InputTokens=usage.get("inputTokens", 0), OutputTokens=usage.get("outputTokens", 0),
           RetrievalTopScore=float(hits[0]["score"]) if hits else 0.0, LlmLatencyMs=llm_ms, RetrievalLatencyMs=t_retrieval)
    log.info("chat answered", extra={"user": user["username"], "model": model_id, "retriever": retriever, "hits": len(hits),
                                     "retrievalMs": t_retrieval, "llmMs": llm_ms, "totalMs": total_ms,
                                     "inputTokens": usage.get("inputTokens"), "outputTokens": usage.get("outputTokens")})
    # Full LLM trace (prompt, retrieval evidence, reasoning, answer) for the Class Insights app.
    log.info("llm_trace", extra={
        "app": "gas-ops-assistant", "traceType": "rag-chat", "user": user["username"], "model": model_id,
        "question": question,
        "systemPrompt": SYSTEM_PROMPT,
        "promptMessages": [{"role": m["role"], "text": m["content"][0]["text"][:12000]} for m in messages],
        "retrieval": {"engine": retriever, "topK": k, "minScore": MIN_SCORE, "embeddingModel": config("embedding-model-id", ""),
                      "knowledgeBaseId": kb.KB_ID if retriever == "bedrock-kb" else None,
                      "hits": [{"n": i, "doc": h["doc_name"], "chunk": h["chunk_index"], "page": h.get("page"),
                                "score": round(float(h["score"]), 4)} for i, h in enumerate(hits, 1)]},
        "reasoning": reasoning, "response": answer,
        "inferenceConfig": req["inferenceConfig"], "modelFamily": model.get("vendor"), "reasoningMode": model.get("reasoning"),
        "thinkingBudget": budget if model.get("reasoning") == "extended-thinking" and budget >= 1024 else 0,
        "usage": usage, "stopReason": out.get("stopReason"),
        "latencyMs": {"retrieval": t_retrieval, "llm": llm_ms, "total": int((time.time() - t0) * 1000)},
    })
    try:
        sql("INSERT INTO chat_log (username, question, answer, sources, latency_ms, model, retriever) VALUES (:u, :q, :a, CAST(:s AS jsonb), :l, :m, :r)",
            {"u": user["username"], "q": question, "a": answer, "m": model_id, "r": retriever,
             "s": json.dumps([{"docName": s["docName"], "score": s["score"]} for s in sources]), "l": total_ms})
    except Exception:  # noqa: BLE001 - logging must never break answering
        log.exception("chat_log insert failed")

    return {"answer": answer, "sources": sources, "model": model_id, "modelName": model.get("name", model_id), "retriever": retriever,
            "timings": {"retrievalMs": t_retrieval, "llmMs": llm_ms, "totalMs": total_ms}}


# ------------------------------------------------------------------ documents
def list_documents():
    rows = sql("""SELECT id, name, category, content_type, size_bytes, status, error, chunk_count, page_count,
                         uploaded_by, uploaded_at, indexed_at
                    FROM documents ORDER BY uploaded_at DESC""")
    return {"documents": rows, "categories": CATEGORIES}


def create_document(user, body):
    require_admin(user)
    name = re.sub(r"[^\w.\- ()&]", "_", (body.get("name") or "").strip())[:200]
    ctype = body.get("contentType") or ""
    size = int(body.get("size") or 0)
    category = body.get("category") if body.get("category") in CATEGORIES else "General"
    if not name:
        raise HttpError(400, "name is required")
    if name.lower().endswith(".md"):
        ctype = "text/markdown"
    if ctype not in ALLOWED_TYPES:
        raise HttpError(400, "Unsupported file type. Upload PDF, DOCX, TXT or MD.")
    if size <= 0 or size > MAX_UPLOAD_BYTES:
        raise HttpError(400, "File must be between 1 byte and 25 MB")
    doc_id = uuid.uuid4().hex[:16]
    key = f"uploads/{doc_id}/{name}"
    sql("""INSERT INTO documents (id, name, category, s3_key, content_type, size_bytes, status, uploaded_by)
           VALUES (:id, :name, :cat, :key, :ct, :size, 'uploading', :by)""",
        {"id": doc_id, "name": name, "cat": category, "key": key, "ct": ctype, "size": size, "by": user["username"]})
    url = s3.generate_presigned_url("put_object", ExpiresIn=900,
                                    Params={"Bucket": DOCS_BUCKET, "Key": key, "ContentType": ctype})
    log.info("upload url issued", extra={"user": user["username"], "docId": doc_id, "docName": name, "size": size})
    return {"id": doc_id, "uploadUrl": url, "contentType": ctype}


def get_doc(doc_id):
    rows = sql("SELECT id, name, s3_key, content_type FROM documents WHERE id = :id", {"id": doc_id})
    if not rows:
        raise HttpError(404, "Document not found")
    return rows[0]


def download_document(doc_id):
    d = get_doc(doc_id)
    url = s3.generate_presigned_url("get_object", ExpiresIn=300, Params={
        "Bucket": DOCS_BUCKET, "Key": d["s3_key"], "ResponseContentType": d["content_type"],
        "ResponseContentDisposition": f'inline; filename="{d["name"]}"'})
    return {"url": url}


def reindex_document(user, doc_id):
    require_admin(user)
    d = get_doc(doc_id)
    sql("UPDATE documents SET status = 'queued', error = NULL WHERE id = :id", {"id": doc_id})
    launch_ingest(S3_BUCKET=DOCS_BUCKET, S3_KEY=d["s3_key"], REASON=f"re-indexed {d['name']}"[:200])
    return {"ok": True}


_ecs = boto3.client("ecs", region_name=REGION)


def launch_ingest(**env):
    """Start a one-off Fargate ingest task (the work used to be an async Lambda invoke)."""
    r = _ecs.run_task(
        cluster=os.environ["ECS_CLUSTER"], taskDefinition=os.environ["INGEST_TASK_FAMILY"], launchType="FARGATE", count=1,
        networkConfiguration={"awsvpcConfiguration": {"subnets": os.environ["TASK_SUBNETS"].split(","),
                                                      "securityGroups": [os.environ["INGEST_SECURITY_GROUP"]],
                                                      "assignPublicIp": "ENABLED"}},
        overrides={"containerOverrides": [{"name": "ingest", "environment": [{"name": k, "value": v} for k, v in env.items()]}]},
        startedBy="sdge-rag-api", propagateTags="TASK_DEFINITION")
    if r.get("failures"):
        raise HttpError(503, f"could not start ingest task: {r['failures'][0].get('reason')}")
    log.info("ingest task launched", extra={"taskArn": r["tasks"][0]["taskArn"], "env": env})


def delete_document(user, doc_id):
    require_admin(user)
    d = get_doc(doc_id)
    sql("DELETE FROM documents WHERE id = :id", {"id": doc_id})  # chunks cascade
    try:
        versions = s3.list_object_versions(Bucket=DOCS_BUCKET, Prefix=f"uploads/{doc_id}/")
        objs = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                for v in versions.get("Versions", []) + versions.get("DeleteMarkers", [])]
        if objs:
            s3.delete_objects(Bucket=DOCS_BUCKET, Delete={"Objects": objs})
    except Exception:  # noqa: BLE001
        log.exception("s3 cleanup failed", extra={"docId": doc_id})
    launch_ingest(ACTION="kb-sync", REASON=f"deleted {d['name']}"[:200])
    log.info("document deleted", extra={"user": user["username"], "docId": doc_id, "docName": d["name"]})
    return {"ok": True}


def stats(user):
    require_admin(user)
    docs = sql("SELECT status, count(*) AS n FROM documents GROUP BY status")
    chunks = sql("SELECT count(*) AS n FROM chunks")
    chats = sql("""SELECT count(*) AS total,
                          count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS last24h,
                          count(DISTINCT username) AS users,
                          coalesce(avg(latency_ms), 0)::int AS avg_latency_ms
                     FROM chat_log""")
    recent = sql("SELECT username, question, latency_ms, model, created_at FROM chat_log ORDER BY created_at DESC LIMIT 15")
    return {"documentsByStatus": {r["status"]: r["n"] for r in docs}, "chunks": chunks[0]["n"],
            "chats": chats[0], "recentQuestions": recent}


# ------------------------------------------------------------------ router
def handler(event, context):
    method = event.get("httpMethod", "GET")
    path = event.get("path", "")
    user = user_from(event)
    log.info("request", extra={"method": method, "path": path, "user": user["username"],
                               "requestId": event.get("requestContext", {}).get("requestId")})
    try:
        body = json.loads(event.get("body") or "{}") if event.get("body") else {}
    except json.JSONDecodeError:
        return resp(400, {"error": "Invalid JSON body"})

    try:
        m = re.match(r"^/api/documents/([0-9a-f]{16})(/download|/reindex)?$", path)
        if path == "/api/me" and method == "GET":
            return resp(200, user)
        if path == "/api/chat" and method == "POST":
            return resp(200, chat(user, body))
        if path == "/api/documents" and method == "GET":
            return resp(200, list_documents())
        if path == "/api/documents" and method == "POST":
            return resp(201, create_document(user, body))
        if path == "/api/kb" and method == "GET":
            return resp(200, kb.status())
        if path == "/api/models" and method == "GET":
            return resp(200, model_lab.get_state())
        if path == "/api/models/evaluate" and method == "POST":
            try:
                return resp(200, model_lab.evaluate(user, body))
            except ValueError as e:      # invalid weights
                raise HttpError(400, str(e))
        if path == "/api/stats" and method == "GET":
            return resp(200, stats(user))
        if m:
            doc_id, action = m.group(1), m.group(2)
            if action == "/download" and method == "GET":
                return resp(200, download_document(doc_id))
            if action == "/reindex" and method == "POST":
                return resp(202, reindex_document(user, doc_id))
            if action is None and method == "DELETE":
                return resp(200, delete_document(user, doc_id))
        return resp(404, {"error": f"No route for {method} {path}"})
    except HttpError as e:
        log.warning("client error", extra={"status": e.status, "error": e.message, "path": path})
        return resp(e.status, {"error": e.message})
    except Exception as e:  # noqa: BLE001
        log.exception("unhandled error", extra={"path": path, "error": str(e)})
        return resp(500, {"error": "Internal error. Please try again.",
                          "requestId": getattr(context, "aws_request_id", None)})
