"""Verify the installed wheel from a working directory outside the checkout."""

import asyncio
import os
import shutil
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke():
    command = shutil.which("formkiq-mcp-server")
    if command is None:
        raise RuntimeError("Installed console entry point is missing")
    env = {name: "" for name in os.environ if name.startswith(("FORMKIQ_", "FASTMCP_"))}
    env["MCP_TRANSPORT"] = "stdio"
    with tempfile.TemporaryDirectory() as directory:
        params = StdioServerParameters(command=command, env=env, cwd=directory)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 7
                resource = await session.read_resource("openapi://formkiq/jwt")
                assert "openapi: 3.0" in resource.contents[0].text
                result = await session.call_tool("describe_operation", {"operation_id": "DocumentSearch"})
                assert not result.isError
    print("Installed wheel: console entry point, stdio, schemas, and bundled resource passed")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(smoke(), 30))
