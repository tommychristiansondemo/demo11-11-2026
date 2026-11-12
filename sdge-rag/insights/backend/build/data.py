"""Class Insights UI endpoints (HTTP API, JWT authorizer = Gas Ops user pool).

  GET /api/knowledge          projectknowledge.md
  GET /api/inventory          live CloudFormation inventory
  GET /api/traces?app=&limit=&detail=
  GET /api/logs?component=&q=&level=&minutes=&limit=
  GET /api/metrics?hours=
  GET /api/alarms
  GET /api/pipeline
  GET /api/components         list of log components
"""
import json

import tools
from tools import log


def resp(status, body, ctype="application/json"):
    return {"statusCode": status, "headers": {"Content-Type": ctype, "Cache-Control": "no-store"},
            "body": body if isinstance(body, str) else json.dumps(body, default=str)}


def handler(event, context):
    path = event.get("rawPath", "")
    q = event.get("queryStringParameters") or {}
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims", {})
    log.info("request", extra={"path": path, "user": claims.get("cognito:username"), "query": q})
    try:
        if path == "/api/knowledge":
            return resp(200, {"markdown": tools.get_knowledge()})
        if path == "/api/inventory":
            return resp(200, tools.list_resources(include_details=q.get("details") == "1"))
        if path == "/api/traces":
            return resp(200, tools.get_llm_traces(app=q.get("app", "all"), limit=int(q.get("limit", 15)),
                                                  detail=q.get("detail", "full")))
        if path == "/api/logs":
            return resp(200, tools.search_logs(component=q.get("component", "main-api"), filter_text=q.get("q", ""),
                                               level=q.get("level", ""), minutes=int(q.get("minutes", 60)),
                                               limit=int(q.get("limit", 50))))
        if path == "/api/metrics":
            return resp(200, tools.get_metrics(hours=int(q.get("hours", 24))))
        if path == "/api/alarms":
            return resp(200, tools.get_alarms())
        if path == "/api/pipeline":
            return resp(200, tools.get_pipeline_runs())
        if path == "/api/components":
            return resp(200, {"components": tools.LOG_GROUPS})
        return resp(404, {"error": f"no route {path}"})
    except ValueError as e:
        return resp(400, {"error": str(e)})
    except Exception as e:  # noqa: BLE001
        log.exception("unhandled", extra={"path": path})
        return resp(500, {"error": f"{type(e).__name__}: {e}"})
