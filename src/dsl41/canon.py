"""Canonical serialization: the one implementation of period-model ss3.2 (DL-119).

A digest is computed over canonical bytes, never over incidental `json.dumps` output, so a
re-serialization that changed nothing does not make `audit` report a mismatch. The encoder is
written out rather than borrowed: `json.dumps(ensure_ascii=False)` leaves U+007F and
U+0080..009F unescaped and accepts floats and lone surrogates, all of which ss3.2 refuses.

The grammar is object, array, string, integer, boolean, null, plus `datetime` (an ISO-8601
naive-UTC string with exactly six fractional digits). Anything else (float, bytes, set, date,
Decimal) is a `CanonError` at any depth, because an unwritable value would make an estate
unsealable (PR-09, PR-11).

`decode` is the reading half and refuses what ss3.2 refuses at ingress: duplicate object keys
(PR-12), floats, non-scalar strings (PR-10a) and an `artifact_format_version` this binary does
not implement (PR-08d).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final, TypeGuard

#: One shared version over every artifact ss3.2 governs (PR-08d); a canonicalization change moves
#: all their bytes together.
ARTIFACT_FORMAT_VERSION: Final[int] = 1

#: The top-level key a digest is computed without; a nested payload key of that name is data
#: (PR-13).
DIGEST_KEY: Final[str] = "digest"

#: ss3.2's short forms. Every other control character goes out as `\u00xx`.
_SHORT_ESCAPES: Final[dict[int, str]] = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


class CanonError(ValueError):
    """A value that cannot be canonicalized, or input that ss3.2 refuses."""


def is_scalar_string(s: str) -> bool:
    """True when `s` is a sequence of Unicode scalar values.

    Python accepts `"\\ud800"`, an unpaired surrogate, as a string, and encoding it as UTF-8
    raises. Every ingress refuses it, because one would make the estate unsealable (PR-10a).
    """
    return not any(0xD800 <= ord(ch) <= 0xDFFF for ch in s)


def is_wire_int(value: object) -> TypeGuard[int]:
    """True when `value` is an integer on the wire, and not a bool (DL-152).

    `bool` subclasses `int`, so `isinstance(v, int)` alone admits `true` as a number.

    A `TypeGuard` narrows the true arm only (`TypeIs` needs 3.13; this package builds on 3.12),
    so a site that uses the number must read it inside the true arm.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def is_scalar_json(value: object) -> bool:
    """True when every string and object key reachable in `value` is scalar (PR-10a).

    The nested form of `is_scalar_string`, for an ingress holding a whole decoded document.
    """
    if isinstance(value, str):
        return is_scalar_string(value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and is_scalar_string(key) and is_scalar_json(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(is_scalar_json(item) for item in value)
    return True


def canonical_bytes(value: object) -> bytes:
    """Encode `value` as ss3.2 canonical JSON.

    UTF-8, `ensure_ascii=false`, separators `(",", ":")`, no whitespace, object keys sorted by
    Unicode code point at every depth, list order kept.
    """
    parts: list[str] = []
    _write(value, parts, "$")
    return "".join(parts).encode("utf-8")


def hash_over(value: object) -> str:
    """`"sha256:" + hexdigest` over the canonical bytes of `value`.

    `catalog_hash` v2 and `runtime_hash` (period-model ss1.1/ss2.1) are this over a whole
    document. `digest` is this over a document minus its own `digest` key."""
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest(value: Mapping[str, Any]) -> str:
    """`"sha256:" + hexdigest` over `value` with only the top-level `digest` key removed (PR-13)."""
    if not isinstance(value, Mapping):
        raise CanonError(f"digest takes a mapping, got {type(value).__name__}")
    return hash_over({key: item for key, item in value.items() if key != DIGEST_KEY})


def with_digest(value: Mapping[str, Any]) -> dict[str, Any]:
    """A new dict: `value` with its `digest` key set to `digest(value)`."""
    return {**value, DIGEST_KEY: digest(value)}


def check_artifact_version(value: object) -> None:
    """Refuse an `artifact_format_version` this binary does not implement, naming it (PR-08d).

    A document with no such key passes; whether one is required is the reader's call.
    """
    if not isinstance(value, Mapping) or "artifact_format_version" not in value:
        return
    version = value["artifact_format_version"]
    if not is_wire_int(version):
        raise CanonError(f"artifact_format_version {version!r} is not an integer")
    if version != ARTIFACT_FORMAT_VERSION:
        raise CanonError(
            f"artifact_format_version {version}: this binary implements {ARTIFACT_FORMAT_VERSION}"
        )


def decode(data: bytes | str) -> object:
    """Decode canonical JSON, refusing what ss3.2 refuses at ingress.

    Refused: duplicate object keys at any depth (PR-12), float literals and the `NaN` and
    `Infinity` constants (PR-11), non-scalar strings (PR-10a), an unimplemented
    `artifact_format_version` (PR-08d), and bytes that are not UTF-8.

    Invalid UTF-8 raises `CanonError`, not `UnicodeDecodeError`, so every reader's
    `except CanonError` catches it (DL-151).
    """
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
    except UnicodeDecodeError as exc:
        raise CanonError(f"not UTF-8: {exc}") from exc
    try:
        obj = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_float=_no_float,
            parse_constant=_no_constant,
        )
    except json.JSONDecodeError as exc:
        raise CanonError(f"not JSON: {exc}") from exc
    if not is_scalar_json(obj):
        raise CanonError("a decoded string is not a sequence of Unicode scalar values")
    check_artifact_version(obj)
    return obj


# ------------------------------------------------------------------ encoder


def _write(value: object, parts: list[str], path: str) -> None:
    """Append one value to `parts`; `path` names the site in a refusal."""
    if value is None:
        parts.append("null")
    elif isinstance(value, bool):  # before int: bool is an int subclass
        parts.append("true" if value else "false")
    elif isinstance(value, int):
        parts.append(str(int(value)))  # int(): an IntEnum writes as its number
    elif isinstance(value, float):
        what = "float" if math.isfinite(value) else "NaN/infinity"
        raise CanonError(f"{path}: {what} {value!r} -- the grammar has no floats (ss3.2)")
    elif isinstance(value, str):
        parts.append(_encode_string(value, path))
    elif isinstance(value, datetime):
        parts.append(_encode_string(naive_utc(value).isoformat(timespec="microseconds"), path))
    elif isinstance(value, Mapping):
        _write_object(value, parts, path)
    elif isinstance(value, (list, tuple)):
        _write_array(value, parts, path)
    else:
        raise CanonError(f"{path}: {type(value).__name__} is not in the ss3.2 value grammar")


def _write_object(value: Mapping[Any, Any], parts: list[str], path: str) -> None:
    keys: list[str] = []
    for key in value:
        if not isinstance(key, str):
            raise CanonError(f"{path}: object key {key!r} is not a string")
        keys.append(key)
    keys.sort()  # Python compares strings by code point, which is ss3.2's order
    parts.append("{")
    for position, key in enumerate(keys):
        if position:
            parts.append(",")
        parts.append(_encode_string(key, f"{path}.{key}"))
        parts.append(":")
        _write(value[key], parts, f"{path}.{key}")
    parts.append("}")


def _write_array(value: list[Any] | tuple[Any, ...], parts: list[str], path: str) -> None:
    parts.append("[")
    for position, item in enumerate(value):
        if position:
            parts.append(",")
        _write(item, parts, f"{path}[{position}]")
    parts.append("]")


def _encode_string(value: str, path: str) -> str:
    """ss3.2's escaping: `"` and `\\` escaped; `\\b \\f \\n \\r \\t` in short form; every other
    control character as lower-case `\\u00xx`; `/` and everything else unescaped."""
    if not is_scalar_string(value):
        raise CanonError(f"{path}: string is not a sequence of Unicode scalar values (PR-10a)")
    out = ['"']
    for ch in value:
        code = ord(ch)
        short = _SHORT_ESCAPES.get(code)
        if short is not None:
            out.append(short)
        elif code <= 0x1F or 0x7F <= code <= 0x9F:
            # Unicode category Cc: U+0000..001F, U+007F, U+0080..009F (DL-128); ss3.2's "control
            # character" means the category
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def naive_utc(value: datetime) -> datetime:
    """An aware datetime as the naive-UTC basis every durable record uses (ss3.2, DL-178j)."""
    return value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)


# ------------------------------------------------------------------ decoder hooks


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise CanonError(f"duplicate object key {key!r} (PR-12)")
        out[key] = value
    return out


def _no_float(literal: str) -> float:
    raise CanonError(f"float literal {literal!r} -- the grammar has no floats (ss3.2)")


def _no_constant(literal: str) -> float:
    raise CanonError(f"{literal} is not in the ss3.2 value grammar")
