# Top-level ESP-IDF project for the maintained nff Level-2 (QEMU) firmware. The real nff-sdk-c is
# pulled in as the IDF component `nff` via components/nff (a symlink that tools/setup_components.sh
# creates: components/nff -> ../../../../../nff-sdk-c — the same convention the SDK examples + the
# Stage-0 spike use). The shared networking/NVS helper lives in components/nff_l2_net/. Both are
# auto-discovered under components/.
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(nff_l2)

# NFF_QEMU must reach EVERY component — especially the nff SDK (components/nff), whose IDF port gates
# the broker hostname-check skip on `#ifdef NFF_QEMU` (the fleet server cert's CN is nff-fleet-server,
# but the device dials the guestfwd IP). A per-component target_compile_definitions on `main` would
# NOT reach the nff component, so set it project-wide here. Real-hardware arduino-cli builds never
# define it, so production keeps full CN/SAN verification.
idf_build_set_property(COMPILE_DEFINITIONS "NFF_QEMU=1" APPEND)
