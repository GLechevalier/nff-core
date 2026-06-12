# nff-diagnosis — ESP32 crash-diagnosis FastAPI service (uvicorn, port 8080).

FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY nff_diagnosis/ nff_diagnosis/

RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install .

FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /venv /venv
COPY nff_diagnosis/ nff_diagnosis/

ENV PATH="/venv/bin:$PATH" \
    NFF_STORE_ROOT=/data/store \
    NFF_DB_PATH=/data/crashes.db

EXPOSE 8080
EXPOSE 3005

# The CLI defaults to 127.0.0.1; bind 0.0.0.0 so the port is reachable from
# outside the container. The MCP server (port 3005) is the same image launched
# with `entrypoint: ["nff-diag", "start"]` (see docker-compose service nff-diagnosis-mcp).
ENTRYPOINT ["nff-diag", "server", "start", "--host", "0.0.0.0", "--port", "8080"]
