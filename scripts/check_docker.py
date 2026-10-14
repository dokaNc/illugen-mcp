"""Starts the Docker image over MCP stdio and checks that it answers introspection (used by CI).

    docker build -t illugen-mcp . && python scripts/check_docker.py
"""
import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(command="docker", args=["run", "-i", "--rm", "illugen-mcp"])
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        init = await s.initialize()
        tools = (await s.list_tools()).tools
        print(init.serverInfo.name, len(tools), "tools")
        assert len(tools) >= 25
        res = await s.call_tool("describe_node_type", {"node_type": "grayscale noise"})
        assert not res.isError, res.content[0].text


asyncio.run(main())
