"""Read-only data access for the Class Insights app. Used by the UI endpoints
(data.py) and as Bedrock tool-use tools by the chatbot (chat.py).

Security: this module only reads deployment metadata, logs and metrics.
It has no access to Secrets Manager, API key values, or the application
database (IAM explicitly denies those). Every string returned passes
through redact() as defence in depth."""
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-2")
ENV = os.environ.get("TARGET_ENV", "prod")
INSIGHTS_ENV = os.environ.get("ENV_NAME", "prod")
KNOWLEDGE_BUCKET = os.environ.get("KNOWLEDGE_BUCKET", "")
KNOWLEDGE_KEY = "projectknowledge.md"

log = logging.getLogger("sdge-insights")
log.setLevel(logging.INFO)

logs = boto3.client("logs", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)

LOG_GROUPS = {
    "main-api": f"/ecs/sdge-rag-{ENV}-api",                      # Fargate service (since 2026-11-11)
    "main-ingest": f"/ecs/sdge-rag-{ENV}-ingest",                # Fargate tasks, one per document
    "main-presignup": f"/aws/lambda/sdge-rag-{ENV}-presignup",
    "main-postconfirm": f"/aws/lambda/sdge-rag-{ENV}-postconfirm",
    "main-dbinit": f"/aws/lambda/sdge-rag-{ENV}-dbinit",
    "main-apigw-access": f"/aws/apigateway/sdge-rag-{ENV}-access",
    "pipeline-validate": f"/aws/codebuild/sdge-rag-{ENV}-validate",
    "pipeline-deploy": f"/aws/codebuild/sdge-rag-{ENV}-deploy",
    "image-builder": f"/aws/codebuild/sdge-rag-{ENV}-image-builder",
    "insights-chat": f"/ecs/sdge-insights-{INSIGHTS_ENV}-chat",  # Fargate service (since 2026-11-11)
    "insights-data": f"/aws/lambda/sdge-insights-{INSIGHTS_ENV}-data",
    "insights-knowledge": f"/aws/lambda/sdge-insights-{INSIGHTS_ENV}-knowledge",
    "insights-apigw-access": f"/aws/apigateway/sdge-insights-{INSIGHTS_ENV}-access",
    "legacy-main-api-lambda": f"/aws/lambda/sdge-rag-{ENV}-api",
    "legacy-main-ingest-lambda": f"/aws/lambda/sdge-rag-{ENV}-ingest",
    "legacy-insights-chat-lambda": f"/aws/lambda/sdge-insights-{INSIGHTS_ENV}-chat",
}
# LLM traces: current Fargate log groups plus the Lambda-era groups (so history is not lost)
TRACE_GROUPS = {"main": ["main-api", "legacy-main-api-lambda"], "insights": ["insights-chat", "legacy-insights-chat-lambda"]}

STACKS = [
    ("us-east-1", f"sdge-rag-{ENV}-edge"),
    ("us-east-2", f"sdge-rag-{ENV}-app"),
    ("us-east-2", f"sdge-rag-{ENV}-pipeline"),
    ("us-east-2", f"sdge-rag-{ENV}-containers"),
    ("us-east-1", f"sdge-insights-{INSIGHTS_ENV}-edge"),
    ("us-east-2", f"sdge-insights-{INSIGHTS_ENV}-app"),
]

_REDACTIONS = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[redacted-token]"),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "[redacted-access-key-id]"),
    (re.compile(r"(X-Amz-(?:Signature|Credential|Security-Token)=)[^&\s\"']+"), r"\1[redacted]"),
    (re.compile(r"(?i)(\"?(?:x-api-key|apiKey|api_key|password|secret|authorization)\"?\s*[:=]\s*\"?)[^\s\",}]{6,}"), r"\1[redacted]"),
]


def redact(value):
    if isinstance(value, str):
        for pat, rep in _REDACTIONS:
            value = pat.sub(rep, value)
        return value
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


