"""Talks to the server over real MCP stdio: lists tools and calls a few of them.

    python tests/test_stdio.py
"""
import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from illugen_mcp.paths import DEFAULT_EXE  # noqa: E402


async def run() -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "illugen_mcp"], cwd=str(ROOT))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = (await s.list_tools()).tools
        print(len(tools), "tools:", ", ".join(t.name for t in tools))
        assert len(tools) >= 25
        res = await s.call_tool("describe_node_type", {"node_type": "grayscale noise"})
        print("describe:", res.content[0].text[:160].replace("\n", " "))
        assert not res.isError
        preset = DEFAULT_EXE.parent / "presets_1_0_0" / "clock.illugen"
        if preset.exists():
            res = await s.call_tool("project_thumbnail", {"path": str(preset)})
            print("thumbnail content types:", [c.type for c in res.content])
        res = await s.call_tool("node_add", {"path": "relative.illugen", "node_type": "x"})
        print("error surfaced:", res.isError, res.content[0].text[:120])
        assert res.isError
    return 0


def test_stdio_smoke():
    assert asyncio.run(run()) == 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
