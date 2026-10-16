"""MCP server layer for nff-diagnosis.

A thin streamable-http MCP wrapper over the proven nff-diagnosis CLI/engine, built
on the official `mcp` SDK — the same framework as the sibling nff-ota (3002) and
nff-github (3003) servers. Tools call the existing in-process pipeline, store, and
storage directly (no shelling out). See nff_diagnosis/mcp/server.py.
"""
