"""Builds data/catalog.json and data/blank.illugen by asking the real IlluGen.

Run after updating IlluGen:  python -m illugen_mcp.build_catalog
Output goes to the user data folder (see paths.py) and overrides the bundled tables.

1. Candidate node types = every "Node_*" identifier in IlluGen.exe + every type used by shipped presets.
2. Candidates are written into scratch projects as nodes with *no* parameters and loaded by an invisible
   IlluGen, which fills in every default (parameters, curves, gradients, preset fields) and saves.
   Types IlluGen doesn't know are dropped on load; batches that crash are bisected.
3. Shipped presets add observed enum values and pin names (from links) per node type.
4. data/legacy_params.json: parameters of older project versions that IlluGen renames or drops on load
   (`--legacy` rebuilds only this table).
"""
from __future__ import annotations

import collections
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from . import codec, jsonio
from .paths import DEFAULT_EXE, data_file
from .paths import USER_DATA as DATA
from .runner import IlluGenError, IlluGenSession

BATCH = 30


def _candidates(exe: Path) -> set[str]:
    blob = exe.read_bytes()
    names = {m.decode() for m in re.findall(rb"(?<![A-Za-z0-9_])Node_[A-Z][A-Za-z0-9_]+(?![A-Za-z0-9_])", blob)}
    return {n for n in names if "_$" not in n and len(n) < 64}


def _iter_graphs(v):
    """Yields every graph dict (top-level and nested subgraphs)."""
    stack = [v]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if isinstance(x.get("nodes"), list):
                yield x
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)


def _preset_observations(install: Path):
    files = list((install / "presets_1_0_0").glob("*.illugen")) + list((install / "node_presets").glob("*/*.node_preset"))
    enums = collections.defaultdict(lambda: collections.defaultdict(set))
    pins_in = collections.defaultdict(set)
    pins_out = collections.defaultdict(set)
    usage = collections.Counter()
    types_seen: set[str] = set()
    for f in files:
        try:
            v = codec.read_file(f)
        except codec.FormatError:
            continue
        roots = list(_iter_graphs(v)) or []
        if "type" in v and "parameters" in v:  # single-node preset
            roots.append({"nodes": [v]})
        for g in roots:
            by_id = {n.get("id"): n.get("type") for n in g["nodes"]}
            for n in g["nodes"]:
                t = n.get("type")
                if not t:
                    continue
                types_seen.add(t)
                usage[t] += 1
                for p in n.get("parameters", []):
                    if isinstance(p.get("value"), str):
                        enums[t][p["name"]].add(p["value"])
                for ln in n.get("links", []):
                    pins_out[t].add(ln["from_pin"])
                    if ln.get("to_node") in by_id:
                        pins_in[by_id[ln["to_node"]]].add(ln["to_pin"])
    return types_seen, enums, pins_in, pins_out, usage


def _base_project(install: Path) -> codec.Obj:
    v = codec.read_file(install / "presets_1_0_0" / "stylized_flame_flipbook.illugen")
    v["thumbnail"]["data"] = b""
    v["thumbnail"]["explicitly_created"] = False
    v["settings"]["read_only"] = False
    v["graph"]["nodes"] = []
    v["graph"]["groups"] = []
    v["graph"]["notes"] = []
    return v