def _clip(s, n):
    s = s if isinstance(s, str) else json.dumps(s, default=str)
    return s if len(s) <= n else s[:n] + f"… [+{len(s) - n} chars]"


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ logs
def search_logs(component="main-api", filter_text="", minutes=60, limit=25, level=""):
    group = LOG_GROUPS.get(component)
    if not group:
        return {"error": f"unknown component; choose one of {sorted(LOG_GROUPS)}"}
    minutes = max(1, min(int(minutes), 7 * 24 * 60))
    limit = max(1, min(int(limit), 100))
    pattern = ""
    if level:
        pattern = f'{{ $.level = "{level.upper()}" }}'
    elif filter_text:
        pattern = '"' + filter_text.replace('"', "") + '"'
    events, token = [], None
    start = int((time.time() - minutes * 60) * 1000)
    try:
        while len(events) < 500:
            kw = dict(logGroupName=group, startTime=start, limit=500)
            if pattern:
                kw["filterPattern"] = pattern
            if token:
                kw["nextToken"] = token
            r = logs.filter_log_events(**kw)
            events.extend(r.get("events", []))
            token = r.get("nextToken")
            if not token:
                break
    except logs.exceptions.ResourceNotFoundException:
        return {"component": component, "logGroup": group, "events": [], "note": "log group has no events yet"}
    events = sorted(events, key=lambda e: e["timestamp"], reverse=True)[:limit]
    out = []
    for e in events:
        msg = e["message"].strip()
        try:
            parsed = json.loads(msg)
            if isinstance(parsed, dict) and parsed.get("message") == "llm_trace":
                parsed = {k: parsed.get(k) for k in ("timestamp", "level", "message", "app", "user", "model", "question")}
            msg = json.dumps(parsed, default=str)
        except (json.JSONDecodeError, ValueError):
            pass
        out.append({"time": _iso(e["timestamp"]), "message": redact(_clip(msg, 1500))})
    return {"component": component, "logGroup": group, "minutes": minutes, "count": len(out), "events": out}


def get_llm_traces(app="all", limit=5, minutes=7 * 24 * 60, detail="summary"):
    """Recent LLM calls (prompts, retrieval evidence, model reasoning, response, usage)."""
    apps = list(TRACE_GROUPS) if app == "all" else [app]
    limit = max(1, min(int(limit), 25))
    start = int((time.time() - min(int(minutes), 14 * 24 * 60) * 60) * 1000)
    traces = []
    for group in [LOG_GROUPS[c] for a in apps for c in TRACE_GROUPS[a]]:
        try:
            r = logs.filter_log_events(logGroupName=group, startTime=start, limit=500,
                                       filterPattern='{ $.message = "llm_trace" }')
        except logs.exceptions.ResourceNotFoundException:
            continue
        evs = r.get("events", [])
        token = r.get("nextToken")
        while token and len(evs) < 1000:
            r = logs.filter_log_events(logGroupName=group, startTime=start, limit=500, nextToken=token,
                                       filterPattern='{ $.message = "llm_trace" }')
            evs.extend(r.get("events", []))
            token = r.get("nextToken")
        for e in evs:
            try:
                t = json.loads(e["message"])
            except json.JSONDecodeError:
                continue
            t["_ts"] = e["timestamp"]
            traces.append(t)
    traces.sort(key=lambda t: t["_ts"], reverse=True)
    traces = traces[:limit]
    full = detail == "full"
    out = []
    for t in traces:
        item = {
            "time": _iso(t["_ts"]), "app": t.get("app"), "type": t.get("traceType"), "user": t.get("user"),
            "model": t.get("model"), "question": t.get("question"),
            "reasoning": t.get("reasoning") if full else _clip(t.get("reasoning") or "", 1200),
            "response": t.get("response") if full else _clip(t.get("response") or "", 1200),
            "usage": t.get("usage"), "latencyMs": t.get("latencyMs"), "stopReason": t.get("stopReason"),
        }
        if t.get("retrieval"):
            item["retrieval"] = t["retrieval"]
        if t.get("toolCalls"):
            item["toolCalls"] = t["toolCalls"] if full else [
                {"name": c.get("name"), "input": c.get("input")} for c in t["toolCalls"]]
        if full:
            item["systemPrompt"] = t.get("systemPrompt")
            item["promptMessages"] = t.get("promptMessages")
            item["inferenceConfig"] = t.get("inferenceConfig")
            item["rounds"] = t.get("rounds")
        out.append(item)
    return {"count": len(out), "traces": redact(out)}


