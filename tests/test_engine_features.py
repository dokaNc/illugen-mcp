"""Curves, gradients, parameter pins, optional inputs, subgraphs and splines against the real IlluGen.

    python tests/test_engine_features.py
"""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import Image  # noqa: E402
from PIL import Image as PImage  # noqa: E402
from PIL import ImageStat

from illugen_mcp import server as S  # noqa: E402

WORK = Path(__file__).resolve().parent / "work_features"


def data(content):
    return next(c for c in content if not isinstance(c, Image))


def mean_rgb(path):
    return ImageStat.Stat(PImage.open(path).convert("RGB")).mean


def render(p, out):
    res = data(S.project_render(p, str(out), preview=False))
    assert res["ok"], res
    return res


def base(name):
    p = str(WORK / f"{name}.illugen")
    S.project_new(p)
    noise = S.node_add(p, "Grayscale_Noise", {"scale": 25})["id"]
    return p, noise


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir()
    results = {}

    # 1. curve: inverting the noise's final remap must invert brightness
    p, noise = base("curve")
    exp = S.node_add(p, "Export_Grayscale", {"filename": "plain"})["id"]
    S.link_add(p, noise, "Output", exp, "Mask")
    plain = mean_rgb(render(p, WORK / "out_curve")["files"][0])[0]
    S.curve_set(p, noise, "Final remap", [[0, 1], [1, 0]], "linear")
    S.node_set(p, exp, {"filename": "inverted"})
    inv = mean_rgb(next(f for f in render(p, WORK / "out_curve")["files"] if "inverted" in f))[0]
    results["curve"] = f"mean {plain:.0f} -> {inv:.0f} (sum {plain + inv:.0f}, expect ~255)"
    assert abs(plain + inv - 255) < 20, results

    # 2. gradient: fire ramp must make the output red-dominant
    p, noise = base("gradient")
    ramp = S.node_add(p, "Color_Ramp")["id"]
    S.gradient_set(p, ramp, [[0, "#200000"], [0.5, "#ff4000"], [1, "#ffff80"]], "OKLab")
    exp = S.node_add(p, "Export_Color", {"filename": "fire"})["id"]
    S.link_add(p, noise, "Output", ramp, "Input")
    S.link_add(p, ramp, "Output", exp, "Color")
    r, g, b = mean_rgb(render(p, WORK / "out_gradient")["files"][0])
    results["gradient"] = f"mean rgb {r:.0f},{g:.0f},{b:.0f}"
    assert r > g > b, results

    # 3. signal -> parameter pin, and optional (dynamic) input
    p, noise = base("pins")
    osc = S.node_add(p, "Oscillator")["id"]
    S.link_add(p, osc, "Signal", noise, "Scale")
    lv = S.node_add(p, "Utils_Levels")["id"]
    noise2 = S.node_add(p, "Grayscale_Noise", {"seed": 3})["id"]
    S.link_add(p, noise, "Output", lv, "Input")
    S.link_add(p, noise2, "Output", lv, "In Low mod")
    exp = S.node_add(p, "Export_Grayscale")["id"]
    S.link_add(p, lv, "Output", exp, "Mask")
    v = data(S.project_validate(p, engine=True))
    results["pins"] = f"ok={v['ok']} dropped_links={v['dropped_links']} trial={v.get('trial_export')}"
    assert v["ok"], v

    # 4. subgraph built from scratch, used from the top level, with an exposed parameter
    p = str(WORK / "subgraph.illugen")
    S.project_new(p)
    sub = S.node_add(p, "Subgraph", label="my noise")["id"]
    inner = S.node_add(p, "Grayscale_Noise", {"scale": 12}, graph=sub)["id"]
    out = S.node_add(p, "Node_Output", label="Noise out", graph=sub)["id"]
    S.link_add(p, inner, "Output", out, "Input", graph=sub)
    S.subgraph_expose_param(p, sub, inner, "seed", "Seed")
    exp = S.node_add(p, "Export_Grayscale", {"filename": "sub"})["id"]
    S.link_add(p, sub, "Noise out", exp, "Mask")
    v = data(S.project_validate(p, engine=True))
    res = render(p, WORK / "out_sub")
    results["subgraph"] = f"validate ok={v['ok']} files={len(res['files'])}"
    assert v["ok"] and res["files"], (v, res)

    # 5. spline control points
    p = str(WORK / "spline.illugen")
    S.project_new(p)
    sp = S.node_add(p, "Grayscale_Spline")["id"]
    S.spline_set(p, sp, [[-30, -30, 0], [0, 30, 0], [30, -30, 0]], closed=True)
    exp = S.node_add(p, "Export_Grayscale", {"filename": "spline"})["id"]
    S.link_add(p, sp, "Output", exp, "Mask")
    v = data(S.project_validate(p, engine=True))
    m = mean_rgb(render(p, WORK / "out_spline")["files"][0])[0]
    results["spline"] = f"validate ok={v['ok']} mean={m:.1f}"
    assert v["ok"] and m > 0.5, results

    for k, val in results.items():
        print(f"{k:9s} {val}")
    print("PASS")
    return 0


@pytest.mark.engine
def test_features():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