def _normalize(base, types: list[str], work: Path, tag: str) -> dict[str, dict] | None:
    """Returns {type: normalized node} or None if IlluGen crashed."""
    v = codec.decode(codec.encode(base))
    v["graph"]["nodes"] = [
        {"type": t, "id": 900000 + i, "pos": codec.Vec("f2", (float(i % 10) * 300, float(i // 10) * 300)),
         "parameters": [], "links": []}
        for i, t in enumerate(types)
    ]
    path = work / f"batch_{tag}.illugen"
    codec.write_file(path, v)
    try:
        with IlluGenSession(path) as s:
            s.save()
    except IlluGenError as e:
        print(f"  batch {tag}: {e}".splitlines()[0], file=sys.stderr)
        return None
    out = codec.read_file(path)
    return {n["type"]: n for n in out["graph"]["nodes"]}


def _probe(base, types: list[str], work: Path, tag: str, found: dict, rejected: set) -> None:
    res = _normalize(base, types, work, tag)
    if res is None:
        if len(types) == 1:
            rejected.add(types[0])
            print(f"  {types[0]}: crashes IlluGen", file=sys.stderr)
            return
        mid = len(types) // 2
        _probe(base, types[:mid], work, tag + "a", found, rejected)
        _probe(base, types[mid:], work, tag + "b", found, rejected)
        return
    for t in types:
        if t in res:
            found[t] = res[t]
        else:
            rejected.add(t)
    print(f"  batch {tag}: {len(res)}/{len(types)} valid", file=sys.stderr)


def _category(t: str) -> str:
    parts = t.split("_")
    return parts[1] if len(parts) > 2 else "Misc"


def build(exe: Path = DEFAULT_EXE) -> dict:
    install = exe.parent
    preset_types, enums, pins_in, pins_out, usage = _preset_observations(install)
    candidates = sorted(_candidates(exe) | preset_types)
    print(f"{len(candidates)} candidate node types", file=sys.stderr)
    base = _base_project(install)
    work = Path(tempfile.mkdtemp(prefix="illugen_catalog_"))
    found: dict[str, dict] = {}
    rejected: set[str] = set()
    try:
        # known-good preset types first, then the binary-only names
        known = [t for t in candidates if t in preset_types]
        unknown = [t for t in candidates if t not in preset_types]
        for group in (known, unknown):
            for i in range(0, len(group), BATCH):
                _probe(base, group[i:i + BATCH], work, f"{len(found)}_{i}", found, rejected)
        # blank project: base with no nodes, normalized by IlluGen
        blank = work / "blank.illugen"
        codec.write_file(blank, base)
        with IlluGenSession(blank) as s:
            s.save()
        DATA.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(blank, DATA / "blank.illugen")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    nodes = {}
    for t, n in sorted(found.items()):
        params = {}
        for p in n.get("parameters", []):
            val = p.get("value")
            entry = {"kind": codec.kind_of(val), "default": codec.to_json(val)}
            if "bounds" in p:
                entry["bounds"] = codec.to_json(p["bounds"])
            if entry["kind"] == "enum":
                entry["values_seen"] = sorted(enums[t][p["name"]] | {val})
            params[p["name"]] = entry
        data = n.get("data", {})
        nodes[t] = {
            "category": _category(t),
            "preset_usage": usage.get(t, 0),
            "inputs_seen": sorted(pins_in[t]),
            "outputs_seen": sorted(pins_out[t]),
            "params": params,
            "curves": sorted((data.get("curves") or {}).keys()) if isinstance(data, dict) else [],
            "template": jsonio.dump(n),
        }
    catalog = {
        "illugen_version": codec.read_file(DATA / "blank.illugen").get("app_version"),
        "node_count": len(nodes),
        "rejected_names": sorted(rejected),
        "nodes": nodes,
    }
    (DATA / "catalog.json").write_text(json.dumps(catalog, indent=1), encoding="utf-8")
    print(f"catalog: {len(nodes)} node types -> {DATA / 'catalog.json'}", file=sys.stderr)
    build_legacy(exe)
    return catalog


# ---------------------------------------------------------------- legacy parameters

def _exe_renames(exe: Path) -> dict[str, str]:
    """IlluGen's project-upgrade table: selector 'parameters.*{name=OLD}.name' followed by the new name
    (e.g. frequency -> frequency.hz, camera_orthogonal -> camera_orthographic_UI)."""
    blob = exe.read_bytes()
    out: dict[str, str] = {}
    for m in re.finditer(rb"parameters\.\*\{name=([A-Za-z0-9_.]+)\}\.name\x00", blob):
        nxt = re.search(rb"[A-Za-z_][A-Za-z0-9_.]{2,}(?=\x00)", blob[m.end():m.end() + 96])
        if nxt:
            out[m.group(1).decode()] = nxt.group().decode()
    return out


def _legacy_pairs(install: Path, params_by_type: dict[str, set]) -> dict[tuple[str, str], list[str]]:
    """(node type, parameter) used by the shipped projects but unknown to the current catalog."""
    pairs: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for f in sorted((install / "presets_1_0_0").glob("*.illugen")):
        for g in _iter_graphs(codec.read_file(f)):
            for n in g["nodes"]:
                known = params_by_type.get(n.get("type"))
                if known is None:
                    continue
                for p in n.get("parameters", []):
                    if p["name"] not in known and f.name not in pairs[(n["type"], p["name"])]:
                        pairs[(n["type"], p["name"])].append(f.name)
    return pairs


def build_legacy(exe: Path = DEFAULT_EXE) -> dict:
    """Builds data/legacy_params.json: parameters written by older IlluGen versions (the shipped presets were
    saved by 1.1.0 to 1.2.0) that the current IlluGen renames or drops when it loads the file. Each one is
    verified by a real load + save of a shipped preset that contains it."""
    install = exe.parent
    cat = json.loads(data_file("catalog.json").read_text(encoding="utf-8"))
    params_by_type = {t: set(n["params"]) for t, n in cat["nodes"].items()}
    renames = _exe_renames(exe)
    pairs = _legacy_pairs(install, params_by_type)
    # smallest set of presets covering every pair (greedy), each loaded and saved once by IlluGen
    todo, chosen = set(pairs), []
    while todo:
        best = max({f for k in todo for f in pairs[k]}, key=lambda f: sum(f in pairs[k] for k in todo))
        chosen.append(best)
        todo -= {k for k in todo if best in pairs[k]}
    table: dict[str, dict] = collections.defaultdict(dict)
    work = Path(tempfile.mkdtemp(prefix="illugen_legacy_"))
    try:
        for name in chosen:
            v = codec.read_file(install / "presets_1_0_0" / name)
            v["settings"]["read_only"] = False
            path = work / name
            codec.write_file(path, v)
            before = {n.get("id"): n for g in _iter_graphs(v) for n in g["nodes"]}
            with IlluGenSession(path) as s:
                s.save()
            after = {n.get("id"): n for g in _iter_graphs(codec.read_file(path)) for n in g["nodes"]}
            for nid, n in before.items():
                t = n.get("type")
                if t not in params_by_type or nid not in after:
                    continue
                old = {p["name"]: p.get("value") for p in n.get("parameters", [])}
                new = {p["name"]: p.get("value") for p in after[nid].get("parameters", [])}
                for pname, val in old.items():
                    if pname in params_by_type[t] or pname in table[t]:
                        continue
                    target = renames.get(pname)
                    if pname in new:
                        entry = {"action": "kept"}  # still read by IlluGen: the catalog is incomplete
                    elif target in params_by_type[t] and codec.to_json(new.get(target)) == codec.to_json(val):
                        entry = {"action": "renamed", "to": target}
                    else:
                        entry = {"action": "dropped"}
                    entry["sample"] = name
                    entry["presets"] = len(pairs[(t, pname)])
                    table[t][pname] = entry
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out = {
        "illugen_version": cat.get("illugen_version"),
        "about": "Parameters written by older IlluGen versions that the current one renames ('renamed', value "
                 "moved to 'to') or ignores ('dropped') on load. Verified by loading and saving 'sample' in "
                 "IlluGen. Regenerate with `python -m illugen_mcp.build_catalog --legacy`.",
        "nodes": {t: dict(sorted(v.items())) for t, v in sorted(table.items())},
    }
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "legacy_params.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"legacy parameters: {sum(map(len, table.values()))} on {len(table)} node types "
          f"({len(chosen)} presets round-tripped) -> {DATA / 'legacy_params.json'}", file=sys.stderr)
    return out


if __name__ == "__main__":
    if "--legacy" in sys.argv[1:]:
        build_legacy()
    else:
        build()