# ------------------------------------------------------------------ metrics
_METRICS = {
    "chat_requests": ("SdgeRag/" + ENV, "ChatRequests", [("Service", "api")], "Sum"),
    "input_tokens": ("SdgeRag/" + ENV, "InputTokens", [("Service", "api")], "Sum"),
    "output_tokens": ("SdgeRag/" + ENV, "OutputTokens", [("Service", "api")], "Sum"),
    "llm_latency_ms_p90": ("SdgeRag/" + ENV, "LlmLatencyMs", [("Service", "api")], "p90"),
    "retrieval_top_score_avg": ("SdgeRag/" + ENV, "RetrievalTopScore", [("Service", "api")], "Average"),
    "documents_ingested": ("SdgeRag/" + ENV, "DocumentsIngested", [("Service", "ingest")], "Sum"),
    "chunks_indexed": ("SdgeRag/" + ENV, "ChunksIndexed", [("Service", "ingest")], "Sum"),
    "api_requests": ("AWS/ApiGateway", "Count", [("ApiName", f"sdge-rag-{ENV}"), ("Stage", ENV)], "Sum"),
    "api_4xx": ("AWS/ApiGateway", "4XXError", [("ApiName", f"sdge-rag-{ENV}"), ("Stage", ENV)], "Sum"),
    "api_5xx": ("AWS/ApiGateway", "5XXError", [("ApiName", f"sdge-rag-{ENV}"), ("Stage", ENV)], "Sum"),
    "api_latency_ms_p90": ("AWS/ApiGateway", "Latency", [("ApiName", f"sdge-rag-{ENV}"), ("Stage", ENV)], "p90"),
    "api_service_cpu_pct": ("AWS/ECS", "CPUUtilization", [("ClusterName", f"sdge-rag-{ENV}"), ("ServiceName", f"sdge-rag-{ENV}-api")], "Average"),
    "api_service_memory_pct": ("AWS/ECS", "MemoryUtilization", [("ClusterName", f"sdge-rag-{ENV}"), ("ServiceName", f"sdge-rag-{ENV}-api")], "Average"),
    "api_error_logs": ("SdgeRag/" + ENV, "ApiErrorLogs", [], "Sum"),
    "ingest_failures": ("SdgeRag/" + ENV, "IngestFailures", [("Service", "ingest")], "Sum"),
    "aurora_acu_avg": ("AWS/RDS", "ServerlessDatabaseCapacity", [("DBClusterIdentifier", f"sdge-rag-{ENV}")], "Average"),
    "insights_chat_cpu_pct": ("AWS/ECS", "CPUUtilization", [("ClusterName", f"sdge-rag-{ENV}"), ("ServiceName", f"sdge-insights-{INSIGHTS_ENV}-chat")], "Average"),
}


def get_metrics(names=None, hours=24):
    hours = max(1, min(int(hours), 24 * 14))
    names = [n for n in (names or list(_METRICS)) if n in _METRICS] or list(_METRICS)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    period = 300 if hours <= 24 else 3600
    queries = []
    for i, n in enumerate(names):
        ns, metric, dims, stat = _METRICS[n]
        queries.append({"Id": f"m{i}", "Label": n, "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": ns, "MetricName": metric, "Dimensions": [{"Name": k, "Value": v} for k, v in dims]},
            "Period": period, "Stat": stat}})
    r = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
    out = {}
    for res in r["MetricDataResults"]:
        vals = res.get("Values", [])
        stat = _METRICS[res["Label"]][3]
        if stat == "Sum":
            summary = round(sum(vals), 2)
        elif vals:
            summary = round(max(vals) if stat.startswith("p") else sum(vals) / len(vals), 3)
        else:
            summary = None
        series = sorted(zip([t.isoformat(timespec="minutes") for t in res.get("Timestamps", [])], vals))
        out[res["Label"]] = {"stat": stat, "value": summary, "points": series[-48:]}
    return {"hours": hours, "periodSeconds": period, "metrics": out}


