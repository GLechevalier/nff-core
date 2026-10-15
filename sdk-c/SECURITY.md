# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.x     | Yes       |

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Email **gauthier.lechevalier26@gmail.com** with the subject line:
`[nff-sdk-c SECURITY] <short description>`

Include:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept (no need to weaponise — a clear description is enough)
- Affected version(s) and platform(s)
- Any suggested mitigation if you have one

You will receive an acknowledgement within **72 hours** and a status update within **7 days**.

## Disclosure Policy

We follow coordinated disclosure. Please allow up to **90 days** for a fix to be prepared and released before publishing your findings. We will credit reporters in the release notes unless you prefer to remain anonymous.

## Scope

This SDK runs on embedded devices with no network-facing attack surface of its own. High-impact areas include:

- **Signature verification bypass** (`nff_security.c`, `nff_port.h` — `nff_port_ecdsa_p256_verify`)
- **Nonce/replay protection** (`src/nff_cmd.c` — nonce ring)
- **OTA integrity check** (`src/nff_ota.c` — SHA-256 verification of firmware images)
- **Credential handling** — any path where DER-encoded keys could be leaked or corrupted

Out of scope: issues in your platform's TLS/MQTT stack, generated `credentials.h` content (a deployment concern, not an SDK bug), or theoretical attacks requiring physical device access with no software component.
