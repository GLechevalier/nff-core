#!/usr/bin/env bash
# Run INSIDE the nff-spike container. Patches the SDK copy for IDF 5.3 component naming, then builds.
set -e
cd /project

# Patch the THROWAWAY container copy only — the real nff-sdk-c stays pristine. These are real
# gaps the spike surfaced in the SDK's ESP-IDF component metadata under a strict IDF 5.3 build:
NFFC=components/nff/CMakeLists.txt
# (idempotent — the container copy persists across runs)
#  1. IDF 5.3 renamed the MQTT component `esp-mqtt` -> `mqtt`.
sed -i 's/\besp-mqtt\b/mqtt/g' "$NFFC"
#  2. nff_port_esp32_idf.c needs esp_ota_ops.h (app_update) + esp_core_dump.h (espcoredump);
#     add both to the IDF REQUIRES list.
grep -q 'app_update' "$NFFC" || sed -i 's/^\(\s*\)REQUIRES$/\1REQUIRES\n\1    app_update/' "$NFFC"
grep -q 'espcoredump' "$NFFC" || sed -i 's/^\(\s*\)REQUIRES$/\1REQUIRES\n\1    espcoredump/' "$NFFC"
#  2b. Fix the stale IDF port: 8-arg set_tls + leaf+intermediate PEM chain, stub non-public panic hook.
python3 /project/patch_idf_port.py
#  2c. The fleet server cert's SAN doesn't include the guest IP (10.13.0.10) the device dials, so
#      esp-mqtt's hostname check would reject it (the Python mock sets tls_insecure_hostname for the
#      same reason). Skip the CN/SAN check (chain is still verified against the pinned root CA).
PORT=components/nff/src/port/nff_port_esp32_idf.c
grep -q 'skip_cert_common_name_check' "$PORT" || sed -i 's|\(\s*\)\.verification\.certificate_len = mh->ca_pem ? strlen(mh->ca_pem) + 1 : 0,|&\n\1.verification.skip_cert_common_name_check = true,|' "$PORT"
#  3. The SDK uses %u for uint32_t (long unsigned on xtensa); IDF builds -Werror=all. Relax it for
#     the nff component only (throwaway spike — the proper fix is PRI-family macros across the SDK).
grep -q 'spike-only: relax' "$NFFC" || cat >> "$NFFC" <<'EOF'

# --- spike-only: relax -Werror for the SDK (container copy; real SDK untouched) ---
if(DEFINED IDF_TARGET)
    target_compile_options(${COMPONENT_LIB} PRIVATE -w)
endif()
EOF
# Normalize the relax line every run to -w (inhibit all warnings; bare -Wno-error doesn't undo
# gcc's specific -Werror=format). Idempotent: the pattern matches -W... not the lowercase -w.
sed -i 's/PRIVATE -W[^)]*/PRIVATE -w/' "$NFFC"
echo "patched REQUIRES block:"; grep -nE "REQUIRES|app_update|espcoredump|mqtt|https_ota|relax|Wno-error" "$NFFC" || true

. "$IDF_PATH/export.sh" >/dev/null 2>&1
rm -rf build
rm -f sdkconfig   # re-apply sdkconfig.defaults (e.g. the QEMU watchdog-off knobs)

echo "=== set-target esp32 ==="
if ! idf.py set-target esp32 >/tmp/st.log 2>&1; then
  echo "SET-TARGET FAILED"; tail -40 /tmp/st.log; exit 1
fi

echo "=== build (boot_epoch=$(date +%s)) ==="
if idf.py build -DNFF_SPIKE_BOOT_EPOCH=$(date +%s) >/tmp/build.log 2>&1; then
  echo "BUILD_OK"
  tail -20 /tmp/build.log
else
  echo "BUILD_FAILED"
  tail -70 /tmp/build.log
  exit 1
fi
