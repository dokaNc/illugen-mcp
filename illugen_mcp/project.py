"""Offline editing of IlluGen projects (graph nodes, parameters, links, export settings)."""
from __future__ import annotations

import copy
import difflib
import json
import random
from functools import cache, lru_cache
from pathlib import Path

from . import codec, jsonio
from .paths import data_file

EXPORT_PREFIX = "Node_Export_"


class ProjectError(ValueError):
    pass


MISSING_HINT = ("\nIf IlluGen really has it, the node tables may be outdated for your IlluGen version: run "
                "`python -m illugen_mcp.build_catalog`, or report it at https://github.com/dokaNc/illugen-mcp/issues")


@lru_cache(maxsize=1)
def catalog() -> dict:
    path = data_file("catalog.json")
    if not path.exists():
        raise ProjectError("catalog missing: run `python -m illugen_mcp.build_catalog` once")
    return json.loads(path.read_text(encoding="utf-8"))


def node_types() -> dict[str, dict]:
    return catalog()["nodes"]


@lru_cache(maxsize=1)
def pin_table() -> dict[str, dict]:
    """Pin data types per node type.
    Kinds: Grayscale, Color, UV, Normal, Islands, GeometryBuffer, ... plus Inherit (takes the kind of
    its connected input), Any (passthrough), Signal (modulators), Camera."""
    path = data_file("pin_types.json")
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("nodes", {})


FLEX_KINDS = {"Inherit", "Any"}


def pin_kind(node_type: str, pin: str, side: str) -> str | None:
    info = pin_table().get(node_type)
    if not info:
        return None
    pins = info.get(side, [])
    if side == "inputs":
        pins = pins + [[p, k] for p, k in info.get("extra_inputs_typed", [])]
    for name, kind in pins:
        if name == pin:
            return kind
    if side == "inputs" and pin in info.get("extra_inputs", []):
        return "Grayscale"
    return None


