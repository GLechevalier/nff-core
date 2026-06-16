# nff-mock — production image. Slim, non-root, one virtual device per container.
FROM python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml README.md ./
COPY nff_mock/ nff_mock/
RUN pip install --no-cache-dir --upgrade pip build && \
    pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels && \
    useradd --create-home --uid 10001 mock
USER mock

ENTRYPOINT ["nff-mock", "run"]
