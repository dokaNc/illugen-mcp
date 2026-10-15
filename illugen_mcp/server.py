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
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from pydantic import Field

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

# ---------------------------------------------------------------- shared argument types and tool hints

ProjectPath = Annotated[str, Field(description="Absolute path of the .illugen project file.")]
GraphScope = Annotated[str, Field(description=(
    "Graph to work in: empty for the top-level graph, or the id or label of a Node_Subgraph node "
    "('outer/inner' for nested subgraphs)."))]
NodeRef = Annotated[str, Field(description=(
    "Node id as a string (from project_summary or node_add), or the node's label if it is unique."))]

# offline tools only read or write the given project file; engine tools start a private IlluGen process
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
ADD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
REPLACE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)


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

@mcp.tool(title="IlluGen status", annotations=READ_ONLY)
def illugen_status() -> dict:
    """Reports whether IlluGen is installed and which node catalog this server uses.

    Call it first when an engine tool (project_render, project_validate with engine=true, project_screenshot,
    project_normalize) fails, or to find the folder of the shipped presets. Takes no arguments, does not start
    IlluGen and changes nothing.
    Returns: exe (path used, set with the ILLUGEN_EXE variable), installed (bool), running_instances (IlluGen
    processes currently open, GUI ones included), catalog_illugen_version and node_types (IlluGen version and
    node count of the bundled catalog), presets_dir."""
    cat = catalog()
    return {
        "exe": str(DEFAULT_EXE), "installed": DEFAULT_EXE.exists(),
        "running_instances": len(_illugen_pids()),
        "catalog_illugen_version": cat.get("illugen_version"), "node_types": cat.get("node_count"),
        "presets_dir": str(DEFAULT_EXE.parent / "presets_1_0_0"),
    }


@mcp.tool(title="List node types", annotations=READ_ONLY)
def list_node_types(
    category: Annotated[str, Field(description=(
        "Keep only this category (exact name, case-insensitive): Grayscale, Color, UV, Normal, Geometry, Utils, "
        "Islands, Export, Import, Particle, Camera, Time or Misc. Empty = all categories."))] = "",
    search: Annotated[str, Field(description=(
        "Keep only type names containing this text (case-insensitive), e.g. 'noise'. Empty = no filter."))] = "",
) -> dict:
    """Lists the IlluGen node types this server can create, grouped by category.

    Use it to find a type name for node_add, then describe_node_type for that type's parameters and pins.
    Offline (IlluGen not needed) and read-only.
    Returns {category: [type names]}, e.g. {"Grayscale": ["Node_Grayscale_Noise", ...]}; an empty object means
    nothing matched the filters."""
    groups: dict[str, list[str]] = {}
    for t, info in sorted(node_types().items()):
        if category and info["category"].lower() != category.lower():
            continue
        if search and search.lower() not in t.lower():
            continue
        groups.setdefault(info["category"], []).append(t)
    return groups


@mcp.tool(title="Describe node type", annotations=READ_ONLY)
def describe_node_type(
    node_type: Annotated[str, Field(description=(
        "Node type name from list_node_types. Case, spaces and the 'Node_' prefix are optional, "
        "e.g. 'grayscale noise' for Node_Grayscale_Noise."))],
) -> dict:
    """Describes one node type: its parameters, curves and typed input/output pins.

    Call it before node_add or node_set to get valid parameter names, value kinds, defaults, enum values and UI
    ranges, and before link_add to get pin names and the data kind each pin carries. Offline and read-only.
    Pin kinds: Grayscale, Color, UV, Normal, Islands, GeometryBuffer...; 'Inherit' takes the kind of whatever is
    connected; 'Signal' is a modulator (Oscillator, Cycle, Constant...).
    Returns: type, category, params {name: {kind, default, values for enums, min/max, label, modulatable}},
    curves, pins {inputs, outputs, optional_inputs} and parameter_pins {pin name: parameter it drives when a
    Signal is linked to it, e.g. 'Scale', 'Seed'}. An unknown type fails with close-match suggestions."""
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


