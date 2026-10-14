"""Lossless codec for IlluGen's binary serialization (.illugen projects, .node_preset files).

Format (IlluGen 1.2.1; round-trips byte-exact on all shipped presets):

    0x01 bool    1 byte
    0x05 int     int64 LE
    0x06 int2    2 x int64
    0x09 float   float64 LE
    0x0a float2  2 x float64
    0x0b float3  3 x float64
    0x0c float4  4 x float64
    0x0d bytes   u32 length + raw bytes (e.g. embedded JPEG thumbnail)
    0x0e string  u32 length + UTF-8 bytes
    0x0f strref  u32 offset of the length field of an earlier 0x0e string (string interning)
    0x10 array   values ... 0x12
    0x11 object  (key value)* ... 0x12   keys are strings; duplicate keys occur in the wild
    0x12 end

Every string (key or value) is written in full the first time and as a back-reference afterwards.
"""
from __future__ import annotations

import struct
from typing import Any

T_BOOL, T_INT, T_INT2, T_FLOAT, T_FLOAT2, T_FLOAT3, T_FLOAT4 = 0x01, 0x05, 0x06, 0x09, 0x0A, 0x0B, 0x0C
T_BYTES, T_STR, T_STRREF, T_ARRAY, T_OBJECT, T_END = 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12

_VEC = {T_INT2: ("i2", "<2q", 16), T_FLOAT2: ("f2", "<2d", 16), T_FLOAT3: ("f3", "<3d", 24), T_FLOAT4: ("f4", "<4d", 32)}
_VEC_BY_KIND = {kind: (tag, fmt) for tag, (kind, fmt, _) in _VEC.items()}


class Vec(tuple):
    """Fixed-size numeric vector. kind is one of i2, f2, f3, f4."""

    kind: str

    def __new__(cls, kind: str, values):
        if kind not in _VEC_BY_KIND:
            raise ValueError(f"unknown vector kind {kind!r}")
        size = int(kind[1])
        values = tuple(int(v) if kind[0] == "i" else float(v) for v in values)
        if len(values) != size:
            raise ValueError(f"{kind} needs {size} components, got {len(values)}")
        obj = super().__new__(cls, values)
        obj.kind = kind
        return obj

    def __repr__(self) -> str:
        return f"Vec({self.kind!r}, {tuple(self)!r})"


