"""Service banners, weak-credential sets, and planted-secret material per flavor.

Credentials are intentionally weak and chosen to exist in the pentester's
``resources/wordlists/common_passwords.txt`` so the brute-force / default-cred
tools find them with their bundled wordlists:

- ``root:root`` and ``admin:admin`` — both passwords are in the wordlist.
- web login ``admin:admin``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Flavor:
    """Identity + weak surfaces for one target app flavor."""

    name: str
    device_id: str
    fw_version: str
    http_server_banner: str
    telnet_banner: str
    telnet_shell_prompt: str
    # username -> password. The telnet/ssh handshakes accept any of these pairs.
    weak_logins: dict[str, str]
    # The blob's firmware "product" line, embedded in planted strings.
    firmware_product: str


# NOTE: the telnet shell prompt MUST contain "$" — telnet_brute's default
# success_prompt is "$" and it declares success when that byte is in the reply.
ESP32 = Flavor(
    name="esp32",
    device_id="nff-mock-esp32-01",
    fw_version="1.4.2",
    http_server_banner="nffCam/1.4.2 (ESP32; lwIP/2.1)",
    telnet_banner="nff-mock-esp32 login: ",
    telnet_shell_prompt="\r\n[esp32]$ ",
    weak_logins={"admin": "admin", "root": "root"},
    firmware_product="nff-mock-esp32 IoT Camera",
)

LINUX = Flavor(
    name="linux",
    device_id="nff-mock-linux-01",
    fw_version="2.1.0",
    http_server_banner="lighttpd/1.4.45",
    telnet_banner="\r\nnff-mock-linux login: ",
    telnet_shell_prompt="\r\nBusyBox v1.31.1 (nff-mock) built-in shell (ash)\r\n/ $ ",
    weak_logins={"root": "root", "admin": "admin"},
    firmware_product="nff-mock-linux Router",
)

FLAVORS: dict[str, Flavor] = {ESP32.name: ESP32, LINUX.name: LINUX}
