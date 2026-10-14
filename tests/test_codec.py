"""Byte-exact round trip on every project and node preset shipped with IlluGen.

    python tests/test_codec.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from illugen_mcp import codec, jsonio  # noqa: E402
from illugen_mcp.runner import DEFAULT_EXE  # noqa: E402


def main() -> int:
    root = DEFAULT_EXE.parent
    files = sorted(root.glob("presets_1_0_0/*.illugen")) + sorted(root.glob("node_presets/*/*.node_preset"))
    bad = []
    for f in files:
        data = f.read_bytes()
        value = codec.decode(data)
        if codec.encode(value) != data:
            bad.append(f"{f.name}: codec round trip differs")
        if codec.encode(jsonio.load(jsonio.dump(value))) != data:
            bad.append(f"{f.name}: json round trip differs")
    print(f"{len(files) - len(bad)}/{len(files)} files round-trip byte-exact")
    for b in bad:
        print("  FAIL", b)
    return 1 if bad else 0


@pytest.mark.engine
def test_codec_round_trip():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
