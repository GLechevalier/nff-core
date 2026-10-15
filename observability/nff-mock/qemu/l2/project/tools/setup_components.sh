#!/usr/bin/env bash
# Link the real nff-sdk-c into this project as the IDF component `nff`. Run once on the toolchain
# box before `idf.py build` (the same convention the SDK examples + the Stage-0 spike use). IDF
# auto-discovers components/, and the component name `nff` matches the REQUIRES in main/CMakeLists.txt
# and components/nff_l2_net/CMakeLists.txt.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"          # .../nff-mock/qemu/l2/project
sdk="$(cd "$here/../../../../nff-sdk-c" && pwd)"   # repo-root/nff-sdk-c
link="$here/components/nff"

mkdir -p "$here/components"
if [ -e "$link" ] || [ -L "$link" ]; then
    rm -rf "$link"
fi
ln -s "$sdk" "$link"
echo "linked components/nff -> $sdk"
