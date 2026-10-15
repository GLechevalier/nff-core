#!/usr/bin/env python3
"""watch_heartbeat.py — the Stage-0 spike's authoritative PASS/FAIL detector.

Run this on the BROKER HOST while the spike firmware boots under QEMU. It subscribes to the
project-scoped heartbeat topic on nff-fleet's trusted non-TLS loopback listener and waits for the
spike device to come online — i.e. it confirms the firmware-in-QEMU completed an mTLS handshake +
CONNACK and published its retained heartbeat (the spike's pass condition).

Why the loopback listener (127.0.0.1:11883):
  nff-fleet's broker (nff_fleet/broker/broker.py) binds two listeners — the mTLS device listener on
  0.0.0.0:8883 and a NON-TLS `default` listener on 127.0.0.1:11883 that is trusted as the INTERNAL
  identity (ssl_object is None ⇒ full access), the same channel the internal router uses. So from
  the broker host we can subscribe to every project's heartbeats with no client cert.

Topic schema (the live one — the README/CLAUDE.md `nff/devices/...` form is stale):
    nff/{project_id}/devices/{device_id}/heartbeat   (retained, QoS 1, {"status":"online",...})

Exit 0 = SPIKE PASS, exit 1 = SPIKE FAIL (timeout), exit 2 = usage/connection error.

Requires: paho-mqtt  (pip install 'nff-mock-l2[spike]'  or  pip install paho-mqtt)
"""
from __future__ import annotations

import argparse
import json
import sys
import time

# paho is imported lazily inside main() so the pure helper below (heartbeat_matches) stays
# importable for unit tests even when the optional `spike` dependency isn't installed.


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Wait for the spike device's heartbeat on the broker.")
    p.add_argument("--device-id", required=True, help="The provisioned spike device id (cert CN).")
    p.add_argument("--host", default="127.0.0.1", help="Broker loopback host (default 127.0.0.1).")
    p.add_argument("--port", type=int, default=11883,
                   help="Broker non-TLS loopback port (default 11883).")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="Seconds to wait for the heartbeat before declaring FAIL (default 120).")
    return p.parse_args(argv)


def heartbeat_matches(topic: str, payload: bytes, device_id: str) -> bool:
    """True if ``topic``/``payload`` is an ONLINE heartbeat for ``device_id``.

    Pure + testable: topic must be nff/<project>/devices/<device_id>/heartbeat and the JSON
    status must not be 'offline' (the router treats a missing status as online, registry.py).
    """
    parts = topic.split("/")
    if len(parts) != 5 or parts[0] != "nff" or parts[2] != "devices" or parts[4] != "heartbeat":
        return False
    if parts[3] != device_id:
        return False
    try:
        data = json.loads(payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return data.get("status", "online") != "offline"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        import paho.mqtt.client as mqtt
    except ImportError:  # pragma: no cover - dependency missing on the toolchain box
        sys.stderr.write(
            "paho-mqtt is required: pip install paho-mqtt  (or pip install 'nff-mock-l2[spike]')\n"
        )
        return 2

    seen: dict[str, object] = {"hit": False, "project": None}

    def on_connect(client, userdata, flags, rc, *_):
        if rc != 0:
            sys.stderr.write(f"broker connect failed rc={rc}\n")
            return
        client.subscribe("nff/+/devices/+/heartbeat", qos=1)
        print(f"watching nff/+/devices/+/heartbeat on {args.host}:{args.port} "
              f"for device '{args.device_id}'...")

    def on_message(client, userdata, msg):
        if heartbeat_matches(msg.topic, msg.payload, args.device_id):
            seen["hit"] = True
            seen["project"] = msg.topic.split("/")[1]

    client = mqtt.Client(client_id="spike-watcher")
    # The non-TLS listener trusts the connection by INTERNAL identity; username "server" mirrors the
    # router's mqtt://server:@127.0.0.1:11883 url. No password / no TLS.
    client.username_pw_set("server", "")
    try:
        client.connect(args.host, args.port, keepalive=30)
    except OSError as exc:
        sys.stderr.write(
            f"cannot reach broker at {args.host}:{args.port}: {exc}\n"
            "Is nff-fleet up, and are you on the broker host? (the 11883 listener binds 127.0.0.1)\n"
        )
        return 2

    client.on_connect = on_connect
    client.on_message = on_message
    client.loop_start()

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if seen["hit"]:
            elapsed = args.timeout - (deadline - time.monotonic())
            client.loop_stop()
            print(f"SPIKE PASS: {args.device_id} online (project {seen['project']}) "
                  f"in {elapsed:.0f}s")
            return 0
        time.sleep(0.2)

    client.loop_stop()
    print(f"SPIKE FAIL: no online heartbeat for {args.device_id} in {args.timeout:.0f}s")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
