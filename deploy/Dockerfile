# AutoHeal Gate backend image.
#
# Build from the repository root:
#   docker build -f deploy/Dockerfile -t autoheal-gate .
#
# SQLite lives in a volume so the container is disposable but the data isn't.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# git and patch are used to apply and validate candidate fixes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git patch curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/backend

COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

# Optional extras are installed only when the matching build arg is set, so
# the default image stays small and free of anything that needs a GPU/model.
ARG WITH_LLM=false
ARG WITH_OBSERVABILITY=false
ARG WITH_RAG=false
COPY backend/requirements-llm.txt backend/requirements-observability.txt backend/requirements-rag.txt ./
RUN if [ "$WITH_LLM" = "true" ]; then pip install -r requirements-llm.txt; fi \
    && if [ "$WITH_OBSERVABILITY" = "true" ]; then pip install -r requirements-observability.txt; fi \
    && if [ "$WITH_RAG" = "true" ]; then pip install -r requirements-rag.txt; fi

COPY backend/ ./
COPY knowledge_base/ /srv/knowledge_base/
COPY sample-repo/ /srv/sample-repo/

ENV RAG_KNOWLEDGE_DIR=/srv/knowledge_base \
    SAMPLE_REPO_PATH=/srv/sample-repo \
    DATABASE_URL=sqlite:////data/autoheal.db \
    WORKSPACE_ROOT=/data/workspaces

RUN mkdir -p /data/workspaces

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