@mcp.tool(title="List presets", annotations=READ_ONLY)
def list_presets(
    search: Annotated[str, Field(description=(
        "Keep only presets whose name contains this text (case-insensitive). Empty = all presets."))] = "",
) -> list[dict]:
    """Lists the example projects shipped with IlluGen, usable as starting points.

    Pass a returned name to project_new(from_preset=...) to get an editable copy; the presets themselves are
    never modified. Read-only; reads the presets folder of the installed IlluGen.
    Returns [{name, path}], sorted by name. The list is empty when nothing matches or when IlluGen is not found
    (check illugen_status)."""
    out = []
    for f in sorted((DEFAULT_EXE.parent / "presets_1_0_0").glob("*.illugen")):
        if search and search.lower() not in f.stem.lower():
            continue
        out.append({"name": f.stem, "path": str(f)})
    return out


# ---------------------------------------------------------------- project editing

@mcp.tool(title="New project", annotations=ADD)
def project_new(
    path: Annotated[str, Field(description=(
        "Absolute path of the project to create; must end in .illugen. Missing folders are created."))],
    from_preset: Annotated[str, Field(description=(
        "Optional source to copy: a preset name from list_presets, or an absolute path to any .illugen file. "
        "Empty = blank project."))] = "",
    overwrite: Annotated[bool, Field(description=(
        "Replace the file if it already exists (its previous content is lost). Default false: fail instead."))] = False,
) -> dict:
    """Creates a new .illugen project file, blank or copied from a preset or another project.

    Start every new graph here, then add nodes with node_add. Writes only the file at `path`; the copied preset
    or project is left untouched, and a copied preset is made editable.
    Returns the new file's path plus its summary (same shape as project_summary)."""
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


@mcp.tool(title="Project summary", annotations=READ_ONLY)
def project_summary(path: ProjectPath, graph: GraphScope = "") -> dict:
    """Gives an overview of a project: its nodes, links, export settings, annotations and main settings.

    Use it to get node ids before node_get, node_set or link_add, and to check the graph after edits; use
    node_get for every value of a single node. Offline and read-only.
    Returns: path, graph (scope shown), app_version, settings (frames_per_second, loop_region, default texture
    size...), annotations {groups, notes} in index order, node_count and nodes [{id, type, label, pos, links
    going out of the node, export settings for Node_Export_* nodes, subgraph_nodes for subgraphs}]."""
    return Project.load(_abs(path)).enter(graph).summary()


@mcp.tool(title="Get node", annotations=READ_ONLY)
def node_get(path: ProjectPath, node_id: NodeRef, graph: GraphScope = "") -> dict:
    """Returns every detail of one node: parameter values, curves, gradients, spline points, pins and links.

    Use it before node_set, curve_set, gradient_set, spline_set or node_set_data to see current values and exact
    names; use project_summary for the whole graph. Offline and read-only.
    Returns: id, type, label, pos, disabled, params {name: value} ('<driven by link>' when a Signal drives it),
    curves {name: [[position, value], ...]}, gradients, spline_points, subgraph pins for subgraphs,
    parameter_pins, outgoing and incoming links, known_inputs and known_outputs."""
    return Project.load(_abs(path)).enter(graph).node_detail(node_id)


@mcp.tool(title="Add node", annotations=ADD)
def node_add(
    path: ProjectPath,
    node_type: Annotated[str, Field(description=(
        "Node type from list_node_types, e.g. 'Node_Grayscale_Noise' (case, spaces and 'Node_' prefix optional)."))],
    params: Annotated[dict[str, Any] | None, Field(description=(
        "Parameter values to set, {name: value}, names and kinds from describe_node_type. Vectors and colors as "
        "lists ([x, y], [r, g, b, a]), enums as strings. Omitted parameters keep IlluGen's defaults."))] = None,
    label: Annotated[str, Field(description=(
        "Optional display label. A unique label can then replace the id in the other tools."))] = "",
    position: Annotated[list[float] | None, Field(description=(
        "[x, y] position in the node editor. Default: to the right of the right-most node."))] = None,
    graph: GraphScope = "",
) -> dict:
    """Adds a node to the graph with IlluGen's default values, optionally setting some parameters.

    Writes the project file in place. Parameter values are checked: an unknown name or invalid enum value fails
    with suggestions and nothing is saved; a value outside the UI range is kept and reported in warnings.
    Connect the new node with link_add afterwards.
    Returns: id (string id of the new node), type, and warnings if any."""
    prj = Project.load(_abs(path)).enter(graph)
    node = prj.add_node(node_type, params or {}, label, position)
    prj.save()
    return _ok(prj, id=str(node["id"]), type=node["type"])


