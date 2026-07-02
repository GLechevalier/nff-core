FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY nff_fleet/ nff_fleet/

RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install .

# Xtensa toolchain source — its addr2line symbolicates L2 (QEMU) crash backtraces on ingest
# (nff_fleet.crash). v5.3.1 ships the unified `xtensa-esp-elf-*` that built the L2 firmware ELF.
FROM espressif/idf:v5.3.1 AS xtensa

FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /venv /venv
COPY --from=xtensa /opt/esp/tools/xtensa-esp-elf /opt/esp/tools/xtensa-esp-elf
COPY nff_fleet/ nff_fleet/

ENV PATH="/opt/esp/tools/xtensa-esp-elf/esp-13.2.0_20240530/xtensa-esp-elf/bin:/venv/bin:$PATH"

EXPOSE 8883 3001

ENTRYPOINT ["nff-fleet", "start"]
