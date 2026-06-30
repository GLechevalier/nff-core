# Top-level ESP-IDF project for the maintained nff Level-2 (QEMU) firmware. The real nff-sdk-c is
# pulled in as the IDF component `nff` via components/nff (a symlink that tools/setup_components.sh
# creates: components/nff -> ../../../../../nff-sdk-c — the same convention the SDK examples + the
# Stage-0 spike use). The shared networking/NVS helper lives in components/nff_l2_net/. Both are
# auto-discovered under components/.
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(nff_l2)
