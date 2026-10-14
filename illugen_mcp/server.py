"""IlluGen MCP server.

Edits .illugen projects directly (lossless binary codec + node catalog built from IlluGen itself) and drives
the real IlluGen invisibly for anything that needs its engine: rendering/exporting, normalizing (filling
defaults, validating against the real loader) and screenshots. No mouse, keyboard focus or visible window.
"""
from __future__ import annotations

import io
import os
import shutil
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

from .paths import DEFAULT_EXE
from .project import Project, ProjectError, catalog, node_types, param_info, pin_table, resolve_type
from .runner import ExportFailed, IlluGenSession, _illugen_pids

mcp = FastMCP("illugen", instructions=(
    "Create and edit JangaFX IlluGen projects (.illugen node graphs) and render them with the real IlluGen, "
    "fully automatically and invisibly. Typical flow: project_new -> node_add (several) -> link_add -> "
    "project_validate -> project_render (returns produced files + preview). Node ids are strings. Use "
    "describe_node_type to see parameters, defaults and known pins. Paths must be absolute. Engine tools run a private, invisible "
    "IlluGen instance, so a GUI IlluGen may stay open. If a tool fails, read its error and any attached screenshot: "
    "most failures are graph problems (pin types, missing export node) that node_get / describe_node_type explain."
))


