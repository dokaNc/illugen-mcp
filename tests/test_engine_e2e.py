"""End-to-end test against the real IlluGen (invisible). Takes ~40 s.

    python tests/test_engine_e2e.py
"""
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import Image  # noqa: E402

from illugen_mcp import server as S  # noqa: E402
from illugen_mcp.codec import Obj  # noqa: E402
from illugen_mcp.project import Project, ProjectError  # noqa: E402

WORK = Path(__file__).resolve().parent / "work"


def split(content):
    data = [c for c in content if not isinstance(c, Image)]
    images = [c for c in content if isinstance(c, Image)]
    return data[0], images


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir()
    p = str(WORK / "e2e.illugen")
    S.project_new(p)
    noise = S.node_add(p, "Grayscale_Noise", {"scale": 30, "noise_type_1": "Perlin", "seed": 7}, label="noise")["id"]
    levels = S.node_add(p, "Node_Utils_Levels", {"input_low_level": 20.0, "input_high_level": 80.0})["id"]
    bad_export = S.node_add(p, "Node_Export_Color", {"filename": "noise_test"})["id"]
    S.link_add(p, noise, "Output", levels, "Input")
    # grayscale into a color export: refused offline thanks to the pin types...
    try:
        S.link_add(p, levels, "Output", bad_export, "Color")
        raise AssertionError("type mismatch not detected offline")
    except ProjectError as e:
        print("offline refusal:", str(e)[:90])
    # ...and forced in anyway to exercise the engine-side detection
    prj = Project.load(p)
    prj.node(levels)["links"].append(Obj(from_pin="Output", to_pin="Color", to_node=int(bad_export)))
    prj.save()

    t = time.monotonic()
    report, imgs = split(S.project_validate(p, engine=True))
    print(f"validate(bad) {time.monotonic() - t:.1f}s ok={report['ok']} trial={report.get('trial_export')} images={len(imgs)}")
    assert report["ok"] is False and imgs, report

    t = time.monotonic()
    res, imgs = split(S.project_render(p, str(WORK / "out")))
    print(f"render(bad) {time.monotonic() - t:.1f}s ok={res['ok']} images={len(imgs)}")
    assert res["ok"] is False

    S.link_remove(p, bad_export, "Color")
    ramp = S.node_add(p, "Node_Color_Ramp")["id"]
    S.link_add(p, levels, "Output", ramp, "Input")
    S.link_add(p, ramp, "Output", bad_export, "Color")
    gray = S.node_add(p, "Node_Export_Grayscale", {"filename": "noise_gray"})["id"]
    S.link_add(p, levels, "Output", gray, "Mask")
    report, _ = split(S.project_validate(p, engine=True))
    print(f"validate(good) ok={report['ok']}")
    assert report["ok"], report
    t = time.monotonic()
    res, imgs = split(S.project_render(p, str(WORK / "out")))
    print(f"render(good) {time.monotonic() - t:.1f}s ok={res['ok']} files={res.get('files')} images={len(imgs)}")
    assert res["ok"] and len(res["files"]) == 2 and res["items_written"] == 2 and imgs, res
    (WORK / "preview.png").write_bytes(imgs[0].data)
    print("PASS")
    return 0


@pytest.mark.engine
def test_e2e():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