@mcp.tool(title="Set node", annotations=EDIT)
def node_set(
    path: ProjectPath,
    node_id: NodeRef,
    params: Annotated[dict[str, Any] | None, Field(description=(
        "Parameter values to change, {name: value}, same format as node_add. Other parameters are left as is."))] = None,
    label: Annotated[str | None, Field(description="New display label; omit to keep it.")] = None,
    position: Annotated[list[float] | None, Field(description="New [x, y] position; omit to keep it.")] = None,
    disabled: Annotated[bool | None, Field(description=(
        "true bypasses the node in IlluGen (a disabled export node is skipped by project_render); false "
        "re-enables it; omit to keep it."))] = None,
    graph: GraphScope = "",
) -> dict:
    """Changes an existing node's parameter values, label, position or disabled flag.

    Only what you pass changes. Writes the project file in place; values are checked like in node_add. For
    curves, gradients and spline points use curve_set, gradient_set and spline_set.
    Returns: changed {name: new value} and warnings (value outside the UI range, or parameter currently driven
    by a link, in which case the value only applies once unlinked)."""
    prj = Project.load(_abs(path)).enter(graph)
    changed = prj.set_params(node_id, params) if params else {}
    prj.update_node(node_id, label=label, pos=position, disabled=disabled)
    prj.save()
    return _ok(prj, changed=changed)


@mcp.tool(title="Remove node", annotations=DELETE)
def node_remove(path: ProjectPath, node_id: NodeRef, graph: GraphScope = "") -> dict:
    """Deletes a node from the graph, with its outgoing links and every link from other nodes into it.

    Writes the project file in place and cannot be undone: copy the file first if you may need the node back.
    To bypass a node without losing it, use node_set(disabled=true) instead.
    Returns: removed_links, the number of links from other nodes that pointed to the deleted node."""
    prj = Project.load(_abs(path)).enter(graph)
    removed = prj.remove_node(node_id)
    prj.save()
    return {"removed_links": removed}


@mcp.tool(title="Link pins", annotations=EDIT)
def link_add(
    path: ProjectPath,
    from_node: Annotated[str, Field(description="Source node id or unique label.")],
    from_pin: Annotated[str, Field(description="Output pin name on the source node (see describe_node_type).")],
    to_node: Annotated[str, Field(description="Destination node id or unique label.")],
    to_pin: Annotated[str, Field(description=(
        "Input pin name on the destination node: a regular input, a parameter pin or a subgraph input."))],
    graph: GraphScope = "",
    modulation_range: Annotated[list | None, Field(description=(
        "Only for parameter pins: [min, max] in 0..1 of the parameter range the Signal sweeps, or one [min, max] "
        "pair per vector component. Default: the full range."))] = None,
) -> dict:
    """Connects an output pin of one node to an input pin of another.

    Any existing connection into that input is replaced. Pin data kinds are checked (Grayscale, Color, UV...) and
    a mismatch fails with an explanation. Parameter pins (e.g. 'Scale', 'Seed') let a Signal node (Oscillator,
    Cycle, Constant...) drive a parameter; optional inputs (e.g. Levels 'In Low mod') are exposed automatically.
    Subgraph pins are the labels of the subgraph's inner Node_Input/Node_Output nodes. Writes the project file in
    place.
    Returns: link {from_node, from_pin, to_node, to_pin} and warnings if any."""
    prj = Project.load(_abs(path)).enter(graph)
    link = prj.link(from_node, from_pin, to_node, to_pin, modulation_range)
    prj.save()
    return _ok(prj, link=link)


@mcp.tool(title="Unlink pin", annotations=DELETE)
def link_remove(
    path: ProjectPath,
    to_node: Annotated[str, Field(description="Node id or unique label whose input is disconnected.")],
    to_pin: Annotated[str, Field(description="Input pin name to disconnect.")],
    from_node: Annotated[str, Field(description=(
        "Optional: only remove links coming from this node (id or unique label). Empty = from any node."))] = "",
    graph: GraphScope = "",
) -> dict:
    """Disconnects an input pin: removes the link(s) arriving at `to_pin` of `to_node`.

    Writes the project file in place. If the pin drove a parameter, the parameter gets a fixed value again (its
    default if it had none). Removing nothing is not an error. node_get lists a node's incoming links.
    Returns: removed, the number of links deleted (0 if the pin was not connected)."""
    prj = Project.load(_abs(path)).enter(graph)
    n = prj.unlink(to_node, to_pin, from_node or None)
    prj.save()
    return {"removed": n}


