# nff-mock-l2 — production image (Level 2). Heavy: QEMU + the supervisor.
# Build context is the nff-mock/ ROOT so the shared core (core/) is in scope. See Dockerfile.dev
# for the ESP_QEMU_URL build-arg (the Espressif qemu-system-xtensa release to bundle).
FROM python:3.12-slim AS builder
WORKDIR /app
COPY core/ core/
COPY qemu/ qemu/
RUN pip install --no-cache-dir --upgrade pip build && \
    pip wheel --no-cache-dir --wheel-dir /wheels ./core && \
    pip wheel --no-cache-dir --wheel-dir /wheels --find-links /wheels ./qemu

FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        ncat ca-certificates xz-utils curl \
    && rm -rf /var/lib/apt/lists/*

ARG ESP_QEMU_URL=""
RUN if [ -n "$ESP_QEMU_URL" ]; then \
        curl -fsSL "$ESP_QEMU_URL" -o /tmp/qemu.tar.xz && \
        mkdir -p /opt/qemu && tar -xJf /tmp/qemu.tar.xz -C /opt/qemu --strip-components=1 && \
        rm /tmp/qemu.tar.xz && ln -sf /opt/qemu/bin/qemu-system-xtensa /usr/local/bin/qemu-system-xtensa ; \
    else \
        echo "WARNING: ESP_QEMU_URL unset — qemu-system-xtensa NOT installed (doctor works, run will not)"; \
    fi

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir esptool /wheels/*.whl && rm -rf /wheels

ENTRYPOINT ["nff-mock-l2", "run"]
