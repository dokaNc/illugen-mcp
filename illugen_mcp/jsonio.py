"""Lossless JSON storage for decoded IlluGen values (used for the catalog and node templates)."""
from __future__ import annotations

import base64
from typing import Any

from .codec import Obj, Vec


def dump(v: Any) -> Any:
    if isinstance(v, Vec):
        return {"__vec__": v.kind, "v": list(v)}
    if isinstance(v, (bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, dict):
        d = {k: dump(x) for k, x in v.items()}
        dups = getattr(v, "dups", None)
        if dups:
            d["__dups__"] = [[p, k, dump(x)] for p, k, x in dups]
        return d
    if isinstance(v, list):
        return [dump(x) for x in v]
    return v


def load(v: Any) -> Any:
    if isinstance(v, dict):
        if "__vec__" in v:
            return Vec(v["__vec__"], v["v"])
        if "__bytes__" in v:
            return base64.b64decode(v["__bytes__"])
        o = Obj((k, load(x)) for k, x in v.items() if k != "__dups__")
        o.dups = [(p, k, load(x)) for p, k, x in v.get("__dups__", [])]
        return o
    if isinstance(v, list):
        return [load(x) for x in v]
    return v