@mcp.tool(title="Project settings", annotations=EDIT)
def project_set_settings(
    path: ProjectPath,
    frames_per_second: Annotated[float | None, Field(description="Animation frame rate, e.g. 30 or 60.")] = None,
    loop_region: Annotated[list[float] | None, Field(description=(
        "[start, end] of the animation loop, in seconds (e.g. [0, 2] = 2 s, 120 frames at 60 fps)."))] = None,
    default_texture_width: Annotated[str | None, Field(description=(
        "Default texture width, a power-of-two enum string from '_1' to '_16384', e.g. '_512', '_1024', '_2048'."))] = None,
    default_texture_height: Annotated[str | None, Field(description=(
        "Default texture height, same values as default_texture_width."))] = None,
) -> dict:
    """Changes project-wide settings: frame rate, animation loop region and default texture size.

    Only the values you pass change; omitted ones are kept. Applies to the whole project (top-level graph and
    subgraphs). Writes the project file in place. The resolution of an individual export is set on its export
    node with node_set.
    Returns the resulting settings, same shape as project_summary.settings."""
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

@mcp.tool(title="Set curve", annotations=REPLACE)
def curve_set(
    path: ProjectPath,
    node_id: NodeRef,
    curve: Annotated[str, Field(description=(
        "Curve name as shown by node_get or describe_node_type, e.g. Noise 'Final remap', Levels 'Main remap', "
        "Oscillator 'custom_waveform'."))],
    points: Annotated[list, Field(description=(
        "At least 2 keys: [[position, value], ...] (positions usually 0..1), or dicts "
        "{position, value, interpolation} to vary the interpolation per key."))],
    interpolation: Annotated[Literal["smooth", "linear", "constant", "bezier"], Field(description=(
        "Interpolation for keys that do not set their own."))] = "smooth",
    graph: GraphScope = "",
) -> dict:
    """Replaces all the keys of a node's curve (remap curves, custom oscillator waveforms...).

    The previous keys are discarded. Writes the project file in place.
    Returns: curve (name written) and keys (number of keys)."""
    prj = Project.load(_abs(path)).enter(graph)
    res = prj.set_curve(node_id, curve, points, interpolation)
    prj.save()
    return res


@mcp.tool(title="Set gradient", annotations=REPLACE)
def gradient_set(
    path: ProjectPath,
    node_id: NodeRef,
    stops: Annotated[list, Field(description=(
        "At least one stop: [[position 0..1, color], ...], color as [r, g, b, a] floats 0..1 or '#rrggbb[aa]'."))],
    color_space: Annotated[Literal["", "RGB", "OKLab", "sRGB", "Constant", "Beer_Lambert"], Field(description=(
        "Interpolation space between stops. Empty = keep the current one."))] = "",
    index: Annotated[int, Field(description="Which gradient of the node, for nodes with several. 0 = first.")] = 0,
    graph: GraphScope = "",
) -> dict:
    """Replaces a node's color gradient (Color Ramp, Color Interpolate, Geometry Combine Color...).

    The previous stops are discarded. Fails if the node has no gradient. Writes the project file in place.
    Returns: stops (number written) and color_space."""
    prj = Project.load(_abs(path)).enter(graph)
    res = prj.set_gradient(node_id, stops, color_space or None, index)
    prj.save()
    return res


@mcp.tool(title="Set spline points", annotations=REPLACE)
def spline_set(
    path: ProjectPath,
    node_id: NodeRef,
    points: Annotated[list, Field(description=(
        "Control points in order: [x, y, z] in scene units for Grayscale_Spline, Geometry_Sweep and "
        "Geometry_Points_Shape; [x, y] in 0..1 for Utils_Lattice."))],
    closed: Annotated[bool, Field(description="Connect the last point back to the first (closed shape).")] = False,
    edges: Annotated[list | None, Field(description=(
        "Optional explicit segments [[i, j], ...] between point indexes, for branching shapes. Overrides the "
        "default chaining."))] = None,
    graph: GraphScope = "",
) -> dict:
    """Replaces the control points of a spline, sweep, point-shape or lattice node.

    Points are chained in order unless `edges` is given. The previous points are discarded. Fails if the node has
    no spline or lattice points. Writes the project file in place.
    Returns: points (number written) and edges (number of segments, when applicable)."""
    prj = Project.load(_abs(path)).enter(graph)
    res = prj.set_spline(node_id, points, closed, edges)
    prj.save()
    return res