# ------------------------------------------------------------------ inventory
def list_resources(include_details=False):
    stacks, total_types = [], Counter()
    for region, name in STACKS:
        cfn = boto3.client("cloudformation", region_name=region)
        try:
            st = cfn.describe_stacks(StackName=name)["Stacks"][0]
        except cfn.exceptions.ClientError:
            continue
        res = []
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=name):
            res.extend(page["StackResourceSummaries"])
        types = Counter(r["ResourceType"] for r in res)
        total_types.update(types)
        entry = {
            "stack": name, "region": region, "status": st["StackStatus"],
            "lastUpdated": str(st.get("LastUpdatedTime") or st.get("CreationTime")),
            "resourceCount": len(res), "resourceTypes": dict(sorted(types.items())),
            "description": st.get("Description", "").strip(),
        }
        if include_details:
            entry["resources"] = [{"logicalId": r["LogicalResourceId"], "type": r["ResourceType"],
                                   "physicalId": r.get("PhysicalResourceId", ""), "status": r["ResourceStatus"]} for r in res]
        stacks.append(entry)
    return redact({
        "stacks": stacks,
        "totals": {"stacks": len(stacks), "resources": sum(total_types.values()),
                   "distinctResourceTypes": len(total_types),
                   "awsServices": sorted({t.split("::")[1] for t in total_types if t.startswith("AWS::")}),
                   "byType": dict(sorted(total_types.items()))},
        "notInStacks": ["S3 bucket sdge-rag-artifacts-<account>-us-east-2 (Lambda deployment packages, created by scripts/deploy.sh)",
                        "Route 53 hosted zone awsteach.com (pre-existing)",
                        "ACM DNS-validation CNAME records (created by ACM)"],
    })


def get_alarms():
    out = []
    for prefix in (f"sdge-rag-{ENV}", f"sdge-insights-{INSIGHTS_ENV}"):
        for a in cw.describe_alarms(AlarmNamePrefix=prefix)["MetricAlarms"]:
            out.append({"name": a["AlarmName"], "state": a["StateValue"], "metric": f'{a["Namespace"]}/{a["MetricName"]}',
                        "threshold": a.get("Threshold"), "description": a.get("AlarmDescription", ""),
                        "updated": str(a.get("StateUpdatedTimestamp"))})
    return {"alarms": out}


def get_pipeline_runs(limit=5):
    cp = boto3.client("codepipeline", region_name=REGION)
    name = f"sdge-rag-{ENV}"
    try:
        r = cp.list_pipeline_executions(pipelineName=name, maxResults=max(1, min(int(limit), 20)))
        state = cp.get_pipeline_state(name=name)
    except cp.exceptions.PipelineNotFoundException:
        return {"pipeline": name, "runs": [], "note": "pipeline not deployed"}
    return {"pipeline": name,
            "stages": [{"stage": s["stageName"], "status": (s.get("latestExecution") or {}).get("status")} for s in state["stageStates"]],
            "runs": [{"id": e["pipelineExecutionId"], "status": e["status"], "start": str(e.get("startTime")),
                      "end": str(e.get("lastUpdateTime")), "trigger": (e.get("trigger") or {}).get("triggerType")}
                     for e in r["pipelineExecutionSummaries"]]}


def get_document_library():
    """Names of documents in the Gas Ops knowledge base (S3 object listing only)."""
    bucket = f"sdge-rag-{ENV}-docs-{os.environ.get('ACCOUNT_ID', '')}-{REGION}"
    docs = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="uploads/"):
        for o in page.get("Contents", []):
            parts = o["Key"].split("/", 2)
            docs.append({"name": parts[2] if len(parts) == 3 else o["Key"], "bytes": o["Size"],
                         "uploaded": str(o["LastModified"])})
    return {"bucket": bucket, "count": len(docs), "documents": docs}


# ------------------------------------------------------------------ Model Lab (AppConfig, read-only data plane)
def get_model_selection():
    """Live Model Lab state from AWS AppConfig: weights, active model, ranking, recent changes."""
    acd = boto3.client("appconfigdata", region_name=REGION)
    tok = acd.start_configuration_session(ApplicationIdentifier=f"sdge-rag-{ENV}", EnvironmentIdentifier=ENV,
                                          ConfigurationProfileIdentifier="model-selection")["InitialConfigurationToken"]
    cfg = json.loads(acd.get_latest_configuration(ConfigurationToken=tok)["Configuration"].read() or "{}")
    names = {m["id"]: m["name"] for m in cfg.get("models", [])}
    ev = cfg.get("lastEvaluation") or {}
    return redact({
        "appconfig": {"application": f"sdge-rag-{ENV}", "environment": ENV, "profile": "model-selection"},
        "weights": cfg.get("weights"), "activeModelId": cfg.get("activeModelId"),
        "activeModelName": names.get(cfg.get("activeModelId")), "selectionPolicy": cfg.get("selectionPolicy"),
        "catalog": [{k: m.get(k) for k in ("id", "name", "vendor", "qualityPrior", "pricePer1M", "reasoning", "openWeights", "strengths")}
                    for m in cfg.get("models", [])],
        "lastEvaluation": {k: ev.get(k) for k in ("at", "by", "weights", "measuredAt", "decision", "ranking")},
        "measurements": ev.get("measurements"),
        "history": [dict(h, fromName=names.get(h.get("from")), toName=names.get(h.get("to"))) for h in cfg.get("history", [])[:15]],
    })


