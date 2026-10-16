#!/usr/bin/env python3
"""Regenerate the Arduino library from this repo (the single source of truth).

The repo uses a nested layout (include/ + src/ + src/port/) and supports four
platforms via mutually-exclusive #if-guarded port files. The Arduino IDE/CLI,
however, needs a *flat* library: every file under src/ is compiled, and the
ESP32 Arduino port contains C++ so it must carry a .cpp extension. This script
performs that transformation so a change in the repo always reaches what a
sketch (e.g. sketches/hello_nff) actually builds against.

Scope: ESP32 only (matches the deployed library). The esp8266 / esp32-idf /
posix ports are intentionally excluded — including them would make the Arduino
build try to compile non-Arduino ports.

Usage:
    python tools/sync_arduino_lib.py [--dest <path-to>/libraries/nff] [--quiet]

Dest resolution order:
    1. --dest argument
    2. `arduino-cli config get directories.user` + /libraries/nff
    3. ~/Documents/Arduino/libraries/nff
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Top-level src/*.c files that are platform-agnostic (everything except port/).
# Globbed at runtime, but listed here for documentation/clarity.
EXCLUDED_PORTS = {
    "nff_port_esp8266_arduino.c",  # ESP8266 (BearSSL) — not in ESP32-only lib
    "nff_port_esp32_idf.c",        # ESP-IDF native — not Arduino
    "nff_port_posix.c",            # host tests — not Arduino
}
# The one port the Arduino lib ships, renamed .c -> .cpp (it is C++).
ARDUINO_PORT_SRC = "src/port/nff_port_esp32_arduino.c"
ARDUINO_PORT_DST = "nff_port_esp32_arduino.cpp"


def resolve_dest(arg_dest: str | None) -> Path:
    if arg_dest:
        return Path(arg_dest).expanduser().resolve()
    # Ask arduino-cli where the user (sketchbook) directory is.
    try:
        out = subprocess.run(
            ["arduino-cli", "config", "get", "directories.user"],
            capture_output=True, text=True, timeout=20,
        )
        user_dir = out.stdout.strip()
        if out.returncode == 0 and user_dir:
            return Path(user_dir) / "libraries" / "nff"
    except (OSError, subprocess.SubprocessError):
        pass
    # Fallback: the platform default Arduino sketchbook location.
    return Path.home() / "Documents" / "Arduino" / "libraries" / "nff"


def git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def copy(src: Path, dst: Path, log) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    log(f"  {src.relative_to(REPO_ROOT)}  ->  {dst.name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", help="Path to the Arduino library dir (…/libraries/nff)")
    ap.add_argument("--quiet", action="store_true", help="Only print the summary line")
    args = ap.parse_args()

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg)

    inc = REPO_ROOT / "include"
    src = REPO_ROOT / "src"
    port_src = REPO_ROOT / ARDUINO_PORT_SRC
    lib_props = REPO_ROOT / "library.properties"

    missing = [p for p in (inc / "nff.h", inc / "nff_port.h", port_src, lib_props) if not p.exists()]
    if missing:
        print(f"error: repo files missing: {', '.join(str(m) for m in missing)}", file=sys.stderr)
        return 2

    dest = resolve_dest(args.dest)
    dest_src = dest / "src"

    log(f"repo : {REPO_ROOT}")
    log(f"dest : {dest}")

    # Wipe src/ so renamed/removed files never linger as stale duplicates.
    if dest_src.exists():
        shutil.rmtree(dest_src)
    dest_src.mkdir(parents=True, exist_ok=True)

    # Header: duplicated to the lib root (for <nff.h>) and src/ (recursive layout).
    copy(inc / "nff.h", dest / "nff.h", log)
    copy(inc / "nff.h", dest_src / "nff.h", log)
    copy(inc / "nff_port.h", dest_src / "nff_port.h", log)

    # Platform-agnostic sources + internal headers (everything in src/ except port/).
    for f in sorted(src.glob("*.c")):
        copy(f, dest_src / f.name, log)
    for f in sorted(src.glob("*.h")):
        copy(f, dest_src / f.name, log)

    # The single Arduino ESP32 port, renamed .c -> .cpp (it is C++).
    copy(port_src, dest_src / ARDUINO_PORT_DST, log)

    # Library manifest.
    copy(lib_props, dest / "library.properties", log)

    # Sync marker for drift detection.
    head = git_head()
    (dest / ".nff_sync_meta").write_text(
        f"synced_from={REPO_ROOT}\ncommit={head}\nports=esp32_arduino_only\n",
        encoding="utf-8",
    )

    print(f"synced nff -> {dest}  (commit {head[:12]}, esp32-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
