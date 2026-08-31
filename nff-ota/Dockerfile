# nff-ota — OTA firmware update MCP server (streamable-http, port 3002).
# Multi-stage: build a venv with the package installed, then copy it into a
# slim runtime image.

FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY nff_ota/ nff_ota/

RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install .

FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /venv /venv
COPY nff_ota/ nff_ota/

ENV PATH="/venv/bin:$PATH"

# Streamable-http MCP transport (Claude + nff-github connect here).
EXPOSE 3002

ENTRYPOINT ["nff-ota"]
