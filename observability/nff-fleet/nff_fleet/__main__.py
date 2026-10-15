from nff_fleet.server import mcp
from nff_fleet.config import settings

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=settings.mcp_port)
