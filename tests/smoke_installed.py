"""Verify the installed wheel from a working directory outside the checkout."""

import asyncio
import shutil
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke():
    command = shutil.which("formkiq-mcp-server")
    if command is None:
        raise RuntimeError("Installed console entry point is missing")
    # The SDK inherits only basic OS variables; do not forward deployment credentials.
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, ".env").write_text("MCP_TRANSPORT=stdio\nFORMKIQ_API_GROUPS=core\n")
        params = StdioServerParameters(command=command, env={}, cwd=directory)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert len(tools) == 29
                schema_tool = next(tool for tool in tools if tool.name == "get_sites_schema")
                assert schema_tool.inputSchema["required"] == ["siteId"]
                resource = await session.read_resource("openapi://formkiq/jwt")
                assert "openapi: 3.0" in resource.contents[0].text
                result = await session.call_tool("describe_operation", {"operation_id": "DocumentSearch"})
                assert not result.isError
                operations = await session.call_tool("list_formkiq_operations", {})
                assert not operations.isError
                assert len(operations.structuredContent["result"]) == 25
                schema = await session.call_tool("describe_operation", {"operation_id": "GetSitesSchema"})
                assert not schema.isError
                assert schema.structuredContent["requiredPermission"] == "READ_SCHEMAS"
    print("Installed wheel: console entry point, .env startup, stdio, schemas, and bundled resource passed")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(smoke(), 30))