# ------------------------------------------------------------------ ECS Fargate
_ecs = boto3.client("ecs", region_name=REGION)


def get_fargate_status():
    """ECS cluster sdge-rag-<env>: services (desired/running, task size, arch, image, rollout), running and
    recently stopped tasks (incl. one-off ingest tasks with their exit codes)."""
    cluster = f"sdge-rag-{ENV}"
    svc_arns = _ecs.list_services(cluster=cluster, maxResults=20).get("serviceArns", [])
    services = []
    for svc in (_ecs.describe_services(cluster=cluster, services=svc_arns)["services"] if svc_arns else []):
        td = _ecs.describe_task_definition(taskDefinition=svc["taskDefinition"])["taskDefinition"]
        c = td["containerDefinitions"][0]
        services.append({
            "service": svc["serviceName"], "status": svc["status"], "desired": svc["desiredCount"],
            "running": svc["runningCount"], "pending": svc["pendingCount"], "launchType": svc.get("launchType"),
            "taskDefinition": svc["taskDefinition"].split("/")[-1],
            "vCPU": int(td.get("cpu", 0)) / 1024, "memoryGiB": int(td.get("memory", 0)) / 1024,
            "architecture": (td.get("runtimePlatform") or {}).get("cpuArchitecture"), "image": c["image"].split("/")[-1],
            "exposedVia": "API Gateway REST -> VPC Link -> internal NLB" if svc.get("loadBalancers")
                          else "HTTP API -> VPC Link -> Cloud Map" if svc.get("serviceRegistries") else "none",
            "rollout": [{"status": d["status"], "rolloutState": d.get("rolloutState"), "taskDefinition": d["taskDefinition"].split("/")[-1],
                         "running": d["runningCount"], "updated": str(d.get("updatedAt"))} for d in svc.get("deployments", [])],
            "recentEvents": [e["message"] for e in svc.get("events", [])[:5]],
        })
    tasks = []
    for status in ("RUNNING", "STOPPED"):
        arns = _ecs.list_tasks(cluster=cluster, desiredStatus=status, maxResults=20).get("taskArns", [])
        for t in (_ecs.describe_tasks(cluster=cluster, tasks=arns)["tasks"] if arns else []):
            ct = (t.get("containers") or [{}])[0]
            tasks.append({"task": t["taskArn"].split("/")[-1][:12], "group": t.get("group"), "startedBy": t.get("startedBy"),
                          "lastStatus": t.get("lastStatus"), "vCPU": int(t.get("cpu") or 0) / 1024,
                          "memoryGiB": int(t.get("memory") or 0) / 1024,
                          "createdAt": str(t.get("createdAt")), "startedAt": str(t.get("startedAt")),
                          "stoppedAt": str(t.get("stoppedAt")) if t.get("stoppedAt") else None,
                          "stoppedReason": t.get("stoppedReason"), "exitCode": ct.get("exitCode"),
                          "health": t.get("healthStatus")})
    tasks.sort(key=lambda t: t["createdAt"], reverse=True)
    return redact({"cluster": cluster, "services": services, "tasks": tasks[:25],
                   "note": "Stopped tasks are retained by ECS for about an hour."})


# ------------------------------------------------------------------ Bedrock Knowledge Bases
PROJECT_KB_ID = os.environ.get("PROJECT_KB_ID", "")
_agent = boto3.client("bedrock-agent", region_name=REGION)
_agent_rt = boto3.client("bedrock-agent-runtime", region_name=REGION)


