"""Project editing without IlluGen: runs anywhere (used by CI)."""
import pytest

from illugen_mcp import codec
from illugen_mcp import server as S
from illugen_mcp.project import Project, ProjectError, node_types


def test_catalog_loaded():
    assert len(node_types()) >= 150
    assert "Node_Grayscale_Noise" in node_types()


def test_describe_node_type():
    d = S.describe_node_type("grayscale noise")
    assert d["type"] == "Node_Grayscale_Noise"
    assert d["pins"]["outputs"] == [["Output", "Grayscale"]]
    assert "Png" in S.describe_node_type("Export_Grayscale")["params"]["file_type"]["values"]


def test_build_graph(tmp_path):
    p = str(tmp_path / "graph.illugen")
    S.project_new(p)
    noise = S.node_add(p, "Grayscale_Noise", {"scale": 30, "seed": 7})["id"]
    ramp = S.node_add(p, "Color_Ramp")["id"]
    out = S.node_add(p, "Export_Color", {"filename": "fire"})["id"]
    S.link_add(p, noise, "Output", ramp, "Input")
    S.link_add(p, ramp, "Output", out, "Color")
    S.gradient_set(p, ramp, [[0, "#200000"], [1, "#ffff80"]])
    report = S.project_validate(p)[0]
    assert report["ok"], report
    summary = S.project_summary(p)
    assert summary["node_count"] == 3
    # the file stays a valid, lossless IlluGen project
    data = (tmp_path / "graph.illugen").read_bytes()
    assert codec.encode(codec.decode(data)) == data


def test_type_mismatch_refused(tmp_path):
    p = str(tmp_path / "bad.illugen")
    S.project_new(p)
    noise = S.node_add(p, "Grayscale_Noise")["id"]
    out = S.node_add(p, "Export_Color")["id"]
    with pytest.raises(ProjectError, match="type mismatch"):
        S.link_add(p, noise, "Output", out, "Color")


def test_unknown_enum_value_refused(tmp_path):
    p = str(tmp_path / "enum.illugen")
    S.project_new(p)
    with pytest.raises(ProjectError):
        S.node_add(p, "Export_Grayscale", {"file_type": "Gif"})


def test_relative_path_refused():
    with pytest.raises(ProjectError, match="absolute"):
        S.project_new("relative.illugen")


def test_blank_round_trip():
    prj = Project.blank()
    assert codec.decode(codec.encode(prj.data)) == prj.data
