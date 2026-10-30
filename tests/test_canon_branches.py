"""Branch tests for src/dsl41/canon.py: the refusals in `digest` and `decode` that
nothing else reaches (a value that is not a mapping, bytes that are not UTF-8)."""

from __future__ import annotations

from typing import Any

import pytest

from dsl41.canon import CanonError, decode, digest


@pytest.mark.parametrize("value", [[1, 2], "text", 7, None])
def test_digest_refuses_a_value_that_is_not_a_mapping(value: Any) -> None:
    with pytest.raises(CanonError, match="digest takes a mapping"):
        digest(value)


def test_digest_names_the_type_it_refused() -> None:
    with pytest.raises(CanonError, match="got list"):
        digest([])


def test_decode_refuses_bytes_that_are_not_utf8_as_a_canon_error() -> None:
    """ss3.2 has one encoding. Invalid UTF-8 raises `CanonError`, so a reader's
    `except CanonError` catches it (DL-151); valid UTF-8 decodes."""
    with pytest.raises(CanonError, match="not UTF-8"):
        decode(b'{"a": "\xff"}')
    assert decode('{"a": "\u00e9"}'.encode()) == {"a": "\u00e9"}