@mcp.tool(title="Set raw node data", annotations=REPLACE)
def node_set_data(
    path: ProjectPath,
    node_id: NodeRef,
    key: Annotated[str, Field(description=(
        "Name of an existing field of the node's data block. An unknown name fails and lists the available fields."))],
    value: Annotated[Any, Field(description=(
        "New value as tagged JSON, in the same form node_get uses: vectors as {\"__vec__\": \"f3\", \"v\": [x, y, z]}."))],
    graph: GraphScope = "",
) -> dict:
    """Low-level escape hatch: overwrites one raw field of a node's internal data block.

    Use it only for fields that node_set, curve_set, gradient_set and spline_set do not cover. The value is
    written without type checking, so run project_validate(engine=true) afterwards. Writes the project file in
    place.
    Returns {"ok": true}."""
    prj = Project.load(_abs(path)).enter(graph)
    prj.set_data(node_id, key, value)
    prj.save()
    return {"ok": True}


@mcp.tool(title="Expose subgraph parameter", annotations=EDIT)
def subgraph_expose_param(
    path: ProjectPath,
    subgraph: Annotated[str, Field(description="Id or unique label of the Node_Subgraph node.")],
    inner_node: Annotated[str, Field(description="Id or unique label of the node inside the subgraph.")],
    param: Annotated[str, Field(description="Parameter name on the inner node (see describe_node_type).")],
    label: Annotated[str, Field(description="Name shown on the subgraph node. Empty = the parameter name.")] = "",
    graph: GraphScope = "",
) -> dict:
    """Shows a parameter of a node inside a subgraph on the subgraph node itself, so it can be tweaked from outside.

    To build a subgraph: node_add('Subgraph'), then node_add and link_add with graph=<subgraph id> to add a
    Node_Input (its label becomes an input pin name), the inner nodes and a Node_Output (its label becomes an
    output pin name). Writes the project file in place.
    Returns: id of the exposed parameter, node_id, param_name and label."""
    prj = Project.load(_abs(path)).enter(graph)
    res = prj.expose_param(subgraph, inner_node, param, label)
    prj.save()
    return res


@mcp.tool(title="Add annotation", annotations=ADD)
def annotation_add(
    path: ProjectPath,
    kind: Annotated[Literal["group", "note"], Field(description=(
        "'group' = framed comment box drawn behind nodes; 'note' = free text label."))],
    text: Annotated[str, Field(description="Comment of the group, or text of the note.")],
    around_nodes: Annotated[list | None, Field(description=(
        "Groups: ids or labels of the nodes to frame; position and size are computed from them."))] = None,
    position: Annotated[list | None, Field(description=(
        "[x, y] top-left corner. Required unless around_nodes is given."))] = None,
    size: Annotated[list | None, Field(description=(
        "[width, height]. Default 600 x 400 for a group, 460 x 60 for a note."))] = None,
    color_idx: Annotated[int, Field(description="Groups: color index of the frame. 0 = default color.")] = 0,
    graph: GraphScope = "",
) -> dict:
    """Adds a comment to the node graph: a framed group around nodes, or a free text note.

    Annotations only document the graph; they do not change the rendered result. Writes the project file in place.
    Returns {"group": index} or {"note": index}, the index to pass to annotation_remove."""
    prj = Project.load(_abs(path)).enter(graph)
    res = prj.add_annotation(kind, text, position, size, around_nodes, color_idx)
    prj.save()
    return res


@mcp.tool(title="Remove annotation", annotations=DELETE)
def annotation_remove(
    path: ProjectPath,
    kind: Annotated[Literal["group", "note"], Field(description="Kind of annotation to remove.")],
    index: Annotated[int, Field(description=(
        "Index in project_summary.annotations.groups or .notes (0-based)."))],
    graph: GraphScope = "",
) -> dict:
    """Deletes one group or note annotation from the node graph.

    The indexes of later annotations of the same kind shift down by one: re-read project_summary before removing
    another. Fails if the index does not exist. Writes the project file in place.
    Returns {"ok": true}."""
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


