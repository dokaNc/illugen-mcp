"""Where the server finds IlluGen and keeps its own files.

- Bundled node tables ship in ``illugen_mcp/data``. ``python -m illugen_mcp.build_catalog`` writes a fresh
  catalog for the installed IlluGen to the user data folder, which then takes precedence.
- Engine jobs use a scratch folder (private IlluGen runtimes, job lock) under the same home.

Environment overrides: ILLUGEN_EXE, ILLUGEN_MCP_HOME, ILLUGEN_MCP_STATE.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_EXE = Path(os.environ.get("ILLUGEN_EXE", r"C:\Program Files\JangaFX\IlluGen\IlluGen.exe"))
PKG_DATA = Path(__file__).resolve().parent / "data"
HOME = Path(os.environ.get("ILLUGEN_MCP_HOME") or Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "illugen-mcp")
USER_DATA = HOME / "data"
STATE_DIR = Path(os.environ.get("ILLUGEN_MCP_STATE") or HOME / "state")


def data_file(name: str) -> Path:
    """User-built table if present, else the bundled one."""
    user = USER_DATA / name
    return user if user.exists() else PKG_DATA / name
