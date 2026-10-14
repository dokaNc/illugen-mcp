"""Builds and renders the three README demo textures (lava, caustics, energy) with the MCP tools.

These are the same calls an agent makes over MCP, here called from Python directly:

    python examples/demo_scenes.py C:/temp/illugen-demo

Needs IlluGen installed (see README). Each scene takes ~15 s (validation + render).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import Image  # noqa: E402

from illugen_mcp import server as S  # noqa: E402

SCENES = {
    "lava": {
        "nodes": [
            ("noise", "Grayscale_Noise", {"noise_type_1": "Voronoi", "voronoi_result": "Difference", "scale": 6, "seed": 0.42}),
            ("levels", "Utils_Levels", {"input_low_level": 0.0, "input_high_level": 45.0}),
            ("ramp", "Color_Ramp", {}),
            ("glow", "Utils_Glow", {"radius": 10, "intensity": 25}),
            ("export", "Export_Color", {"filename": "lava"}),
        ],
        "links": [("noise", "Output", "levels", "Input"), ("levels", "Output", "ramp", "Input"),
                  ("ramp", "Output", "glow", "Input"), ("glow", "Output", "export", "Color")],
        "gradient": ("ramp", [[0, "#fff2a0"], [0.06, "#ff9a10"], [0.16, "#c42000"], [0.32, "#3a0600"], [1, "#080100"]]),
    },
    "caustics": {
        "nodes": [
            ("pattern", "Grayscale_Fractal", {"shape_type_UI": "Regular_Polygon", "scale": 100, "seed": 0.17}),
            ("caustics", "Grayscale_Caustics", {"intensity": 30, "prefiltering": 60, "refraction": 60}),
            ("ramp", "Color_Ramp", {}),
            ("export", "Export_Color", {"filename": "caustics"}),
        ],
        "links": [("pattern", "Output", "caustics", "Grayscale input"), ("caustics", "Output", "ramp", "Input"),
                  ("ramp", "Output", "export", "Color")],
        "gradient": ("ramp", [[0, "#021024"], [0.35, "#05467a"], [0.7, "#19b8d8"], [1, "#e8ffff"]]),
    },
    "energy": {
        "nodes": [
            ("noise", "Grayscale_Noise", {"noise_type_1": "Voronoi", "voronoi_result": "Edges", "scale": 7, "seed": 0.8}),
            ("ramp", "Color_Ramp", {}),
            ("glow", "Utils_Glow", {"radius": 14, "intensity": 45}),
            ("export", "Export_Color", {"filename": "energy"}),
        ],
        "links": [("noise", "Output", "ramp", "Input"), ("ramp", "Output", "glow", "Input"),
                  ("glow", "Output", "export", "Color")],
        "gradient": ("ramp", [[0, "#03010a"], [0.7, "#0e0326"], [0.9, "#6a22ff"], [1, "#b8ffff"]]),
    },
}


def data(content):
    return next(c for c in content if not isinstance(c, Image))


def build(out: Path, name: str, scene: dict) -> list[str]:
    path = str(out / f"{name}.illugen")
    S.project_new(path, overwrite=True)
    ids = {key: S.node_add(path, node_type, params)["id"] for key, node_type, params in scene["nodes"]}
    for a, a_pin, b, b_pin in scene["links"]:
        S.link_add(path, ids[a], a_pin, ids[b], b_pin)
    ramp, stops = scene["gradient"]
    S.gradient_set(path, ids[ramp], stops, "OKLab")
    report = data(S.project_validate(path, engine=True))
    assert report["ok"], report
    result = data(S.project_render(path, str(out / "renders"), preview=False))
    assert result["ok"], result
    return result["files"]


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "illugen-demo").resolve()
    out.mkdir(parents=True, exist_ok=True)
    for name, scene in SCENES.items():
        print(name, "->", ", ".join(build(out, name, scene)))
