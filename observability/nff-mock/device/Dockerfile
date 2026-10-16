# nff-mock — production image (Level 1). Slim, non-root, one virtual device per container.
# Build context is the nff-mock/ ROOT (not device/) so the shared core (../core) is in scope.
FROM python:3.12-slim AS builder
WORKDIR /app
COPY core/ core/
COPY device/ device/
RUN pip install --no-cache-dir --upgrade pip build && \
    pip wheel --no-cache-dir --wheel-dir /wheels ./core && \
    pip wheel --no-cache-dir --wheel-dir /wheels --find-links /wheels ./device

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels && \
    useradd --create-home --uid 10001 mock
USER mock

ENTRYPOINT ["nff-mock", "run"]
