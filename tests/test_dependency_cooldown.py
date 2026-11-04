"""The locked environment and every Dependabot ecosystem share one delay (DL-465)."""

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = '[tool.uv]\nexclude-newer = "{days} days"\n'
FIRST_ECOSYSTEM = """version: 2
updates:
  - package-ecosystem: uv
    cooldown:
      default-days: {days}
"""
SECOND_ECOSYSTEM = """  - package-ecosystem: github-actions
    cooldown:
      default-days: {days}
"""


def _assert_cooldowns_match(pyproject: str, dependabot: str) -> None:
    duration = tomllib.loads(pyproject)["tool"]["uv"]["exclude-newer"]
    days = re.fullmatch(r"(\d+) days", duration)
    assert days is not None, "uv cooldown must be expressed in days"
    expected_days = int(days[1])
    ecosystems = re.split(r"(?m)^  - ", dependabot)[1:]
    assert ecosystems, "no Dependabot ecosystems found"
    for ecosystem in ecosystems:
        package = re.search(r"(?m)^(?:    )?package-ecosystem: (.+)$", ecosystem)
        assert package is not None, "missing package-ecosystem"
        name = package[1]
        cooldown = re.search(r"(?m)^    cooldown:\n((?:      .*\n)*)", ecosystem)
        assert cooldown is not None, f"{name}: missing cooldown"
        default = re.search(r"(?m)^      default-days: (\d+)$", cooldown[1])
        assert default is not None, f"{name}: missing default-days"
        assert int(default[1]) == expected_days, f"{name}: cooldown differs from uv"


def test_dependency_cooldowns_match() -> None:
    _assert_cooldowns_match(
        (REPO / "pyproject.toml").read_text(),
        (REPO / ".github/dependabot.yml").read_text(),
    )


@pytest.mark.parametrize("days", [5, 7])
def test_cooldown_check_accepts_matching_ecosystems(days: int) -> None:
    _assert_cooldowns_match(
        PYPROJECT.format(days=days),
        (FIRST_ECOSYSTEM + SECOND_ECOSYSTEM).format(days=days),
    )


@pytest.mark.parametrize(
    "later_ecosystems",
    [
        SECOND_ECOSYSTEM.format(days=4),
        "  - package-ecosystem: github-actions\n    cooldown:\n      semver-major-days: 5\n",
        "  - package-ecosystem: github-actions\n    schedule:\n      interval: monthly\n",
        SECOND_ECOSYSTEM.format(days=5) + "  - package-ecosystem: npm\n    directory: /\n",
        "  - directory: /\n    package-ecosystem: docker\n",
    ],
    ids=[
        "mismatched",
        "missing-default-days",
        "missing-cooldown",
        "extra-ecosystem",
        "reordered-missing-cooldown",
    ],
)
def test_cooldown_check_rejects_invalid_ecosystem(later_ecosystems: str) -> None:
    with pytest.raises(AssertionError):
        _assert_cooldowns_match(
            PYPROJECT.format(days=5), FIRST_ECOSYSTEM.format(days=5) + later_ecosystems
        )
