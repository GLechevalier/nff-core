# nff-pentester — IoT security assessment MCP server (streamable-http, port 3006).
# Multi-stage: build a venv with the package installed, then copy it into a
# slim runtime image.

FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY nff_pentester/ nff_pentester/

RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install .

FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /venv /venv
COPY nff_pentester/ nff_pentester/

ENV PATH="/venv/bin:$PATH"

# Bind to all interfaces inside the container so it is reachable on the nff
# network; the source default stays loopback. Publish the host port to
# 127.0.0.1 only (the server has no built-in auth).
ENV PENTESTER_MCP_HOST=0.0.0.0

# Streamable-http MCP transport (the agent-worker connects here).
EXPOSE 3006

ENTRYPOINT ["nff-pentester"]