@mcp.tool(title="Render project", annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def project_render(
    path: ProjectPath,
    output_dir: Annotated[str, Field(description=(
        "Absolute folder to write every export to (created if needed). Empty = the folders set on the export "
        "nodes, usually next to the project."))] = "",
    timeout_s: Annotated[int, Field(description="Maximum time to wait for the export, in seconds.")] = 300,
    preview: Annotated[bool, Field(description="Attach a downscaled preview of the first exported image.")] = True,
) -> list:
    """Renders the project with the real IlluGen: runs the export of every enabled Node_Export_* node.

    IlluGen runs as a private, invisible instance (an IlluGen open on screen is not affected). The project file
    itself is not modified; image files are written to output_dir or to the export nodes' folders, overwriting
    files with the same names. Needs IlluGen installed and at least one enabled export node. Run project_validate
    first when the graph is new.
    Returns: a result {ok, items_written, seconds, files (written paths), exports (settings of each export node),
    log_issues} followed by a preview image. On failure: {ok: false, error, log_issues, hint} plus screenshots of
    IlluGen's error dialogs."""
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


@mcp.tool(title="Screenshot project", annotations=READ_ONLY)
def project_screenshot(
    path: ProjectPath,
    width: Annotated[int, Field(description="Width of the invisible IlluGen window, in pixels.")] = 1600,
    height: Annotated[int, Field(description="Height of the invisible IlluGen window, in pixels.")] = 900,
) -> list:
    """Opens the project in a private, invisible IlluGen and returns a screenshot of its window.

    Shows the viewport and the node graph with each node's live preview: use it to check visually what a graph
    does, or to see why a render fails. Use project_render to produce the actual image files. Works on a temporary
    copy, so the project file is not modified. Needs IlluGen installed; takes a few seconds.
    Returns {path} and a PNG image."""
    src = _abs(path)
    tmp, _ = _render_copy(src, None)
    try:
        with IlluGenSession(tmp, width=width, height=height) as s:
            time.sleep(2.0)
            png = s.screenshot_png()
    finally:
        tmp.unlink(missing_ok=True)
    return [{"path": str(src)}, Image(data=png, format="png")]


@mcp.tool(title="Validate project", annotations=READ_ONLY)
def project_validate(
    path: ProjectPath,
    engine: Annotated[bool, Field(description=(
        "Also check the project in the real IlluGen (slower, needs IlluGen installed). Default false: offline "
        "checks only."))] = False,
) -> list:
    """Checks a project for problems before rendering it.

    Offline checks: unknown node types, unknown parameters, dangling links, missing export node. `legacy_params`
    (informational) lists parameters of older project versions, such as the shipped presets, that IlluGen renames
    or ignores on load. With engine=true, it also loads and re-saves a temporary copy in an invisible IlluGen,
    runs a trial export, and reports what the real loader dropped (nodes, links), serialization errors from its log
    and a screenshot of any load-issue dialog. The project file is never modified.
    Returns: a report {ok, offline_issues, legacy_params, and with engine: dropped_nodes, dropped_links,
    trial_export, engine_log_issues, load_dialog} followed by any screenshots."""
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


@mcp.tool(title="Normalize project", annotations=EDIT)
def project_normalize(path: ProjectPath) -> dict:
    """Loads the project in a private, invisible IlluGen and saves it back in place, the way IlluGen writes it.

    Fills in every default value, applies IlluGen's own fixes and refreshes the embedded thumbnail. Use it before
    sharing a project built with this server, or to update a project made with an older IlluGen. Overwrites the
    project file; a copy of the original is kept as <name>.illugen.bak (replacing any previous one). Needs IlluGen
    installed.
    Returns: path, backup (path of the .bak copy), log_issues from IlluGen and node_count."""
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


@mcp.tool(title="Project thumbnail", annotations=READ_ONLY)
def project_thumbnail(path: ProjectPath) -> list:
    """Returns the preview image IlluGen embedded in the project file at its last save.

    Instant and offline (IlluGen not needed), but it can be outdated or missing for a project edited only through
    this server: project_normalize refreshes it, project_screenshot and project_render show the current state.
    Read-only.
    Returns a JPEG image, or {thumbnail: null, hint} when the file has none."""
    data = Project.load(_abs(path)).thumbnail()
    if not data:
        return [{"thumbnail": None, "hint": "no embedded thumbnail; use project_screenshot or project_render"}]
    return [Image(data=data, format="jpeg")]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
