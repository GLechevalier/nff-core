# nff-mock-controller — on-demand mock spawner (Starlette/uvicorn, port 3004).
# Like nff-github, it spawns containers via the docker *client* (docker.py shells out),
# so the image needs only Python + the docker CLI — no daemon, no toolchain.
#
# Build context is the nff-mock/ ROOT (not controller/) so the shared core (../core) is in
# scope — the controller imports nff_mock_core.scenarios for crash injection.

ARG DOCKER_CLI_IMAGE=docker:27-cli
FROM ${DOCKER_CLI_IMAGE} AS docker-cli

FROM python:3.12-slim AS builder
WORKDIR /build
COPY core/ core/
COPY controller/pyproject.toml controller/README.md controller/
COPY controller/nff_mock_controller/ controller/nff_mock_controller/
RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install ./core && \
    /venv/bin/pip install ./controller

FROM python:3.12-slim
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# docker CLI only — the controller's single external tool.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY --from=builder /venv /venv
COPY controller/nff_mock_controller/ nff_mock_controller/
ENV PATH="/venv/bin:$PATH"

EXPOSE 3004
ENTRYPOINT ["nff-mock-controller"]
