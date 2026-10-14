"""Builds dist/illugen-mcp-<version>.mcpb, a one-click MCP Bundle (Claude Desktop and other MCPB hosts).

    python scripts/build_mcpb.py

The bundle is a `uv` server: the host installs the dependencies from pyproject.toml, so it does not depend
on a particular Python build. Requires Node.js (`npx @anthropic-ai/mcpb`) for validation and packing.
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from illugen_mcp import __version__  # noqa: E402
from illugen_mcp.server import mcp  # noqa: E402

MCPB = ["npx", "-y", "@anthropic-ai/mcpb@2.1.2"]


def tools() -> list[dict]:
    return [{"name": t.name, "description": (t.description or "").strip().split("\n")[0]}
            for t in mcp._tool_manager.list_tools()]


def manifest() -> dict:
    return {
        "manifest_version": "0.4",
        "name": "illugen-mcp",
        "display_name": "IlluGen",
        "version": __version__,
        "description": "Build, edit and render JangaFX IlluGen node graphs from your AI agent.",
        "long_description": (
            "Lets the agent create IlluGen projects, add and wire nodes (157 node types, typed pins, curves, "
            "gradients, subgraphs), validate them in the real IlluGen and render them to PNG/EXR/TGA/... "
            "Rendering runs a private, invisible IlluGen instance: your open IlluGen is never touched. "
            "Requires Windows and your own licensed IlluGen 1.2.x. Not affiliated with JangaFX."
        ),
        "author": {"name": "dokaNc", "url": "https://github.com/dokaNc"},
        "repository": {"type": "git", "url": "https://github.com/dokaNc/illugen-mcp"},
        "homepage": "https://github.com/dokaNc/illugen-mcp",
        "support": "https://github.com/dokaNc/illugen-mcp/issues",
        "license": "MIT",
        "icon": "icon.png",
        "keywords": ["illugen", "jangafx", "vfx", "textures", "procedural", "node graph", "gamedev"],
        "server": {
            "type": "uv",
            "entry_point": "server.py",
            "mcp_config": {
                "command": "uv",
                "args": ["run", "--directory", "${__dirname}", "server.py"],
                "env": {"ILLUGEN_EXE": "${user_config.illugen_exe}"},
            },
        },
        "user_config": {
            "illugen_exe": {
                "type": "file",
                "title": "IlluGen.exe",
                "description": "Path to IlluGen.exe (change it only if IlluGen is installed elsewhere).",
                "default": r"C:\Program Files\JangaFX\IlluGen\IlluGen.exe",
                "required": False,
            }
        },
        "tools": tools(),
        "compatibility": {"platforms": ["win32"], "runtimes": {"python": ">=3.10"}},
    }


def main() -> int:
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="illugen_mcpb_"))
    try:
        shutil.copytree(ROOT / "illugen_mcp", stage / "illugen_mcp",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(ROOT / f, stage / f)
        shutil.copy2(ROOT / "docs" / "assets" / "icon.png", stage / "icon.png")
        (stage / "server.py").write_text("from illugen_mcp.server import main\n\nmain()\n", encoding="utf-8")
        (stage / "manifest.json").write_text(json.dumps(manifest(), indent=2), encoding="utf-8")
        subprocess.run([*MCPB, "validate", str(stage / "manifest.json")], check=True, shell=sys.platform == "win32")
        out = dist / f"illugen-mcp-{__version__}.mcpb"
        out.unlink(missing_ok=True)
        subprocess.run([*MCPB, "pack", str(stage), str(out)], check=True, shell=sys.platform == "win32")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    print(f"{out}  sha256={hashlib.sha256(out.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
