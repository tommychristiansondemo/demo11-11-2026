"""Fargate entry point for the Class Insights chatbot: gunicorn serves this WSGI app on :8080.
HTTP API -> VPC Link -> Cloud Map (service discovery, no load balancer) -> this container.
Same chat.handler that used to run on Lambda; the HTTP API JWT authorizer still guards the route."""
import runtime

runtime.setup_logging()
import chat  # noqa: E402

app = runtime.make_app(chat.handler, style="httpapi")
