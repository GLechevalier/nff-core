"""Run INSIDE nff-fleet: watch for a LIVE 'online' heartbeat from spike-01 (up to 80s)."""
import asyncio, json
from amqtt.client import MQTTClient


async def main():
    c = MQTTClient(config={"auto_reconnect": False})
    await c.connect("mqtt://server:@127.0.0.1:11883/")
    await c.subscribe([("nff/+/devices/+/heartbeat", 0)])  # filter spike-01 in code
    print("watching for spike-01 online (80s)...", flush=True)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 80
    result = "NO_ONLINE"
    while loop.time() < deadline:
        try:
            msg = await asyncio.wait_for(c.deliver_message(), timeout=5)
        except asyncio.TimeoutError:
            continue
        topic = msg.publish_packet.variable_header.topic_name
        data = msg.publish_packet.payload.data.decode(errors="replace")
        if "/devices/spike-01/" not in topic:   # ignore the live mocks
            continue
        try:
            status = json.loads(data).get("status")
        except Exception:
            status = "?"
        print(f"spike-01 heartbeat: status={status} :: {data}", flush=True)
        if status == "online":
            result = "PASS_ONLINE"
            break
    await c.disconnect()
    print("RESULT:", result, flush=True)


asyncio.run(main())
