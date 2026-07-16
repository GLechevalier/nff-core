# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest (PyPI) | Yes |
| older releases | No — upgrade to latest |

## Scope

nff runs locally on your machine and communicates with hardware over USB. The attack surface is limited, but relevant concerns include:

- **Arbitrary code execution** via malicious sketch content passed to `nff flash`
- **Serial port injection** — crafted input sent via `serial_write` to a connected device
- **MCP server exposure** — see the security contract below
- **Dependency vulnerabilities** — in `esptool`, `pyserial`, or `arduino-cli`

## MCP server security contract

`nff mcp` starts a streamable HTTP MCP server. Its security model:

- **Binds `127.0.0.1` only.** By default the server listens on loopback (`http://127.0.0.1:3010/mcp`) and is **not network-reachable**. `/health` is always unauthenticated (liveness probe only).
- **The Bearer gate is OFF by default.** nff is a single-user, localhost-only bench tool, so out of the box `/mcp` is open — no token, no OAuth. The loopback bind is what protects it, but **any local process on the machine can call the tools** while the gate is off.
- **Requiring auth — `NFF_MCP_REQUIRE_AUTH`.** Set `NFF_MCP_REQUIRE_AUTH=1` (also accepts `true` / `yes` / `on`) in the environment the server is launched from to turn the gate ON. Every request to `/mcp` must then carry `Authorization: Bearer <token>`, validated against the opaque MCP token (`config.mcp.access_token`, with `config.diagnosis.access_token` accepted for back-compat) in `~/.nff/config.json`. A missing or wrong token returns HTTP 401.
- **When to enable it:** on any host that is **not** a single-user local bench — shared/multi-user machines, or whenever the server is reachable beyond loopback (e.g. you pass a non-loopback `--host`, or forward/tunnel the port).

> ⚠️ **Do not expose the MCP server beyond localhost without authentication.** If you bind a non-loopback `--host` or forward the port off the machine, you **must** set `NFF_MCP_REQUIRE_AUTH=1`. An open, network-reachable `/mcp` gives any client full control of the tools — which includes flashing firmware and writing to serial devices.

## Regulatory note (EU Cyber Resilience Act)

nff is a developer tool that talks to real hardware. If it is ever distributed as a market product in the EU, the **Cyber Resilience Act (CRA)** will apply, which requires a clear security contract (secure defaults, documented configuration, and a vulnerability-handling process). This policy — the localhost-only default, the opt-in Bearer gate, and the reporting process below — is the starting point for that contract.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Email: gauthier.lechevalier26@gmail.com  
Subject line: `[nff security] <short description>`

Include:
- nff version (`pip show nff`)
- OS and Python version
- Steps to reproduce or a proof-of-concept
- What an attacker could achieve

**Response timeline:**
- Acknowledgement within 48 hours
- Assessment and severity within 7 days
- Fix or mitigation within 30 days for confirmed issues

You will be credited in the release notes unless you prefer to remain anonymous.