class Obj(dict):
    """Ordered object that also remembers duplicate keys (later occurrences) for exact re-encoding."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dups: list[tuple[int, str, Any]] = []  # (position among keys, key, value)


class FormatError(ValueError):
    pass


def decode(data: bytes) -> Any:
    strings: dict[int, str] = {}
    n = len(data)

    def need(i: int, k: int) -> None:
        if i + k > n:
            raise FormatError(f"truncated data at offset {i:#x}")

    def val(i: int):
        need(i, 1)
        t = data[i]
        i += 1
        if t == T_OBJECT:
            o = Obj()
            pos = 0
            while True:
                need(i, 1)
                if data[i] == T_END:
                    return o, i + 1
                k, i = val(i)
                if not isinstance(k, str):
                    raise FormatError(f"non-string object key at {i:#x}")
                v, i = val(i)
                if k in o:
                    o.dups.append((pos, k, v))
                else:
                    o[k] = v
                pos += 1
        if t == T_ARRAY:
            a = []
            while True:
                need(i, 1)
                if data[i] == T_END:
                    return a, i + 1
                v, i = val(i)
                a.append(v)
        if t == T_STR:
            need(i, 4)
            ln = struct.unpack_from("<I", data, i)[0]
            need(i + 4, ln)
            s = data[i + 4 : i + 4 + ln].decode("utf-8")
            strings[i] = s
            return s, i + 4 + ln
        if t == T_STRREF:
            need(i, 4)
            off = struct.unpack_from("<I", data, i)[0]
            if off not in strings:
                raise FormatError(f"dangling string reference {off:#x} at {i:#x}")
            return strings[off], i + 4
        if t == T_BYTES:
            need(i, 4)
            ln = struct.unpack_from("<I", data, i)[0]
            need(i + 4, ln)
            return bytes(data[i + 4 : i + 4 + ln]), i + 4 + ln
        if t == T_BOOL:
            need(i, 1)
            return data[i] != 0, i + 1
        if t == T_INT:
            need(i, 8)
            return struct.unpack_from("<q", data, i)[0], i + 8
        if t == T_FLOAT:
            need(i, 8)
            return struct.unpack_from("<d", data, i)[0], i + 8
        if t in _VEC:
            kind, fmt, size = _VEC[t]
            need(i, size)
            return Vec(kind, struct.unpack_from(fmt, data, i)), i + size
        raise FormatError(f"unknown tag {t:#04x} at offset {i - 1:#x}")

    value, end = val(0)
    if end != n:
        raise FormatError(f"trailing data: parsed {end} of {n} bytes")
    return value


def encode(value: Any) -> bytes:
    out = bytearray()
    seen: dict[str, int] = {}

    def w(v: Any) -> None:
        if isinstance(v, bool):
            out.append(T_BOOL)
            out.append(1 if v else 0)
        elif isinstance(v, int):
            out.append(T_INT)
            out.extend(struct.pack("<q", v))
        elif isinstance(v, float):
            out.append(T_FLOAT)
            out.extend(struct.pack("<d", v))
        elif isinstance(v, str):
            if v in seen:
                out.append(T_STRREF)
                out.extend(struct.pack("<I", seen[v]))
            else:
                b = v.encode("utf-8")
                out.append(T_STR)
                seen[v] = len(out)
                out.extend(struct.pack("<I", len(b)))
                out.extend(b)
        elif isinstance(v, (bytes, bytearray)):
            out.append(T_BYTES)
            out.extend(struct.pack("<I", len(v)))
            out.extend(v)
        elif isinstance(v, Vec):
            tag, fmt = _VEC_BY_KIND[v.kind]
            out.append(tag)
            out.extend(struct.pack(fmt, *v))
        elif isinstance(v, dict):
            out.append(T_OBJECT)
            dups = sorted(getattr(v, "dups", []), key=lambda d: d[0])
            items = list(v.items())
            for pos, k, x in dups:  # re-insert duplicates at their original positions
                items.insert(pos, (k, x))
            for k, x in items:
                w(k)
                w(x)
            out.append(T_END)
        elif isinstance(v, (list, tuple)):
            out.append(T_ARRAY)
            for x in v:
                w(x)
            out.append(T_END)
        elif v is None:
            raise TypeError("None is not representable in the IlluGen format")
        else:
            raise TypeError(f"cannot encode {type(v).__name__}")

    w(value)
    return bytes(out)


def read_file(path) -> Any:
    with open(path, "rb") as f:
        return decode(f.read())


def write_file(path, value: Any) -> None:
    data = encode(value)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    import os

    os.replace(tmp, path)


# ---- JSON bridge (for MCP tool inputs/outputs) ----

def to_json(v: Any, *, bytes_placeholder: bool = True) -> Any:
    """Convert decoded values to JSON-safe data. Vectors become plain lists."""
    if isinstance(v, Vec):
        return list(v)
    if isinstance(v, dict):
        return {k: to_json(x, bytes_placeholder=bytes_placeholder) for k, x in v.items()}
    if isinstance(v, list):
        return [to_json(x, bytes_placeholder=bytes_placeholder) for x in v]
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>" if bytes_placeholder else v.hex()
    return v


def kind_of(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "enum"
    if isinstance(v, Vec):
        return v.kind
    if isinstance(v, (bytes, bytearray)):
        return "bytes"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "array"
    return type(v).__name__


def coerce(value: Any, kind: str) -> Any:
    """Coerce a JSON value to the IlluGen kind of an existing parameter."""
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValueError(f"expected bool, got {value!r}")
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"expected number, got {value!r}")
        return float(value)
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise ValueError(f"expected integer, got {value!r}")
        return int(value)
    if kind == "enum":
        if not isinstance(value, str):
            raise ValueError(f"expected string, got {value!r}")
        return value
    if kind in _VEC_BY_KIND:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"expected {kind} list, got {value!r}")
        return Vec(kind, value)
    raise ValueError(f"parameters of kind {kind} cannot be set")
