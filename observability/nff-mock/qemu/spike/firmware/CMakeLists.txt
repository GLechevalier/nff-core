# Top-level ESP-IDF project for the Stage-0 spike. The unmodified nff-sdk-c is pulled in as the
# IDF component `nff` — the spike adds NOTHING to the SDK, it only exercises it.
#
# The SDK's own example (nff-sdk-c/examples/espidf_basic) documents the convention "place
# nff-sdk-c/ under components/nff/". So this project expects `components/nff` to point at the SDK
# (a symlink that tools/build_image.sh creates: components/nff -> ../../../../../nff-sdk-c).
# components/ is auto-discovered by IDF, and the component name `nff` matches `REQUIRES nff`.
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(nff_spike)
