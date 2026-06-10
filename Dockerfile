FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY nff_fleet/ nff_fleet/

RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install .

FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /venv /venv
COPY nff_fleet/ nff_fleet/

ENV PATH="/venv/bin:$PATH"

EXPOSE 8883 3001

ENTRYPOINT ["nff-fleet", "start"]
