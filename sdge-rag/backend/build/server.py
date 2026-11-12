"""Fargate entry point for the Gas Ops API: gunicorn serves this WSGI app on :8080.
API Gateway (REST) -> VPC Link -> internal NLB -> this container. The handler is the same
api.handler that used to run on Lambda; runtime.make_app builds the event it expects."""
import runtime

runtime.setup_logging()
import api  # noqa: E402

app = runtime.make_app(api.handler, style="rest")
