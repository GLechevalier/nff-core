from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from nff_diagnosis.mcp import heartbeat
from nff_diagnosis.mcp.config import settings
from nff_diagnosis.mcp.tools.diagnosis import register as register_diagnosis
from nff_diagnosis.mcp.tools.elf import register as register_elf
from nff_diagnosis.mcp.tools.view import register as register_view

mcp = FastMCP(
    name="nff-diagnosis",
    instructions=(
        "ESP32/Xtensa crash diagnosis server. Turn a raw panic dump into a specific, "
        "source-level explanation of why a device failed (crash class, root cause, and "
        "the function/file/line of the relevant frame). "
        "Typical flow: upload_elf at flash time, then diagnose_serial with the captured "
        "panic text and the firmware's build_id. build_id (SHA-256 of the ELF) is the "
        "join key for symbol resolution — without it, backtraces stay unresolved. "
        "ingest_crash persists the crash and its diagnosis; the view tools read them back. "
        "Every diagnosis carries a confidence score and, when ambiguous, candidate "
        "alternatives — prefer an honest 'unsure' over a confident guess."
    ),
    log_level=settings.log_level,
    host=settings.mcp_host,
    port=settings.mcp_port,
)

register_diagnosis(mcp)
register_elf(mcp)
register_view(mcp)


def main() -> None:
    # The heartbeat runs as a process-level daemon thread, not inside an MCP lifespan:
    # the streamable-http transport enters/exits its lifespan per request, so a
    # lifespan-scoped heartbeat would never tick while the server is idle. See
    # nff_diagnosis/mcp/heartbeat.py (same reasoning as nff-ota / nff-github).
    heartbeat.start()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
