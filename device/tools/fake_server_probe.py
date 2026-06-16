"""Throwaway end-to-end probe: act as the fleet 'server', sign a command, deliver it
over the real broker to a mock device, and print the device's response.

Not part of the package — a manual verification aid. Run inside the nff network:

  docker run --rm --network nff-dev_nff -v nff-dev_fleet-data-dev:/data:ro \
      --entrypoint python nff-mock:dev /app/tools/fake_server_probe.py dev-01 ping
"""
from __future__ import annotations

import asyncio
import json
import secrets
import ssl
import sys
import time

import aiomqtt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


def build_tbs(p: dict) -> str:
    if p["action"] == "ota":
        return f"ota|{p['version']}|{p['sha256']}|{p['nonce']}|{p['timestamp']}"
    return f"{p['action']}|{p['nonce']}|{p['timestamp']}"


async def main(device_id: str, action: str) -> int:
    key = serialization.load_pem_private_key(
        open("/data/keys/cmd_sign.pem", "rb").read(), password=None
    )
    payload = {"action": action, "nonce": secrets.token_hex(4), "timestamp": int(time.time())}
    if action == "ota":
        payload |= {"version": "2.0.0", "url": "http://127.0.0.1:1/fw.bin", "sha256": "0" * 64}
    sig = key.sign(build_tbs(payload).encode(), ec.ECDSA(hashes.SHA256()))
    payload["cmd_sig"] = sig.hex()

    ctx = ssl.create_default_context(cafile="/data/certs/ca.crt")
    ctx.load_cert_chain(f"/data/certs/{device_id}/device.crt", f"/data/certs/{device_id}/device.key")
    ctx.check_hostname = False

    async with aiomqtt.Client(
        hostname="nff-fleet", port=8883, identifier="server", tls_context=ctx
    ) as client:
        await client.subscribe(f"nff/devices/{device_id}/response", qos=1)
        await client.publish(f"nff/devices/{device_id}/cmd", json.dumps(payload), qos=1)
        async with asyncio.timeout(15):
            async for message in client.messages:
                print("RESPONSE:", message.payload.decode())
                return 0
    return 1


if __name__ == "__main__":
    dev = sys.argv[1] if len(sys.argv) > 1 else "dev-01"
    act = sys.argv[2] if len(sys.argv) > 2 else "ping"
    raise SystemExit(asyncio.run(main(dev, act)))
