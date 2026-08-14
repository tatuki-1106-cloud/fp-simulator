"""MCPサーバーのstdio起動用エントリポイント."""

import asyncio

from fp_simulator.db.database import init_db
from fp_simulator.mcp_server.server import mcp


async def main() -> None:
    await init_db()
    await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
