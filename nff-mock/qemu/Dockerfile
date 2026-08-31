# nff-mock-l2 — production image (Level 2). Heavy: the real firmware booted in qemu-system-xtensa.
#
# Based on espressif/idf:v5.3.1, which bundles qemu-system-xtensa (the `esp32` machine) AND
# nvs_partition_gen.py (the NVS credential-splice tool) — the toolchain the Stage-3/Stage-4 live
# runs proved green. The merged.bin is produced server-side (Stage 4); the supervisor only needs
# qemu + nvs-gen. Build context is the nff-mock/ ROOT so the shared core (core/) is in scope.
FROM espressif/idf:v5.3.1

WORKDIR /app

# ncat bridges the emulated NIC to in-cluster services (broker / OTA origin) via QEMU guestfwd.
RUN apt-get update && apt-get install -y --no-install-recommends ncat \
    && rm -rf /var/lib/apt/lists/*

COPY core/ core/
COPY qemu/ qemu/

# Install into the IDF python env (export.sh's) so the supervisor and nvs_partition_gen.py share
# one interpreter — credentials.generate_nvs_bin shells out via sys.executable.
RUN . "$IDF_PATH/export.sh" && \
    pip install --no-cache-dir ./core && \
    pip install --no-cache-dir ./qemu

COPY qemu/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# The entrypoint sources export.sh then execs `nff-mock-l2 "$@"`; CMD is the default subcommand.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["run"]
