"""Deliberately-vulnerable IoT targets for demonstrating nff-pentester.

Two apps share one pure-asyncio service library (``common``):

- ``nff-mock-esp32`` — a misconfigured ESP32 web-config device: weak-cred HTTP
  config portal + anonymous plain-MQTT broker (``device/cmd``) + a planted-secret
  firmware image. Lights vuln-tree Chains 2 (MQTT anon + control) and 5 (web).
- ``nff-mock-linux`` — a classic embedded-Linux target: telnet (and optionally SSH)
  with weak creds → busybox-style root shell, a firmware image with a backdoor
  account, and an optional CoAP actuator. Lights Chains 1, 4, 5 and (optionally) 7.

These run standalone on a laptop/Pi with no fleet infrastructure. They are *only*
for authorised demos behind the nff-pentester event-safety sandbox.
"""

__version__ = "0.1.0"
