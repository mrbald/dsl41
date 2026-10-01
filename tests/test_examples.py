"""The three workflow examples' catalogs lower cleanly (DL-237).

The integration lane in examples/workflows/tests is opt-in, so without this
the default gate never compiles these estates. Each catalog goes through the
door every catalog-consuming CLI verb loads through, with placeholders
resolved from a properties file as `dsl41 run -p` resolves them. Unknown
attributes refuse (DL-07); nothing is permitted through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dsl41.cli_common import load_catalog_or_exit_2

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

EXPECTED = {
    "fulfilment": (
        {"FF_WAVE_B"},
        {
            "FF_RESERVE_C",
            "FF_CANCEL_EARLY_C",
            "FF_PACK_C",
            "FF_CANCEL_LATE_C",
            "FF_LABEL_1_C",
            "FF_LABEL_2_C",
            "FF_MANIFEST_C",
        },
        set(),
    ),
    "energy": (
        {"ENERGY_INITIAL_B", "ENERGY_CORRECTION_B"},
        {
            f"ENERGY_{wave}_{step}"
            for wave in ("INITIAL", "CORRECTION")
            for step in ("INGEST", "CALCULATE", "PUBLISH")
        },
        set(),
    ),
    "media": (
        set(),
        {
            "MEDIA_INPUT",
            "MEDIA_LOW",
            "MEDIA_HIGH",
            "MEDIA_POSTER",
            "MEDIA_STAGE",
            "MEDIA_PUBLISH",
            "MEDIA_VERIFY",
        },
        {"MEDIA_ENCODE"},
    ),
}


@pytest.mark.parametrize("example", sorted(EXPECTED))
def test_example_estate_lowers_without_refusal(example: str, tmp_path: Path) -> None:
    run = tmp_path / "run"
    properties = tmp_path / "site.properties"
    properties.write_text(
        f"WORKER=/usr/bin/python3 /app/examples/{example}/worker.py --run {run}\n"
        f"RUN={run}\n"
        f"PROFILE={run}/profile.sh\n",
        encoding="utf-8",
    )
    boxes, commands, resources = EXPECTED[example]

    catalog = load_catalog_or_exit_2(
        [EXAMPLES / example / "estate.jil"], permit_unknown=False, properties=[properties]
    )

    assert len(catalog.jobs) == len(boxes) + len(commands)
    assert {name for name, job in catalog.jobs.items() if job.job_type == "BOX"} == boxes
    assert {name for name, job in catalog.jobs.items() if job.job_type == "CMD"} == commands
    assert set(catalog.resources) == resources
