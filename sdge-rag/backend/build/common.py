"""Shared helpers: config (Parameter Store), DB access (RDS Data API),
Bedrock calls, structured logging and CloudWatch EMF metrics."""
import json
import logging
import os
import time

import boto3
from botocore.config import Config

import runtime

runtime.setup_logging()   # no-op on Lambda; Lambda-shaped JSON logs on Fargate

REGION = os.environ.get("AWS_REGION", "us-east-2")
ENV_NAME = os.environ.get("ENV_NAME", "dev")
PARAM_PREFIX = os.environ.get("PARAM_PREFIX", f"/sdge-rag/{ENV_NAME}/")
DB_CLUSTER_ARN = os.environ.get("DB_CLUSTER_ARN")
DB_SECRET_ARN = os.environ.get("DB_SECRET_ARN")
DB_NAME = os.environ.get("DB_NAME", "ragdb")
EMBED_DIM = 1024

# Lambda's JSON log format (LoggingConfig.LogFormat=JSON) renders `extra=`
# fields as top-level JSON keys, giving structured, queryable logs.
log = logging.getLogger("sdge-rag")
log.setLevel(logging.INFO)

_retry = Config(retries={"max_attempts": 6, "mode": "adaptive"})
ssm = boto3.client("ssm", region_name=REGION)
rds = boto3.client("rds-data", region_name=REGION, config=_retry)
bedrock = boto3.client("bedrock-runtime", region_name=REGION,
                       config=Config(retries={"max_attempts": 4, "mode": "adaptive"}, read_timeout=60))

_cfg = {"ts": 0, "values": {}}


def config(name, default=None):
    """Read config from Parameter Store with a 5-minute in-memory cache."""
    if time.time() - _cfg["ts"] > 300:
        vals = {}
        paginator = ssm.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=PARAM_PREFIX, Recursive=True):
            for p in page["Parameters"]:
                vals[p["Name"][len(PARAM_PREFIX):]] = p["Value"]
        _cfg.update(ts=time.time(), values=vals)
    return _cfg["values"].get(name, default)


# ---------------------------------------------------------------- database
def _param(name, value):
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    if isinstance(value, bool):
        return {"name": name, "value": {"booleanValue": value}}
    if isinstance(value, int):
        return {"name": name, "value": {"longValue": value}}
    if isinstance(value, float):
        return {"name": name, "value": {"doubleValue": value}}
    return {"name": name, "value": {"stringValue": str(value)}}


def sql(statement, params=None, retries=8):
    """Execute a statement; returns list of dict rows. Retries while an
    auto-paused Aurora cluster resumes."""
    args = dict(resourceArn=DB_CLUSTER_ARN, secretArn=DB_SECRET_ARN, database=DB_NAME,
                sql=statement, formatRecordsAs="JSON",
                parameters=[_param(k, v) for k, v in (params or {}).items()])
    for attempt in range(retries):
        try:
            resp = rds.execute_statement(**args)
            return json.loads(resp.get("formattedRecords") or "[]")
        except rds.exceptions.DatabaseResumingException:
            log.warning("database resuming", extra={"attempt": attempt})
            time.sleep(min(2 + attempt * 2, 10))
        except rds.exceptions.BadRequestException as e:
            if "resuming" in str(e).lower() or "communications link" in str(e).lower():
                time.sleep(min(2 + attempt * 2, 10))
                continue
            raise
    raise RuntimeError("database unavailable")


def sql_batch(statement, param_sets):
    if not param_sets:
        return
    rds.batch_execute_statement(
        resourceArn=DB_CLUSTER_ARN, secretArn=DB_SECRET_ARN, database=DB_NAME, sql=statement,
        parameterSets=[[_param(k, v) for k, v in ps.items()] for ps in param_sets])


# ---------------------------------------------------------------- bedrock
def embed(text):
    model = config("embedding-model-id", "amazon.titan-embed-text-v2:0")
    body = json.dumps({"inputText": text[:30000], "dimensions": EMBED_DIM, "normalize": True})
    resp = bedrock.invoke_model(modelId=model, body=body, contentType="application/json", accept="application/json")
    return json.loads(resp["body"].read())["embedding"]


def vec_literal(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


# ---------------------------------------------------------------- metrics
def metric(service, **values):
    """Emit CloudWatch metrics: Embedded Metric Format on Lambda, PutMetricData on Fargate."""
    if not runtime.IN_LAMBDA:
        runtime.put_metrics(f"SdgeRag/{ENV_NAME}", service, values)
        return
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": f"SdgeRag/{ENV_NAME}",
                "Dimensions": [["Service"]],
                "Metrics": [{"Name": k, "Unit": "Milliseconds" if k.endswith("Ms") else "None"} for k in values],
            }],
        },
        "Service": service,
        **values,
    }))
