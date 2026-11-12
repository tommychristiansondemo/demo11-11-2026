# Gas Ops API service + ingest task image (linux/arm64, Graviton Fargate). Build context: repo root.
FROM public.ecr.aws/docker/library/python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY backend/requirements.txt backend/requirements-container.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-container.txt
COPY backend/src/ ./
RUN useradd --system --uid 10001 app
USER 10001
EXPOSE 8080
# default: the API web service. The ingest task overrides the command with ["python", "ingest_task.py"].
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "8", "--timeout", "60", "--graceful-timeout", "20", "server:app"]