def _abs(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ProjectError(f"use an absolute path, got {path!r}")
    return p


def _ok(project: Project, **extra) -> dict:
    out = dict(extra)
    if project.warnings:
        out["warnings"] = project.warnings
    return out


def _preview_image(png_path: Path, max_side: int = 768) -> Image:
    from PIL import Image as PILImage

    im = PILImage.open(png_path)
    im.thumbnail((max_side, max_side))
    if im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGBA")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return Image(data=buf.getvalue(), format="png")


# ---------------------------------------------------------------- discovery

@mcp.tool()
def illugen_status() -> dict:
    """Installation, running instances and catalog info."""
    cat = catalog()
    return {
        "exe": str(DEFAULT_EXE), "installed": DEFAULT_EXE.exists(),
        "running_instances": len(_illugen_pids()),
        "catalog_illugen_version": cat.get("illugen_version"), "node_types": cat.get("node_count"),
        "presets_dir": str(DEFAULT_EXE.parent / "presets_1_0_0"),
    }


@mcp.tool()
def list_node_types(category: str = "", search: str = "") -> dict:
    """Lists node types, grouped by category (Grayscale, Color, UV, Geometry, Utils, Islands, Export, Import...).
    Optional filters: category (exact, case-insensitive) and search (substring of the type name)."""
    groups: dict[str, list[str]] = {}
    for t, info in sorted(node_types().items()):
        if category and info["category"].lower() != category.lower():
            continue
        if search and search.lower() not in t.lower():
            continue
        groups.setdefault(info["category"], []).append(t)
    return groups


@mcp.tool()
def describe_node_type(node_type: str) -> dict:
    """Parameters (kind, default, known enum values, UI bounds), curves and typed pins of a node type.
    Pin kinds: Grayscale, Color, UV, Normal, Islands, GeometryBuffer...;
    Inherit = takes the kind of what is connected; Signal = modulator. Parameters can also be driven
    through exposed parameter pins (names like 'Scale', 'Seed' in inputs_seen)."""
    t = resolve_type(node_type)
    info = node_types()[t]
    out = {"type": t, **{k: v for k, v in info.items() if k not in ("template", "params")}}
    info_by_name = {p["name"]: p for p in (param_info().get("nodes", {}).get(t) or {}).get("params", [])}
    enums = param_info().get("enums", {})
    params = {}
    for name, cat in info["params"].items():
        r = info_by_name.get(name, {})
        entry = {"kind": cat["kind"], "default": cat["default"]}
        for k in ("label", "unit", "min", "max", "description"):
            if r.get(k) not in (None, ""):
                entry[k] = r[k]
        if r.get("modulatable"):
            entry["modulatable"] = True
        if cat["kind"] == "enum":
            legal = enums.get(r.get("enum") or "")
            entry["values"] = legal if legal else cat.get("values_seen", [])
        params[name] = entry
    out["params"] = params
    out["parameter_pins"] = Project.param_pins(t)
    pins = pin_table().get(t)
    if pins:
        out["label"] = pins.get("label")
        out["pins"] = {"inputs": pins.get("inputs", []), "outputs": pins.get("outputs", []),
                       "optional_inputs": pins.get("extra_inputs", [])}
    return out


@mcp.tool()
def list_presets(search: str = "") -> list[dict]:
    """IlluGen's shipped example projects (usable as starting points with project_new(from_preset=...))."""
    out = []
    for f in sorted((DEFAULT_EXE.parent / "presets_1_0_0").glob("*.illugen")):
        if search and search.lower() not in f.stem.lower():
            continue
        out.append({"name": f.stem, "path": str(f)})
    return out


# ---------------------------------------------------------------- project editing

@mcp.tool()
def project_new(path: str, from_preset: str = "", overwrite: bool = False) -> dict:
    """Creates a project at `path` (.illugen). Empty by default, or a writable copy of a shipped preset
    (name from list_presets, or a path to any .illugen file)."""
    dest = _abs(path)
    if dest.exists() and not overwrite:
        raise ProjectError(f"{dest} exists (pass overwrite=true to replace it)")
    if from_preset:
        src = Path(from_preset)
        if not src.is_absolute():
            src = DEFAULT_EXE.parent / "presets_1_0_0" / f"{from_preset.removesuffix('.illugen')}.illugen"
        prj = Project.load(src)
        prj.data["settings"]["read_only"] = False
    else:
        prj = Project.blank()
        import random

        prj.graph["id"] = random.randint(-(2**63), 2**63 - 1)
    prj.save(dest)
    return {"path": str(dest), **prj.summary()}


@mcp.tool()
def project_summary(path: str, graph: str = "") -> dict:
    """Nodes (id, type, label, position, outgoing links, export settings) and main project settings.
    `graph` = subgraph id/label (or "a/b" for nested) to work inside a Node_Subgraph; empty = top level."""
    return Project.load(_abs(path)).enter(graph).summary()


@mcp.tool()
def node_get(path: str, node_id: str, graph: str = "") -> dict:
    """All parameter values, curves, gradients, spline points, parameter pins and links of one node
    (id or unique label).
    `graph` = subgraph id/label (or "a/b" for nested) to work inside a Node_Subgraph; empty = top level."""
    return Project.load(_abs(path)).enter(graph).node_detail(node_id)


@mcp.tool()
def node_add(path: str, node_type: str, params: dict | None = None, label: str = "",
             position: list[float] | None = None, graph: str = "") -> dict:
    """Adds a node with IlluGen's defaults, then applies `params` (name -> value; vectors as lists).
    Returns the new node id. Position [x, y] defaults to the right of the existing nodes."""
    prj = Project.load(_abs(path)).enter(graph)
    node = prj.add_node(node_type, params or {}, label, position)
    prj.save()
    return _ok(prj, id=str(node["id"]), type=node["type"])


@mcp.tool()
def node_set(path: str, node_id: str, params: dict | None = None, label: str | None = None,
             position: list[float] | None = None, disabled: bool | None = None, graph: str = "") -> dict:
    """Changes parameter values (name -> value) and/or label, position, disabled flag of a node."""
    prj = Project.load(_abs(path)).enter(graph)
    changed = prj.set_params(node_id, params) if params else {}
    prj.update_node(node_id, label=label, pos=position, disabled=disabled)
    prj.save()
    return _ok(prj, changed=changed)


@mcp.tool()
def node_remove(path: str, node_id: str, graph: str = "") -> dict:
    """Deletes a node and every link pointing to it."""
    prj = Project.load(_abs(path)).enter(graph)
    removed = prj.remove_node(node_id)
    prj.save()
    return {"removed_links": removed}


@mcp.tool()
def link_add(path: str, from_node: str, from_pin: str, to_node: str, to_pin: str, graph: str = "",
             modulation_range: list | None = None) -> dict:
    """Connects output pin `from_pin` of `from_node` to input pin `to_pin` of `to_node`
    (replaces any existing connection into that input). Pin data types are checked (Grayscale/Color/UV...).
    Parameter pins (e.g. 'Scale', 'Seed') let a Signal (Oscillator, Cycle, Constant...) drive a parameter;
    optional inputs (e.g. Levels 'In Low mod') are exposed automatically. Subgraph pins = labels of its
    inner Node_Input/Node_Output nodes. modulation_range ([min, max] in 0..1, or one pair per vector
    component) sets how a signal maps onto a parameter pin's range.
    `graph` = subgraph id/label (or "a/b" for nested) to work inside a Node_Subgraph; empty = top level."""
    prj = Project.load(_abs(path)).enter(graph)
    link = prj.link(from_node, from_pin, to_node, to_pin, modulation_range)
    prj.save()
    return _ok(prj, link=link)


@mcp.tool()
def link_remove(path: str, to_node: str, to_pin: str, from_node: str = "", graph: str = "") -> dict:
    """Removes the connection(s) into input `to_pin` of `to_node` (optionally only from `from_node`)."""
    prj = Project.load(_abs(path)).enter(graph)
    n = prj.unlink(to_node, to_pin, from_node or None)
    prj.save()
    return {"removed": n}


@mcp.tool()
def project_set_settings(path: str, frames_per_second: float | None = None, loop_region: list[float] | None = None,
                         default_texture_width: str | None = None, default_texture_height: str | None = None) -> dict:
    """Project settings. Texture sizes are enum strings such as "_512", "_1024", "_2048"."""
    prj = Project.load(_abs(path))
    s = prj.data["settings"]
    if frames_per_second is not None:
        s["frames_per_second"] = float(frames_per_second)
    if loop_region is not None:
        s["loop_region"] = [float(x) for x in loop_region]
    if default_texture_width is not None:
        s["default_texture_width"] = default_texture_width
    if default_texture_height is not None:
        s["default_texture_height"] = default_texture_height
    prj.save()
    return prj.summary()["settings"]


# ---------------------------------------------------------------- curves, gradients, splines, subgraphs

@mcp.tool()
def curve_set(path: str, node_id: str, curve: str, points: list, interpolation: str = "smooth",
              graph: str = "") -> dict:
    """Replaces a node curve (names from node_get/describe_node_type, e.g. Noise 'Final remap', Levels
    'Main remap', Oscillator 'custom_waveform'). points = [[position, value], ...] (positions usually 0..1)
    or dicts {position, value, interpolation}. interpolation: smooth | linear | constant | bezier."""
    prj = Project.load(_abs(path)).enter(graph)
    res = prj.set_curve(node_id, curve, points, interpolation)
    prj.save()
    return res


@mcp.tool()
def gradient_set(path: str, node_id: str, stops: list, color_space: str = "", index: int = 0,
                 graph: str = "") -> dict:
    """Replaces a color gradient (Color Ramp, Color Interpolate, Geometry Combine Color).
    stops = [[position 0..1, color], ...] with color = [r,g,b,a] floats 0..1 or "#rrggbb[aa]".
    color_space: RGB | OKLab | sRGB | Constant | Beer_Lambert (empty = keep)."""
    prj = Project.load(_abs(path)).enter(graph)
    res = prj.set_gradient(node_id, stops, color_space or None, index)
    prj.save()
    return res


@mcp.tool()
def spline_set(path: str, node_id: str, points: list, closed: bool = False, edges: list | None = None,
               graph: str = "") -> dict:
    """Control points of Grayscale_Spline, Geometry_Sweep, Geometry_Points_Shape ([x, y, z] in scene units)
    and Utils_Lattice ([x, y] in 0..1). Points are chained in order (closed=true loops back), or pass
    explicit edges [[i, j], ...] for branching shapes."""
    prj = Project.load(_abs(path)).enter(graph)
    res = prj.set_spline(node_id, points, closed, edges)
    prj.save()
    return res


@mcp.tool()
def node_set_data(path: str, node_id: str, key: str, value, graph: str = "") -> dict:
    """Escape hatch for any other node.data field shown by node_get (tagged JSON: vectors as
    {"__vec__": "f3", "v": [x, y, z]}). Validate with project_validate(engine=true) afterwards."""
    prj = Project.load(_abs(path)).enter(graph)
    prj.set_data(node_id, key, value)
    prj.save()
    return {"ok": True}


@mcp.tool()
def subgraph_expose_param(path: str, subgraph: str, inner_node: str, param: str, label: str = "",
                          graph: str = "") -> dict:
    """Shows a parameter of a node inside `subgraph` on the subgraph node itself.
    To build a subgraph: node_add('Subgraph'), then node_add/link_add with graph=<subgraph id> to add
    Node_Input (label = input pin name), inner nodes and a Node_Output (label = output pin name)."""
    prj = Project.load(_abs(path)).enter(graph)
    res = prj.expose_param(subgraph, inner_node, param, label)
    prj.save()
    return res


@mcp.tool()
def annotation_add(path: str, kind: str, text: str, around_nodes: list | None = None, position: list | None = None,
                   size: list | None = None, color_idx: int = 0, graph: str = "") -> dict:
    """Adds a graph annotation. kind='group': framed comment box (around_nodes = node ids to frame
    automatically, or position+size); kind='note': free text note at position."""
    prj = Project.load(_abs(path)).enter(graph)
    res = prj.add_annotation(kind, text, position, size, around_nodes, color_idx)
    prj.save()
    return res


@mcp.tool()
def annotation_remove(path: str, kind: str, index: int, graph: str = "") -> dict:
    """Removes group/note number `index` (as listed in project_summary.annotations)."""
    prj = Project.load(_abs(path)).enter(graph)
    prj.remove_annotation(kind, index)
    prj.save()
    return {"ok": True}


# ---------------------------------------------------------------- engine-backed tools

def _render_copy(src: Path, output_dir: Path | None) -> tuple[Path, Project]:
    """Copy of the project next to the original (relative texture paths keep working) with export
    directories pointed at output_dir and $(project) pinned to the original name."""
    prj = Project.load(src)
    for n in prj.export_nodes():
        for p in n.get("parameters", []):
            if p["name"] == "directory" and output_dir is not None:
                p["value"] = output_dir.as_posix()
            elif p["name"] in ("filename", "directory") and isinstance(p.get("value"), str):
                # the copy has a temporary name: pin $(project) (not $(projectdir)) to the real one
                p["value"] = p["value"].replace("$(project)", src.stem)
    tmp = src.with_name(f"{src.stem}.mcp_render_{os.getpid()}.illugen")
    # shipped presets are read_only: IlluGen ignores Ctrl+S on them, so the trial save would time out
    prj.data["settings"]["read_only"] = False
    prj.save(tmp)
    return tmp, prj


@mcp.tool()
def project_render(path: str, output_dir: str = "", timeout_s: int = 300, preview: bool = True) -> list:
    """Renders the project with IlluGen (File > Export on every enabled Node_Export_* node) invisibly.
    With output_dir, all exports are redirected there (the project file is not modified).
    Returns produced files, IlluGen log issues and a preview of the first image."""
    src = _abs(path)
    out_dir = _abs(output_dir) if output_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    tmp, prj = _render_copy(src, out_dir)
    exports = prj.export_nodes()
    if not exports:
        tmp.unlink(missing_ok=True)
        raise ProjectError("no enabled Node_Export_* node in the project")
    watch = [out_dir] if out_dir else list({src.parent, src.parent / src.stem})
    before = {f: f.stat().st_mtime_ns for d in watch if d.exists() for f in d.rglob("*") if f.is_file()}
    t0 = time.monotonic()
    pictures: list[bytes] = []
    failure = None
    try:
        with IlluGenSession(tmp) as s:
            try:
                items = s.export_all(timeout=timeout_s)
            except ExportFailed as e:
                items, failure = 0, str(e)
            issues = s.log.issues()
            notes = s.notes
            pictures = [p for p in (s.load_issue_png, s.failure_png) if p]
    finally:
        tmp.unlink(missing_ok=True)
    if failure:
        return [{"ok": False, "error": failure, "log_issues": sorted(set(issues))[:30], "notes": notes,
                 "hint": "check pin types with describe_node_type / node_get; project_screenshot shows the graph"},
                *[Image(data=p, format="png") for p in pictures]]
    produced = sorted(
        str(f) for d in watch if d.exists() for f in d.rglob("*")
        if f.is_file() and before.get(f) != f.stat().st_mtime_ns and not f.name.endswith(".illugen")
    )
    result = {
        "ok": True, "items_written": items, "seconds": round(time.monotonic() - t0, 1), "files": produced,
        "exports": [{"id": str(n["id"]), "type": n["type"], **prj._export_info(n)} for n in exports],
    }
    if not out_dir:
        result["note"] = "files detected in the project folder; pass output_dir for exact collection"
    if issues:
        result["log_issues"] = sorted(set(issues))[:30]
    if notes:
        result["notes"] = notes
    content: list = [result]
    if preview:
        img = next((Path(f) for f in produced if f.lower().endswith((".png", ".jpg", ".jpeg", ".tga"))), None)
        if img:
            content.append(_preview_image(img))
    return content


@mcp.tool()
def project_screenshot(path: str, width: int = 1600, height: int = 900) -> list:
    """Opens the project in an invisible IlluGen and returns a screenshot of its window
    (viewport + node graph previews). The project file is not modified."""
    src = _abs(path)
    tmp, _ = _render_copy(src, None)
    try:
        with IlluGenSession(tmp, width=width, height=height) as s:
            time.sleep(2.0)
            png = s.screenshot_png()
    finally:
        tmp.unlink(missing_ok=True)
    return [{"path": str(src)}, Image(data=png, format="png")]


@mcp.tool()
def project_validate(path: str, engine: bool = False) -> list:
    """Checks the project. Offline checks: unknown node types, unknown parameters, dangling links,
    missing export node. `legacy_params` (informational) lists parameters of older project versions, such as
    the shipped presets, that IlluGen renames or ignores on load. With engine=true, also loads and re-saves a copy in IlluGen and reports what the
    real loader dropped (nodes, links), serialization errors from its log, and a screenshot of any
    load-issue dialog."""
    src = _abs(path)
    prj = Project.load(src)
    report: dict = {"offline_issues": prj.validate()}
    if prj.legacy_notes:  # parameters of older project versions, renamed/ignored by IlluGen on load: not a problem
        report["legacy_params"] = prj.legacy_notes
    pictures: list[bytes] = []
    if engine:
        import tempfile

        trial_dir = Path(tempfile.mkdtemp(prefix="illugen_validate_"))
        tmp, copy = _render_copy(src, trial_dir)
        try:
            with IlluGenSession(tmp) as s:
                s.save()
                if s.load_issue_png:
                    pictures.append(s.load_issue_png)
                    report["load_dialog"] = "IlluGen showed 'Issues when loading project' (screenshot attached)"
                # graph warnings (e.g. pin type mismatches) only surface when exporting: do a trial export
                if copy.export_nodes():
                    try:
                        s.export_all(timeout=120)
                        report["trial_export"] = "ok"
                    except ExportFailed as e:
                        report["trial_export"] = str(e)
                        if s.failure_png:
                            pictures.append(s.failure_png)
                report["engine_log_issues"] = sorted(set(s.log.issues()))[:30]
            after = Project.load(tmp)
        finally:
            tmp.unlink(missing_ok=True)
            shutil.rmtree(trial_dir, ignore_errors=True)
        ids_after = {n["id"] for n in after.nodes}
        report["dropped_nodes"] = [f"{n['id']} ({n['type']})" for n in prj.nodes if n["id"] not in ids_after]
        links_before = {(n["id"], ln["from_pin"], ln["to_node"], ln["to_pin"]) for n in prj.nodes for ln in n.get("links", [])}
        links_after = {(n["id"], ln["from_pin"], ln["to_node"], ln["to_pin"]) for n in after.nodes for ln in n.get("links", [])}
        report["dropped_links"] = [{"from_node": str(a), "from_pin": b, "to_node": str(c), "to_pin": d}
                                   for a, b, c, d in sorted(links_before - links_after, key=str)]
    report["ok"] = (not any(report.get(k) for k in ("offline_issues", "dropped_nodes", "dropped_links", "load_dialog"))
                    and report.get("trial_export", "ok") == "ok")
    return [report, *[Image(data=p, format="png") for p in pictures]]


@mcp.tool()
def project_normalize(path: str) -> dict:
    """Loads the project in IlluGen and saves it back in place (fills every default, applies IlluGen's own
    fixes, refreshes the embedded thumbnail). A .bak copy of the original is kept."""
    src = _abs(path)
    shutil.copyfile(src, src.with_suffix(".illugen.bak"))
    prj = Project.load(src)
    if prj.data.get("settings", {}).get("read_only"):  # plain copy of a shipped preset: IlluGen would not save it
        prj.data["settings"]["read_only"] = False
        prj.save(src)
    with IlluGenSession(src) as s:
        s.save()
        issues = sorted(set(s.log.issues()))[:30]
    return {"path": str(src), "backup": str(src.with_suffix(".illugen.bak")), "log_issues": issues,
            **{k: v for k, v in Project.load(src).summary().items() if k in ("node_count",)}}


@mcp.tool()
def project_thumbnail(path: str) -> list:
    """The thumbnail embedded in the project file (no IlluGen needed)."""
    data = Project.load(_abs(path)).thumbnail()
    if not data:
        return [{"thumbnail": None, "hint": "no embedded thumbnail; use project_screenshot or project_render"}]
    return [Image(data=data, format="jpeg")]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
