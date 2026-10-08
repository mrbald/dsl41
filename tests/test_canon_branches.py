"""Branch tests for src/dsl41/canon.py: the one refusal in `digest` that
nothing else reaches (a value that is not a mapping)."""

from __future__ import annotations

from typing import Any

import pytest

from dsl41.canon import CanonError, digest


@pytest.mark.parametrize("value", [[1, 2], "text", 7, None])
def test_digest_refuses_a_value_that_is_not_a_mapping(value: Any) -> None:
    with pytest.raises(CanonError, match="digest takes a mapping"):
        digest(value)


def test_digest_names_the_type_it_refused() -> None:
    with pytest.raises(CanonError, match="got list"):
        digest([])