def search_project_kb(query, max_results=6):
    """Semantic search over the project corpus KB: source code, IaC templates, design narrative, live inventory."""
    max_results = max(1, min(int(max_results or 6), 10))
    r = _agent_rt.retrieve(knowledgeBaseId=PROJECT_KB_ID, retrievalQuery={"text": str(query)[:1000]},
                           retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": max_results}})
    out = []
    for x in r["retrievalResults"]:
        uri = x.get("location", {}).get("s3Location", {}).get("uri", "")
        path = uri.split("/kb/", 1)[-1]
        if path.startswith("source/"):
            path = path[len("source/"):].removesuffix(".txt")
        out.append({"file": path, "score": round(float(x.get("score", 0)), 3), "text": _clip(x["content"].get("text", ""), 2500)})
    return redact({"knowledgeBaseId": PROJECT_KB_ID, "query": query, "results": out})


def _ds_id(kb_id):
    return _agent.list_data_sources(knowledgeBaseId=kb_id, maxResults=5)["dataSourceSummaries"][0]["dataSourceId"]


def sync_project_kb(reason):
    """Start an incremental ingestion job (waits briefly if one is already running)."""
    if not PROJECT_KB_ID:
        return None
    ds = _ds_id(PROJECT_KB_ID)
    for _ in range(12):
        running = _agent.list_ingestion_jobs(knowledgeBaseId=PROJECT_KB_ID, dataSourceId=ds, maxResults=5, filters=[
            {"attribute": "STATUS", "operator": "EQ", "values": ["STARTING", "IN_PROGRESS"]}])["ingestionJobSummaries"]
        if not running:
            try:
                return _agent.start_ingestion_job(knowledgeBaseId=PROJECT_KB_ID, dataSourceId=ds,
                                                  description=reason[:200])["ingestionJob"]["ingestionJobId"]
            except _agent.exceptions.ConflictException:
                pass
        time.sleep(5)
    return None


def get_knowledge_bases():
    """Both Bedrock Knowledge Bases (Gas Ops documents, project corpus): config and recent sync jobs."""
    kbs = []
    for k in _agent.list_knowledge_bases(maxResults=50)["knowledgeBaseSummaries"]:
        if not k["name"].startswith(("sdge-rag-", "sdge-insights-")):
            continue
        detail = _agent.get_knowledge_base(knowledgeBaseId=k["knowledgeBaseId"])["knowledgeBase"]
        ds = _ds_id(k["knowledgeBaseId"])
        jobs = _agent.list_ingestion_jobs(knowledgeBaseId=k["knowledgeBaseId"], dataSourceId=ds, maxResults=5,
                                          sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"})["ingestionJobSummaries"]
        kbs.append({"name": k["name"], "id": k["knowledgeBaseId"], "status": k["status"], "description": k.get("description"),
                    "vectorStore": detail["storageConfiguration"]["type"],
                    "embeddingModel": detail["knowledgeBaseConfiguration"]["vectorKnowledgeBaseConfiguration"]["embeddingModelArn"].split("/")[-1],
                    "recentSyncs": [{"started": str(j["startedAt"]), "status": j["status"], "trigger": j.get("description"),
                                     "stats": {kk: vv for kk, vv in j.get("statistics", {}).items() if vv}} for j in jobs]})
    return redact({"knowledgeBases": kbs})


# ------------------------------------------------------------------ knowledge file
_kcache = {"ts": 0, "text": ""}


def get_knowledge(max_age=60):
    if time.time() - _kcache["ts"] > max_age:
        body = s3.get_object(Bucket=KNOWLEDGE_BUCKET, Key=KNOWLEDGE_KEY)["Body"].read().decode()
        _kcache.update(ts=time.time(), text=body)
    return _kcache["text"]


# ------------------------------------------------------------------ tool specs (Bedrock Converse)
TOOLS = {
    "search_logs": search_logs,
    "get_llm_traces": get_llm_traces,
    "get_metrics": get_metrics,
    "list_resources": list_resources,
    "get_alarms": get_alarms,
    "get_pipeline_runs": get_pipeline_runs,
    "get_document_library": get_document_library,
    "get_model_selection": get_model_selection,
    "search_project_kb": search_project_kb,
    "get_fargate_status": get_fargate_status,
    "get_knowledge_bases": get_knowledge_bases,
}

TOOL_SPECS = [
    {"toolSpec": {"name": "search_logs", "description":
        "Fetch recent CloudWatch log events for one component of the deployment. Use for questions about errors, "
        "recent activity, ingestion runs, sign-ups, API access, or pipeline builds.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "component": {"type": "string", "enum": sorted(LOG_GROUPS)},
            "filter_text": {"type": "string", "description": "optional text to match"},
            "level": {"type": "string", "enum": ["", "ERROR", "WARNING", "INFO"]},
            "minutes": {"type": "integer", "description": "look-back window (default 60, max 10080)"},
            "limit": {"type": "integer", "description": "max events (default 25)"}},
            "required": ["component"]}}}},
    {"toolSpec": {"name": "get_llm_traces", "description":
        "Fetch recent LLM prompt traces with the model's reasoning. app='main' is the Gas Ops RAG assistant "
        "(includes retrieved passages and similarity scores); app='insights' is this chatbot (includes tool calls).",
        "inputSchema": {"json": {"type": "object", "properties": {
            "app": {"type": "string", "enum": ["all", "main", "insights"]},
            "limit": {"type": "integer", "description": "max traces (default 5)"},
            "detail": {"type": "string", "enum": ["summary", "full"], "description": "full includes complete prompts"}}}}}},
    {"toolSpec": {"name": "get_metrics", "description":
        "Fetch CloudWatch metric summaries (sum/avg/p90 over the window plus a short time series).",
        "inputSchema": {"json": {"type": "object", "properties": {
            "names": {"type": "array", "items": {"type": "string", "enum": sorted(_METRICS)}},
            "hours": {"type": "integer", "description": "look-back window in hours (default 24)"}}}}}},
    {"toolSpec": {"name": "list_resources", "description":
        "Live inventory of every CloudFormation stack in this class deployment: resource counts by AWS resource type, "
        "stack status and last update. Set include_details for per-resource logical/physical IDs.",
        "inputSchema": {"json": {"type": "object", "properties": {"include_details": {"type": "boolean"}}}}}},
    {"toolSpec": {"name": "get_alarms", "description": "Current state of all CloudWatch alarms.",
                  "inputSchema": {"json": {"type": "object", "properties": {}}}}},
    {"toolSpec": {"name": "get_pipeline_runs", "description": "Recent CodePipeline executions and stage status.",
                  "inputSchema": {"json": {"type": "object", "properties": {"limit": {"type": "integer"}}}}}},
    {"toolSpec": {"name": "search_project_kb", "description":
        "Semantic search (Amazon Bedrock Knowledge Base) over the project's actual source code, CloudFormation "
        "templates, deploy scripts, buildspecs, Model Lab catalog and the design narrative. Use it for 'show me the code / "
        "template / IAM policy / how is X implemented' questions, then quote the relevant snippet and name the file.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for, in natural language or with identifiers"},
            "max_results": {"type": "integer", "description": "1-10, default 6"}}, "required": ["query"]}}}},
    {"toolSpec": {"name": "get_fargate_status", "description":
        "Live Amazon ECS on Fargate status: the Gas Ops API service and Class Insights chat service (desired/running "
        "tasks, CPU/memory size, arm64, image tag, deployment rollout, recent service events) and running/recently "
        "stopped tasks, including one-off document-ingest tasks launched by EventBridge with their exit codes.",
        "inputSchema": {"json": {"type": "object", "properties": {}}}}},
    {"toolSpec": {"name": "get_knowledge_bases", "description":
        "Status of both Amazon Bedrock Knowledge Bases (Gas Ops document library; project corpus): vector store, "
        "embedding model and recent sync (ingestion) jobs with document counts.",
        "inputSchema": {"json": {"type": "object", "properties": {}}}}},
    {"toolSpec": {"name": "get_model_selection", "description":
        "Live Model Lab state from AWS AppConfig: which LLM the Gas Ops assistant is using right now, the students' "
        "quality/cost/latency weights, the 8-model catalog with prices, the latest benchmark measurements and ranking, "
        "and the history of who changed the model and why.",
        "inputSchema": {"json": {"type": "object", "properties": {}}}}},
    {"toolSpec": {"name": "get_document_library", "description":
        "List the documents currently uploaded to the Gas Ops assistant's knowledge base.",
        "inputSchema": {"json": {"type": "object", "properties": {}}}}},
]


def run_tool(name, args):
    fn = TOOLS.get(name)
    if not fn:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return {"error": f"bad arguments: {e}"}
    except Exception as e:  # noqa: BLE001
        log.exception("tool failed", extra={"tool": name})
        return {"error": f"{type(e).__name__}: {e}"}