@lru_cache(maxsize=1)
def param_info() -> dict:
    """Parameter definitions (labels, enum types, ranges, modulatable)."""
    path = data_file("param_info.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # absent, partially written or not UTF-8: fall back to presets data
        return {}


@lru_cache(maxsize=1)
def legacy_params() -> dict[str, dict]:
    """{node type: {old parameter: {action: renamed|dropped|kept, to?}}}: parameters written by older IlluGen
    versions (the shipped presets) that the current IlluGen renames or ignores on load (data/legacy_params.json)."""
    try:
        return json.loads(data_file("legacy_params.json").read_text(encoding="utf-8")).get("nodes", {})
    except (OSError, ValueError):
        return {}


def legacy_note(node_type: str, name: str) -> str | None:
    e = legacy_params().get(node_type, {}).get(name)
    if not e:
        return None
    if e.get("action") == "renamed":
        return f"legacy parameter (older project version): IlluGen renames it to {e['to']!r} on load"
    if e.get("action") == "dropped":
        return "legacy parameter (older project version): removed from IlluGen, ignored on load"
    return "legacy parameter still read by IlluGen"


@cache
def _param_pins(node_type: str) -> dict[str, str]:
    params = node_types().get(node_type, {}).get("params", {})
    out: dict[str, str] = {}
    for p in (param_info().get("nodes", {}).get(node_type, {}) or {}).get("params", []):
        if p.get("label") and p.get("name") in params and p.get("modulatable", True):
            out[p["label"]] = p["name"]
    # fallback from shipped presets: pins seen on the node that are neither texture pins nor optional inputs
    static = {name for name, _ in pin_table().get(node_type, {}).get("inputs", [])}
    for label in node_types().get(node_type, {}).get("inputs_seen", []):
        if label in out or label in static:
            continue
        guess = label.lower().replace(" ", "_")
        if guess in params:
            out[label] = guess
    return out


def param_enum_values(node_type: str, name: str) -> list[str] | None:
    """Every legal value of an enum parameter, or None if unknown."""
    info = param_info()
    for p in (info.get("nodes", {}).get(node_type, {}) or {}).get("params", []):
        if p.get("name") == name and p.get("enum"):
            return info.get("enums", {}).get(p["enum"])
    shared = info.get("shared_params", {})
    if isinstance(shared, dict):
        for group in shared.values():
            for p in group if isinstance(group, list) else group.get("params", []) if isinstance(group, dict) else []:
                if isinstance(p, dict) and p.get("name") == name and p.get("enum"):
                    return info.get("enums", {}).get(p["enum"])
    return None


def converters(src_kind: str, dst_kind: str) -> list[str]:
    """Node types that take src_kind in and give dst_kind out (data-driven conversion hints)."""
    res = []
    for t, info in pin_table().items():
        ins = {k for _, k in info.get("inputs", [])}
        outs = {k for _, k in info.get("outputs", [])}
        if src_kind in ins and dst_kind in outs:
            res.append(t)
    return sorted(res)


def resolve_type(name: str) -> str:
    """Accepts 'Node_Grayscale_Noise', 'Grayscale_Noise' or 'grayscale noise'."""
    types = node_types()
    if name in types:
        return name
    norm = name.strip().replace(" ", "_").lower()
    for t in types:
        if t.lower() in (norm, "node_" + norm):
            return t
    hint = difflib.get_close_matches(name, list(types), n=5, cutoff=0.4)
    raise ProjectError(f"unknown node type {name!r}" + (f"; did you mean {hint}?" if hint else "") + MISSING_HINT)


def _new_id(taken: set[int]) -> int:
    while True:
        i = random.randint(-(2**63), 2**63 - 1)
        if i not in taken and i != 0:
            return i


CURVE_MODES = {"smooth": ("Auto_Clamped", "Bezier"), "linear": ("Free", "Linear"),
               "constant": ("Free", "Constant"), "bezier": ("Free", "Bezier")}
GRADIENT_SPACES = ["RGB", "OKLab", "sRGB", "Constant", "Beer_Lambert"]


def build_curve_keys(points: list, interpolation: str = "smooth") -> list:
    """[[position, value], ...] (or dicts with position/value and optional per-key interpolation) ->
    IlluGen keyframes with tangents computed the way IlluGen's Auto_Clamped handles look in saved files:
    delta_time = half the distance to the neighbour, slope from neighbours, flat at local extrema/ends."""
    pts = []
    for p in points:
        if isinstance(p, dict):
            pts.append((float(p["position"]), float(p["value"]), p.get("interpolation", interpolation)))
        else:
            pts.append((float(p[0]), float(p[1]), interpolation))
    if len(pts) < 2:
        raise ProjectError("a curve needs at least 2 points")
    pts.sort(key=lambda x: x[0])
    keys = []
    for i, (pos, val, interp) in enumerate(pts):
        if interp not in CURVE_MODES:
            raise ProjectError(f"interpolation must be one of {list(CURVE_MODES)}")
        handle, mode = CURVE_MODES[interp]
        prev_ = pts[i - 1] if i > 0 else None
        next_ = pts[i + 1] if i + 1 < len(pts) else None
        dl = (pos - prev_[0]) / 2 if prev_ else (next_[0] - pos) / 2
        dr = (next_[0] - pos) / 2 if next_ else (pos - prev_[0]) / 2
        if interp == "linear":
            sl = (val - prev_[1]) / (pos - prev_[0]) if prev_ and pos != prev_[0] else 0.0
            sr = (next_[1] - val) / (next_[0] - pos) if next_ and next_[0] != pos else 0.0
        else:
            if prev_ and next_ and (prev_[1] - val) * (next_[1] - val) < 0 and next_[0] != prev_[0]:
                sl = sr = (next_[1] - prev_[1]) / (next_[0] - prev_[0])
            else:
                sl = sr = 0.0
        keys.append(codec.Obj(
            value=val, position=pos, handle_type=handle,
            tangent_left=codec.Obj(mode=mode, delta_time=-dl, delta_value=-sl * dl),
            tangent_right=codec.Obj(mode=mode, delta_time=dr, delta_value=sr * dr)))
    return keys


def parse_color(c) -> codec.Vec:
    if isinstance(c, str):
        h = c.lstrip("#")
        if len(h) not in (6, 8):
            raise ProjectError(f"bad hex color {c!r}")
        vals = [int(h[i:i + 2], 16) / 255 for i in range(0, len(h), 2)]
        if len(vals) == 3:
            vals.append(1.0)
        return codec.Vec("f4", vals)
    vals = [float(x) for x in c]
    if len(vals) == 3:
        vals.append(1.0)
    if len(vals) != 4:
        raise ProjectError(f"color needs 3 or 4 components, got {c!r}")
    return codec.Vec("f4", vals)


class Project:
    def __init__(self, data: codec.Obj, path: Path | None = None):
        self.data = data
        self.path = path
        self.warnings: list[str] = []
        self.legacy_notes: list[str] = []  # filled by validate(): non-blocking
        self._scope: dict | None = None
        self.scope_path = ""

    def enter(self, graph_path: str | None) -> Project:
        """Scopes node/link operations to a subgraph: '' = top level, '<subgraph id or label>' or
        'a/b' for nested subgraphs."""
        g = self.data["graph"]
        self._scope, self.scope_path = None, ""
        if not graph_path:
            return self
        for part in str(graph_path).strip("/").split("/"):
            self._scope = g
            n = self.node(part)
            if n["type"] != "Node_Subgraph":
                raise ProjectError(f"node {part!r} is a {n['type']}, not a Node_Subgraph")
            g = n["data"]["graph"]
        self._scope, self.scope_path = g, str(graph_path).strip("/")
        return self

    # ---- io ----
    @classmethod
    def load(cls, path: str | Path) -> Project:
        p = Path(path)
        if not p.exists():
            raise ProjectError(f"project not found: {p}")
        try:
            return cls(codec.read_file(p), p)
        except codec.FormatError as e:
            raise ProjectError(f"{p} is not a valid IlluGen project: {e}") from e

    @classmethod
    def blank(cls) -> Project:
        return cls(codec.read_file(data_file("blank.illugen")))

    def save(self, path: str | Path | None = None) -> Path:
        p = Path(path) if path else self.path
        if p is None:
            raise ProjectError("no path to save to")
        if p.suffix.lower() != ".illugen":
            raise ProjectError("project files must end in .illugen")
        p.parent.mkdir(parents=True, exist_ok=True)
        codec.write_file(p, self.data)
        self.path = p
        return p

    # ---- graph access ----
    @property
    def graph(self) -> dict:
        return self._scope if self._scope is not None else self.data["graph"]

    @property
    def nodes(self) -> list[dict]:
        return self.graph["nodes"]

    def node(self, node_id: str | int) -> dict:
        try:
            nid = int(node_id)
        except (TypeError, ValueError):
            nid = None
        for n in self.nodes:
            if n.get("id") == nid:
                return n
        matches = [n for n in self.nodes if node_id and n.get("label") == node_id]
        if len(matches) == 1:
            return matches[0]
        raise ProjectError(f"no node with id or unique label {node_id!r}")

    def _ids(self) -> set[int]:
        return {n.get("id") for n in self.nodes}

    def incoming(self, node: dict) -> list[dict]:
        nid = node["id"]
        res = []
        for n in self.nodes:
            for ln in n.get("links", []):
                if ln.get("to_node") == nid:
                    res.append({"from_node": str(n["id"]), "from_type": n["type"], "from_pin": ln["from_pin"],
                                "to_pin": ln["to_pin"]})
        return res

    # ---- views ----
    def summary(self) -> dict:
        nodes = []
        for n in self.nodes:
            entry = {"id": str(n.get("id")), "type": n.get("type"), "pos": codec.to_json(n.get("pos"))}
            if n.get("label"):
                entry["label"] = n["label"]
            if n.get("disabled"):
                entry["disabled"] = True
            if n.get("on") is False:
                entry["on"] = False
            if n.get("links"):
                entry["links"] = [{"from_pin": ln["from_pin"], "to_node": str(ln["to_node"]), "to_pin": ln["to_pin"]}
                                  for ln in n["links"]]
            if n.get("type", "").startswith(EXPORT_PREFIX):
                entry["export"] = self._export_info(n)
            subs = sum(1 for _ in _subgraphs(n))
            if subs:
                entry["subgraph_nodes"] = sum(len(g["nodes"]) for g in _subgraphs(n))
            nodes.append(entry)
        s = self.data.get("settings", {})
        annotations = {
            "groups": [{"comment": g.get("comment"), "pos": codec.to_json(g.get("pos")), "size": codec.to_json(g.get("size"))}
                       for g in self.graph.get("groups", [])],
            "notes": [{"text": n.get("text"), "pos": codec.to_json(n.get("pos"))} for n in self.graph.get("notes", [])],
        }
        return {
            "path": str(self.path) if self.path else None,
            "graph": self.scope_path or "(top level)",
            "annotations": annotations,
            "app_version": self.data.get("app_version"),
            "settings": {k: codec.to_json(s.get(k)) for k in (
                "frames_per_second", "loop_region", "loop", "read_only", "default_texture_width",
                "default_texture_height", "default_texture_filtering", "default_texture_tiling") if k in s},
            "node_count": len(nodes),
            "nodes": nodes,
        }

    def _export_info(self, n: dict) -> dict:
        keep = ("directory", "filename", "file_type", "sequence", "first_frame", "num_frames", "stride_frames",
                "png_bits", "srgb", "alpha", "resolution", "resolution_type")
        return {p["name"]: codec.to_json(p.get("value")) for p in n.get("parameters", []) if p["name"] in keep}

    def node_detail(self, node_id) -> dict:
        n = self.node(node_id)
        info = node_types().get(n["type"], {})
        data = n.get("data") if isinstance(n.get("data"), dict) else {}
        curves = {name: [[k["position"], k["value"]] for k in keys] for name, keys in (data.get("curves") or {}).items()}
        if data.get("custom_waveform_keys"):
            curves["custom_waveform"] = [[k["position"], k["value"]] for k in data["custom_waveform_keys"]]
        extra = {}
        if data.get("gradients"):
            extra["gradients"] = [{"color_space": g.get("color_space"),
                                   "stops": [[k["position"], list(k["color"])] for k in g.get("keys", [])]}
                                  for g in data["gradients"]]
        if "keys" in data:
            extra["spline_points"] = [codec.to_json(k) for k in data["keys"]]
        if n["type"] == "Node_Subgraph":
            ins, outs = self.subgraph_pins(n)
            extra["subgraph"] = {"inputs": ins, "outputs": outs, "inner_nodes": len(data.get("graph", {}).get("nodes", [])),
                                 "exposed_parameters": codec.to_json(data.get("exposed_parameters", []))}
        if data.get("exposed_dynamic_inputs"):
            extra["exposed_optional_inputs"] = list(data["exposed_dynamic_inputs"])
        return {
            "id": str(n["id"]), "type": n["type"], "label": n.get("label", ""), "pos": codec.to_json(n.get("pos")),
            "disabled": n.get("disabled", False), "on": n.get("on", True),
            "params": {p["name"]: codec.to_json(p.get("value", "<driven by link>")) for p in n.get("parameters", [])},
            "curves": curves, **extra,
            "parameter_pins": self.param_pins(n["type"]),
            "outgoing": [{"from_pin": ln["from_pin"], "to_node": str(ln["to_node"]), "to_pin": ln["to_pin"]}
                         for ln in n.get("links", [])],
            "incoming": self.incoming(n),
            "known_inputs": info.get("inputs_seen", []),
            "known_outputs": info.get("outputs_seen", []),
        }

    # ---- edits ----
    def add_node(self, type_name: str, params: dict | None = None, label: str = "",
                 pos: list[float] | None = None) -> dict:
        t = resolve_type(type_name)
        node = jsonio.load(copy.deepcopy(node_types()[t]["template"]))
        node["id"] = _new_id(self._ids())
        node["links"] = []
        node["label"] = label or ""
        if pos is None:
            xs = [n["pos"][0] for n in self.nodes if isinstance(n.get("pos"), codec.Vec)]
            pos = [(max(xs) + 320.0) if xs else 0.0, 0.0]
        node["pos"] = codec.Vec("f2", pos)
        self.nodes.append(node)
        if params:
            self.set_params(node["id"], params)
        return node

    def set_params(self, node_id, params: dict) -> dict:
        n = self.node(node_id)
        by_name = {p["name"]: p for p in n.get("parameters", [])}
        info = node_types().get(n["type"], {}).get("params", {})
        changed = {}
        for name, value in params.items():
            p = by_name.get(name)
            if p is None and name in info:
                # node written by an older IlluGen (shipped presets): the parameter is absent from the file,
                # add it from the catalog template and remove the legacy parameter IlluGen would rename into it
                tmpl = jsonio.load(copy.deepcopy(node_types()[n["type"]]["template"]))
                p = next((x for x in tmpl.get("parameters", []) if x.get("name") == name), None)
                if p is not None:
                    olds = {o for o, e in legacy_params().get(n["type"], {}).items() if e.get("to") == name}
                    n["parameters"] = [x for x in n.get("parameters", []) if x.get("name") not in olds] + [p]
                    by_name = {x["name"]: x for x in n["parameters"]}
            note = None if name in info else legacy_note(n["type"], name)
            if note:
                raise ProjectError(f"{n['type']}.{name}: {note}")
            if p is None:
                hint = difflib.get_close_matches(name, list(by_name), n=4, cutoff=0.5)
                raise ProjectError(f"{n['type']} has no parameter {name!r}" + (f"; did you mean {hint}?" if hint else "") + MISSING_HINT)
            kind = codec.kind_of(p["value"]) if "value" in p else info.get(name, {}).get("kind")
            if kind is None:
                raise ProjectError(f"parameter {name!r} has no value kind to coerce to")
            try:
                new = codec.coerce(value, kind)
            except ValueError as e:
                raise ProjectError(f"{n['type']}.{name}: {e}") from e
            if kind == "enum":
                legal = param_enum_values(n["type"], name)
                if legal is not None:
                    if new not in legal:
                        hint = difflib.get_close_matches(new, legal, n=3, cutoff=0.3)
                        raise ProjectError(f"{n['type']}.{name}: {new!r} is not a valid value; valid: {legal}"
                                           + (f" (closest: {hint})" if hint else ""))
                else:
                    seen = info.get(name, {}).get("values_seen", [])
                    if seen and new not in seen and name not in ("filename", "directory"):
                        self.warnings.append(f"{n['type']}.{name}={new!r} not among known values {seen}; "
                                             "IlluGen will log 'missing enum' if it is invalid")
            if "bounds" in p and kind == "float":
                lo, hi = p["bounds"]
                if not lo <= new <= hi:
                    self.warnings.append(f"{n['type']}.{name}={new} outside UI bounds [{lo}, {hi}]")
            elif kind == "float":
                r = next((x for x in (param_info().get("nodes", {}).get(n["type"]) or {}).get("params", [])
                          if x.get("name") == name), {})
                lo, hi = r.get("min"), r.get("max")
                if lo is not None and hi is not None and not lo <= new <= hi:
                    self.warnings.append(f"{n['type']}.{name}={new} outside the UI slider range [{lo}, {hi}]")
            if "has_pin_override" in p:
                self.warnings.append(f"{n['type']}.{name} is driven by a link; the value applies once unlinked")
            p["value"] = new
            changed[name] = codec.to_json(new)
        return changed

    def update_node(self, node_id, label: str | None = None, pos: list[float] | None = None,
                    disabled: bool | None = None, on: bool | None = None) -> None:
        n = self.node(node_id)
        if label is not None:
            n["label"] = label
        if pos is not None:
            n["pos"] = codec.Vec("f2", pos)
        if disabled is not None:
            n["disabled"] = bool(disabled)
        if on is not None:
            n["on"] = bool(on)

    def remove_node(self, node_id) -> int:
        n = self.node(node_id)
        self.nodes.remove(n)
        removed = 0
        for other in self.nodes:
            before = len(other.get("links", []))
            other["links"] = [ln for ln in other.get("links", []) if ln.get("to_node") != n["id"]]
            removed += before - len(other["links"])
        return removed

    def output_kind(self, node: dict, pin: str, depth: int = 0) -> str | None:
        """Data kind flowing out of node.pin, following polymorphic (Inherit/Any) pins upstream."""
        k = pin_kind(node["type"], pin, "outputs")
        if k not in FLEX_KINDS or depth > 32:
            return k
        info = pin_table().get(node["type"], {})
        flex_inputs = [name for name, kind in info.get("inputs", []) if kind in FLEX_KINDS]
        for src in self.nodes:
            for ln in src.get("links", []):
                if ln.get("to_node") == node["id"] and ln.get("to_pin") in flex_inputs:
                    resolved = self.output_kind(src, ln["from_pin"], depth + 1)
                    if resolved and resolved not in FLEX_KINDS:
                        return resolved
        return k

    def link_problem(self, src: dict, from_pin: str, dst: dict, to_pin: str) -> str | None:
        """Definite type mismatch between two pins, or None when compatible/unknown."""
        out_k = self.output_kind(src, from_pin)
        in_k = pin_kind(dst["type"], to_pin, "inputs")
        if not out_k or not in_k or out_k in FLEX_KINDS or in_k in FLEX_KINDS or out_k == in_k:
            return None
        if "Signal" in (out_k, in_k) or "Camera" in (out_k, in_k):
            return None  # modulation/camera pins are matched by IlluGen at a different level
        conv = converters(out_k, in_k)
        return (f"type mismatch: {src['type']}.{from_pin} gives {out_k} but {dst['type']}.{to_pin} expects {in_k}"
                + (f"; insert a converter such as {conv[:6]}" if conv else ""))

    def link(self, from_id, from_pin: str, to_id, to_pin: str, modulation_range: list | None = None) -> dict:
        src, dst = self.node(from_id), self.node(to_id)
        if src is dst:
            raise ProjectError("cannot link a node to itself")
        for node, pin, side, typed in ((src, from_pin, "outputs_seen", "outputs"), (dst, to_pin, "inputs_seen", "inputs")):
            if node["type"] == "Node_Subgraph":
                ins, outs = self.subgraph_pins(node)
                known = sorted(outs if typed == "outputs" else ins)
                if pin not in known:
                    raise ProjectError(f"subgraph {node['id']} has no {typed[:-1]} {pin!r} (pins come from its inner "
                                       f"Node_Input/Node_Output labels: {known})")
                continue
            known = sorted(set(node_types().get(node["type"], {}).get(side, []))
                           | {p for p, _ in pin_table().get(node["type"], {}).get(typed, [])}
                           | (set(pin_table().get(node["type"], {}).get("extra_inputs", [])) if typed == "inputs" else set())
                           | (set(self.param_pins(node["type"])) if typed == "inputs" else set()))
            if known and pin not in known:
                hint = difflib.get_close_matches(pin, known, n=3, cutoff=0.5)
                if typed == "outputs":
                    raise ProjectError(f"{node['type']} has no output {pin!r} (outputs: {known})"
                                       + (f"; did you mean {hint}?" if hint else ""))
                self.warnings.append(f"input {pin!r} unknown on {node['type']} (known: {known}); it may be an "
                                     "exposed parameter pin" + (f"; did you mean {hint}?" if hint else ""))
        problem = self.link_problem(src, from_pin, dst, to_pin)
        if problem:
            raise ProjectError(problem)
        # an input pin takes a single connection: drop any existing link into it
        for n in self.nodes:
            n["links"] = [ln for ln in n.get("links", []) if not (ln.get("to_node") == dst["id"] and ln.get("to_pin") == to_pin)]
        ln = codec.Obj(from_pin=from_pin, to_pin=to_pin, to_node=dst["id"])
        src.setdefault("links", []).append(ln)
        # optional inputs (e.g. Levels 'In Low mod') only exist once listed in data.exposed_dynamic_inputs
        if to_pin in pin_table().get(dst["type"], {}).get("extra_inputs", []):
            exposed = dst.setdefault("data", codec.Obj()).setdefault("exposed_dynamic_inputs", [])
            if to_pin not in exposed:
                exposed.append(to_pin)
        # parameter pin: IlluGen only keeps the link if the parameter is flagged has_pin_override; the stored
        # value is dropped and modulation_bounds.ranges (one [min, max] per component, 0..1) maps the signal
        pname = self.param_pins(dst["type"]).get(to_pin)
        if pname:
            for p in dst.get("parameters", []):
                if p["name"] == pname:
                    p.pop("value", None)
                    p["has_pin_override"] = True
                    if modulation_range:
                        ranges = modulation_range if isinstance(modulation_range[0], (list, tuple)) else [modulation_range]
                        p["modulation_bounds"] = codec.Obj(ranges=[codec.Vec("f2", r) for r in ranges])
        elif modulation_range:
            self.warnings.append(f"modulation_range ignored: {to_pin!r} is not a parameter pin")
        return {"from_node": str(src["id"]), "from_pin": from_pin, "to_node": str(dst["id"]), "to_pin": to_pin}

    def unlink(self, to_id, to_pin: str, from_id=None) -> int:
        dst = self.node(to_id)
        src_id = self.node(from_id)["id"] if from_id is not None else None
        removed = 0
        for n in self.nodes:
            if src_id is not None and n["id"] != src_id:
                continue
            keep = [ln for ln in n.get("links", []) if not (ln.get("to_node") == dst["id"] and ln.get("to_pin") == to_pin)]
            removed += len(n.get("links", [])) - len(keep)
            n["links"] = keep
        pname = self.param_pins(dst["type"]).get(to_pin)
        if removed and pname:  # the parameter is no longer driven: give it back a value
            default = node_types().get(dst["type"], {}).get("params", {}).get(pname, {})
            for p in dst.get("parameters", []):
                if p["name"] == pname:
                    p.pop("has_pin_override", None)
                    p.pop("modulation_bounds", None)
                    if "value" not in p and "default" in default:
                        p["value"] = codec.coerce(default["default"], default["kind"])
        return removed

    @staticmethod
    def param_pins(node_type: str) -> dict[str, str]:
        """Exposed parameter pins: UI label -> parameter name (signal/modulation drives the parameter)."""
        return _param_pins(node_type)

    # ---- subgraphs ----
    @staticmethod
    def subgraph_pins(node: dict) -> tuple[list[str], list[str]]:
        """Pins of a Node_Subgraph = labels of its inner Node_Input / Node_Output nodes."""
        inner = ((node.get("data") or {}).get("graph") or {}).get("nodes", [])
        ins = [n.get("label") or "Input" for n in inner if n.get("type") == "Node_Input"]
        outs = [n.get("label") or "Output" for n in inner if n.get("type") == "Node_Output"]
        return ins, outs

    def expose_param(self, subgraph_id, inner_node_id, param: str, label: str = "") -> dict:
        """Shows an inner node's parameter on the subgraph node (data.exposed_parameters)."""
        sub = self.node(subgraph_id)
        if sub["type"] != "Node_Subgraph":
            raise ProjectError(f"{subgraph_id} is not a Node_Subgraph")
        inner = Project(self.data)
        inner._scope = sub["data"]["graph"]
        target = inner.node(inner_node_id)
        p = next((p for p in target.get("parameters", []) if p["name"] == param), None)
        if p is None:
            raise ProjectError(f"{target['type']} has no parameter {param!r}")
        exposed = sub["data"].setdefault("exposed_parameters", [])
        taken = {e.get("id") for e in exposed}
        eid = next(i for i in iter(lambda: random.randint(1, 65535), None) if i not in taken)
        entry = codec.Obj(id=eid, node_id=target["id"], param_name=param)
        if label:
            entry["label"] = label
        if "value" in p:
            entry["value"] = p["value"]
        exposed.append(entry)
        return {"id": eid, "node_id": str(target["id"]), "param_name": param, "label": label}

    # ---- node data: curves, gradients, splines ----
    def set_curve(self, node_id, curve: str, points: list, interpolation: str = "smooth") -> dict:
        n = self.node(node_id)
        data = n.setdefault("data", codec.Obj())
        keys = build_curve_keys(points, interpolation)
        if curve in ("custom_waveform", "custom_waveform_keys") and "custom_waveform_keys" in data:
            data["custom_waveform_keys"] = keys
            return {"curve": "custom_waveform_keys", "keys": len(keys)}
        curves = data.setdefault("curves", codec.Obj())
        if curve not in curves:
            raise ProjectError(f"{n['type']} has no curve {curve!r} (curves: {sorted(curves)}"
                               + (", custom_waveform" if "custom_waveform_keys" in data else "") + ")")
        curves[curve] = keys
        return {"curve": curve, "keys": len(keys)}

    def set_gradient(self, node_id, stops: list, color_space: str | None = None, index: int = 0) -> dict:
        n = self.node(node_id)
        grads = (n.get("data") or {}).get("gradients")
        if not grads:
            raise ProjectError(f"{n['type']} has no gradient")
        if not 0 <= index < len(grads):
            raise ProjectError(f"gradient index {index} out of range (0..{len(grads) - 1})")
        keys = []
        for s in stops:
            pos, col = (s["position"], s["color"]) if isinstance(s, dict) else (s[0], s[1])
            keys.append(codec.Obj(position=float(pos), color=parse_color(col)))
        if len(keys) < 1:
            raise ProjectError("a gradient needs at least one stop")
        g = grads[index]
        g["keys"] = sorted(keys, key=lambda k: k["position"])
        if color_space:
            if color_space not in GRADIENT_SPACES:
                raise ProjectError(f"color_space must be one of {GRADIENT_SPACES}")
            g["color_space"] = color_space
        return {"stops": len(keys), "color_space": g["color_space"]}

    def set_spline(self, node_id, points: list, closed: bool = False, edges: list | None = None) -> dict:
        """Control points of spline-based nodes (data.keys): Grayscale_Spline, Geometry_Sweep,
        Geometry_Points_Shape (3D points + adjacency) and Utils_Lattice (2D grid positions)."""
        n = self.node(node_id)
        data = n.get("data") or {}
        if "keys" not in data:
            raise ProjectError(f"{n['type']} has no spline/lattice keys")
        if n["type"] == "Node_Utils_Lattice":
            data["keys"] = [codec.Obj(position=codec.Vec("f2", p[:2])) for p in points]
            return {"points": len(points)}
        adj = [[] for _ in points]
        pairs = edges if edges is not None else [[i, i + 1] for i in range(len(points) - 1)]
        if closed and edges is None and len(points) > 2:
            pairs.append([len(points) - 1, 0])
        for a, b in pairs:
            if not (0 <= a < len(points) and 0 <= b < len(points)) or a == b:
                raise ProjectError(f"bad edge {[a, b]}")
            adj[a].append(b)
            adj[b].append(a)
        data["keys"] = [codec.Obj(position=codec.Vec("f3", (list(p) + [0.0, 0.0])[:3]), flags=0, link=sorted(set(nbrs)))
                        for p, nbrs in zip(points, adj, strict=True)]
        return {"points": len(points), "edges": len(pairs)}

    def set_data(self, node_id, key: str, value) -> None:
        """Raw escape hatch for other node.data fields (tagged JSON: vectors as {"__vec__": "f3", "v": [...]})."""
        n = self.node(node_id)
        data = n.setdefault("data", codec.Obj())
        if key not in data:
            raise ProjectError(f"{n['type']}.data has no field {key!r} (fields: {sorted(data)})")
        data[key] = jsonio.load(value)

    # ---- graph annotations: groups (framed comments) and notes (free text) ----
    def add_annotation(self, kind: str, text: str, pos: list | None = None, size: list | None = None,
                       around_nodes: list | None = None, color_idx: int = 0) -> dict:
        if around_nodes:
            nodes = [self.node(i) for i in around_nodes]
            xs = [n["pos"][0] for n in nodes]
            ys = [n["pos"][1] for n in nodes]
            pos = [min(xs) - 60.0, min(ys) - 80.0]
            size = [max(xs) - min(xs) + 360.0, max(ys) - min(ys) + 340.0]  # node cards are ~250 x 220
        if pos is None:
            raise ProjectError("give pos (and size) or around_nodes")
        if kind == "group":
            size = size or [600.0, 400.0]
            item = codec.Obj(comment=text, color_idx=int(color_idx), pos=codec.Vec("f2", pos), size=codec.Vec("f2", size))
            self.graph.setdefault("groups", []).append(item)
            return {"group": len(self.graph["groups"]) - 1}
        if kind == "note":
            size = size or [460.0, 60.0]
            item = codec.Obj(text=text, pos=codec.Vec("f2", pos), size=codec.Vec("f2", size))
            self.graph.setdefault("notes", []).append(item)
            return {"note": len(self.graph["notes"]) - 1}
        raise ProjectError("kind must be 'group' or 'note'")

    def remove_annotation(self, kind: str, index: int) -> None:
        items = self.graph.get("groups" if kind == "group" else "notes", [])
        if not 0 <= index < len(items):
            raise ProjectError(f"no {kind} #{index}")
        items.pop(index)

    def export_nodes(self) -> list[dict]:
        return [n for n in self.nodes if n.get("type", "").startswith(EXPORT_PREFIX)
                and not n.get("disabled") and n.get("on", True)]

    # ---- checks ----
    def validate(self) -> list[str]:
        """Checks the top-level graph and, recursively, every subgraph."""
        saved = (self._scope, self.scope_path)
        issues = []
        self.legacy_notes = []
        try:
            todo = [("", self.data["graph"])]
            while todo:
                path, g = todo.pop()
                self._scope, self.scope_path = g, path
                for msg in self._validate_scope(top=not path):
                    issues.append(f"[subgraph {path}] {msg}" if path else msg)
                for n in g["nodes"]:
                    if n.get("type") == "Node_Subgraph" and isinstance((n.get("data") or {}).get("graph"), dict):
                        todo.append((f"{path}/{n['id']}".strip("/"), n["data"]["graph"]))
        finally:
            self._scope, self.scope_path = saved
        return issues

    def _validate_scope(self, top: bool) -> list[str]:
        issues = []
        types = node_types()
        ids = self._ids()
        for n in self.nodes:
            t = n.get("type")
            if t not in types:
                issues.append(f"node {n.get('id')}: unknown type {t!r} (IlluGen drops it on load)")
                continue
            names = {p["name"] for p in n.get("parameters", [])}
            unknown = names - set(types[t]["params"])
            legacy = sorted(x for x in unknown if legacy_note(t, x))
            for x in legacy:
                where = f"[subgraph {self.scope_path}] " if self.scope_path else ""
                self.legacy_notes.append(f"{where}node {n['id']} ({t}).{x}: {legacy_note(t, x)}")
            unknown -= set(legacy)
            if unknown:
                issues.append(f"node {n['id']} ({t}): parameters unknown to the catalog: {sorted(unknown)}")
            for ln in n.get("links", []):
                if ln.get("to_node") not in ids:
                    issues.append(f"node {n['id']} ({t}): link to missing node {ln.get('to_node')}")
                    continue
                dst = next(x for x in self.nodes if x.get("id") == ln["to_node"])
                problem = self.link_problem(n, ln["from_pin"], dst, ln["to_pin"])
                if problem:
                    issues.append(f"link {n['id']}.{ln['from_pin']} -> {dst['id']}.{ln['to_pin']}: {problem}")
        if len(ids) != len(self.nodes):
            issues.append("duplicate node ids")
        if top and not self.export_nodes():
            issues.append("no enabled Node_Export_* node: rendering will produce nothing")
        return issues

    def thumbnail(self) -> bytes | None:
        th = self.data.get("thumbnail") or {}
        data = th.get("data")
        return data if data else None


def _subgraphs(node: dict):
    stack = [v for k, v in node.items() if k != "parameters"]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if isinstance(x.get("nodes"), list):
                yield x
                continue
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
