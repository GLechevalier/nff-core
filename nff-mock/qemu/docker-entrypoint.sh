#!/usr/bin/env bash
# Source the ESP-IDF environment, then exec the supervisor CLI.
#
# The image is based on espressif/idf, which ships qemu-system-xtensa, nvs_partition_gen.py, and a
# python env with the tool deps. `export.sh` puts all three on PATH (and sets IDF_PATH, which
# credentials._nvs_partition_gen_cmd resolves the splice tool from). The subcommand is passed
# through as "$@" (CMD default: run), so `command: ["doctor"]` in compose yields `nff-mock-l2
# doctor`, and a plain `docker run <image>` yields `nff-mock-l2 run`.
set -e
. "$IDF_PATH/export.sh" >/dev/null 2>&1
exec nff-mock-l2 "$@"
