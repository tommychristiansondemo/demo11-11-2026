"""Container runtime glue: lets the same handler code run on Lambda *or* on ECS Fargate.

On Fargate there is no Lambda runtime, so this module provides what Lambda gave us for free:
  * JSON structured logging identical in shape to Lambda's LogFormat=JSON (so CloudWatch
    Logs Insights queries, metric filters and the Class Insights trace reader keep working);
  * a WSGI adapter that turns an HTTP request into the API Gateway event the handler expects;
  * JWT verification of the Cognito token (defence in depth: API Gateway already validated it,
    but the container re-checks the signature so nothing inside the VPC can spoof a user).
Used by both images (Gas Ops API/ingest and Class Insights chat)."""
import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

IN_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
_STD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "taskName"}


class LambdaStyleJson(logging.Formatter):
    def format(self, r):
        doc = {"timestamp": datetime.fromtimestamp(r.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
               "level": r.levelname, "message": r.getMessage(), "logger": r.name}
        doc.update({k: v for k, v in vars(r).items() if k not in _STD and not k.startswith("_")})
        if r.exc_info:
            doc["errorType"] = r.exc_info[0].__name__
            doc["errorMessage"] = str(r.exc_info[1])
            doc["stackTrace"] = self.formatException(r.exc_info).splitlines()
        return json.dumps(doc, default=str)


def setup_logging():
    if IN_LAMBDA:
        return
    root = logging.getLogger()
    root.handlers[:] = []
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(LambdaStyleJson())
    root.addHandler(h)
    root.setLevel(logging.INFO)
    for noisy in ("botocore", "boto3", "urllib3", "gunicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ------------------------------------------------------------------ metrics outside Lambda
# CloudWatch only extracts Embedded Metric Format from Lambda logs (or the CloudWatch agent),
# so on Fargate metrics are published with PutMetricData on a background thread.
_cw = None


def put_metrics(namespace, service, values):
    global _cw
    if _cw is None:
        import boto3
        _cw = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-2"))

    def send():
        try:
            _cw.put_metric_data(Namespace=namespace, MetricData=[
                {"MetricName": k, "Dimensions": [{"Name": "Service", "Value": service}], "Value": float(v),
                 "Unit": "Milliseconds" if k.endswith("Ms") else "None"} for k, v in values.items()])
        except Exception:  # noqa: BLE001
            logging.getLogger("sdge-runtime").warning("put_metric_data failed", exc_info=True)
    threading.Thread(target=send, daemon=True).start()


# ------------------------------------------------------------------ Cognito JWT verification
_jwks = {}


def verify_jwt(auth_header):
    """Return the verified claims of a Cognito ID/access token, or None."""
    import jwt  # PyJWT
    token = (auth_header or "").removeprefix("Bearer ").strip()
    if not token:
        return None
    pool, client = os.environ["USER_POOL_ID"], os.environ["USER_POOL_CLIENT_ID"]
    issuer = f"https://cognito-idp.{pool.split('_')[0]}.amazonaws.com/{pool}"
    if issuer not in _jwks:
        _jwks[issuer] = jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True, lifespan=3600)
    try:
        key = _jwks[issuer].get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=issuer, options={"verify_aud": False}, leeway=30)
    except jwt.PyJWTError as e:
        logging.getLogger("sdge-runtime").warning("jwt rejected", extra={"reason": str(e)})
        return None
    if claims.get("token_use") == "id" and claims.get("aud") != client:
        return None
    if claims.get("token_use") == "access" and claims.get("client_id") != client:
        return None
    # API Gateway renders list claims as strings; mimic that so handler code is unchanged
    return {k: (" ".join(map(str, v)) if isinstance(v, list) else str(v)) for k, v in claims.items()}


# ------------------------------------------------------------------ WSGI adapter
def make_app(handler, style="rest", routes_prefix="/api/"):
    """style='rest' builds an API Gateway REST (v1) proxy event; 'httpapi' builds an HTTP API (v2) event."""
    log = logging.getLogger("sdge-runtime")

    def app(environ, start_response):
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET")
        if path == "/healthz":
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]
        if not path.startswith(routes_prefix):
            start_response("404 Not Found", [("Content-Type", "application/json")])
            return [b'{"error":"not found"}']
        claims = verify_jwt(environ.get("HTTP_AUTHORIZATION"))
        if claims is None:
            start_response("401 Unauthorized", [("Content-Type", "application/json")])
            return [b'{"error":"Unauthorized"}']
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length).decode() if length else None
        qs = {}
        for part in filter(None, (environ.get("QUERY_STRING") or "").split("&")):
            k, _, v = part.partition("=")
            from urllib.parse import unquote_plus
            qs[unquote_plus(k)] = unquote_plus(v)
        headers = {k[5:].replace("_", "-").lower(): v for k, v in environ.items() if k.startswith("HTTP_")}
        request_id = headers.get("x-amzn-apigateway-request-id") or headers.get("x-amzn-requestid") or uuid.uuid4().hex
        if style == "rest":
            event = {"httpMethod": method, "path": path, "headers": headers, "queryStringParameters": qs or None,
                     "body": body, "requestContext": {"requestId": request_id, "authorizer": {"claims": claims}}}
        else:
            event = {"version": "2.0", "rawPath": path, "headers": headers, "queryStringParameters": qs or None,
                     "body": body, "requestContext": {"requestId": request_id, "http": {"method": method, "path": path},
                                                      "authorizer": {"jwt": {"claims": claims}}}}
        t0 = time.time()
        try:
            r = handler(event, None)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error", extra={"path": path})
            r = {"statusCode": 500, "headers": {"Content-Type": "application/json"}, "body": '{"error":"Internal error"}'}
        status = int(r.get("statusCode", 200))
        out = (r.get("body") or "").encode()
        hdrs = [(k, str(v)) for k, v in (r.get("headers") or {}).items()]
        start_response(f"{status} {'OK' if status < 400 else 'Error'}", hdrs + [("Content-Length", str(len(out)))])
        log.info("http", extra={"method": method, "path": path, "status": status, "ms": int((time.time() - t0) * 1000)})
        return [out]
    return app
