import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from illugen_mcp.paths import DEFAULT_EXE  # noqa: E402

HAS_ILLUGEN = sys.platform == "win32" and DEFAULT_EXE.exists()


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason=f"IlluGen not found at {DEFAULT_EXE} (set ILLUGEN_EXE)")
    for item in items:
        if "engine" in item.keywords and not HAS_ILLUGEN:
            item.add_marker(skip)
