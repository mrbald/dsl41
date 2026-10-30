"""`reread` as a module (spec-reload steps 1 to 6): load, lint, diff, store the plan.

The nightbank small estate is the large case; a tiny estate written in the test is the box case.
`Live` is built from the files on disk through `load_estate`, so an unedited estate is unchanged.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from dsl41 import reload_plan
from dsl41.canon import canonical_bytes, decode
from dsl41.reload_plan import Live, RereadRefused, RereadResult, load_estate, read_plan, reread

SMALL = Path(__file__).resolve().parent.parent / "examples" / "nightbank" / "estate" / "small"

START = datetime(2026, 1, 6)

PROPERTIES = """\
OWNER=tester
NB_DATA=/var/nb/data
NB_LOGS=/var/nb/logs
NB_PROFILE=/var/nb/profile.env
EOD_AMER=19:11
EOD_APAC=09:05
EOD_EMEA=01:08
HB_WINDOW=00:05-01:35
"""


def _nightbank(tmp_path: Path) -> Path:
    """A writable copy of the small estate, its manifest unchanged. The site layer
    `night.properties` sits beside it, as `run -p` takes it, not inside it."""
    target = tmp_path / "estate"
    target.mkdir()
    for path in SMALL.glob("*.jil"):
        shutil.copy(path, target)
    shutil.copy(SMALL / "estate.toml", target)
    (tmp_path / "night.properties").write_text(PROPERTIES)
    return target


def _site(estate: Path) -> dict[str, Any]:
    props = estate.parent / "night.properties"
    return {"properties": [props], "environment": "sandbox"} if props.exists() else {}


def _live(estate: Path, revision: int = 1, status: dict[str, str] | None = None) -> Live:
    loaded = load_estate(estate, **_site(estate))
    return Live(loaded.catalog, loaded.settings, revision, status or {})


def _reread(estate: Path, live: Live, plans: Path) -> RereadResult:
    return reread(estate, live, plans, start=START, **_site(estate))


def _edit(estate_dir: Path, name: str, old: str, new: str) -> None:
    path = estate_dir / name
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def _tiny(tmp_path: Path, jil: str, extra: str = "") -> Path:
    target = tmp_path / "tiny"
    target.mkdir()
    (target / "a.jil").write_text("insert_machine: m1\ntype: a\nnode_name: localhost\n\n" + jil)
    (target / "estate.toml").write_text(f'name = "tiny"\nfiles = ["a.jil"]\n{extra}')
    return target


_BOX = "insert_job: BX\njob_type: b\n\n"


def _member(name: str, box: str = "BX", command: str = "true") -> str:
    return (
        f"insert_job: {name}\njob_type: c\nbox_name: {box}\n"
        f"command: {command}\nmachine: m1\nowner: tester\n\n"
    )


def test_unedited_estate_is_unchanged_and_the_token_is_stable(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate)
    plans = tmp_path / "plans"
    first = _reread(estate, live, plans)
    second = _reread(estate, live, plans)
    assert first.token == second.token
    assert first.plan.added == first.plan.removed == first.plan.changed == ()
    assert len(first.plan.unchanged) == len(live.catalog.jobs) > 20
    assert first.stop == ()
    assert first.plan_dir == plans / first.token
    sources = {p.name for p in estate.iterdir()}
    stored = {
        p.relative_to(first.plan_dir).as_posix() for p in first.plan_dir.rglob("*") if p.is_file()
    }
    assert stored == {*sources, "site/night.properties", "non-idempotent.toml", "plan.json"}
    for path in first.files:
        name = path.relative_to(first.plan_dir)
        if name.name == "night.properties":
            assert path.read_bytes() == (tmp_path / "night.properties").read_bytes()
        elif name.name != "non-idempotent.toml":
            assert path.read_bytes() == (estate / name).read_bytes()
    assert (first.plan_dir / "non-idempotent.toml").read_bytes() == b""


def test_a_changed_job_and_an_added_job(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate)
    _edit(estate, "global.jil", "OPS_SPOOL_C --sleep 240", "OPS_SPOOL_C --sleep 241")
    with (estate / "global.jil").open("a") as handle:
        handle.write(
            "\ninsert_job: ZZ_NEW_C\njob_type: c\nbox_name: OPS_B\ncommand: true\n"
            "machine: ops_util\nowner: ~{$OWNER}~\n"
        )
    result = _reread(estate, live, tmp_path / "plans")
    assert result.plan.added == ("ZZ_NEW_C",)
    assert [change.name for change in result.plan.changed] == ["OPS_SPOOL_C"]
    assert result.plan.changed[0].reason is None
    assert result.plan.removed == ()
    assert result.stop == ()
    document = read_plan(tmp_path / "plans", result.token)
    assert document is not None
    assert document["added"] == ["ZZ_NEW_C"]
    assert document["stop"] == []


def test_a_removed_job_that_another_references_is_refused(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate)
    text = (estate / "global.jil").read_text()
    stripped = re.sub(r"insert_job: SOD_PREFLIGHT_C\n.*?(?=insert_job:)", "", text, flags=re.S)
    assert stripped != text
    (estate / "global.jil").write_text(stripped)
    with pytest.raises(RereadRefused) as refused:
        _reread(estate, live, tmp_path / "plans")
    assert any(
        "L001" in finding and "SOD_PREFLIGHT_C" in finding for finding in refused.value.findings
    )
    assert not (tmp_path / "plans").exists()


def test_a_member_added_to_a_running_box_is_refused(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1"))
    live = _live(estate, status={"BX": "RUNNING"})
    (estate / "a.jil").write_text((estate / "a.jil").read_text() + _member("M2"))
    with pytest.raises(RereadRefused) as refused:
        _reread(estate, live, tmp_path / "plans")
    assert refused.value.findings == ("box_running BX",)
    assert not (tmp_path / "plans").exists()


def test_a_member_added_to_an_idle_box_is_planned(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1"))
    live = _live(estate, status={"BX": "INACTIVE"})
    (estate / "a.jil").write_text((estate / "a.jil").read_text() + _member("M2"))
    assert _reread(estate, live, tmp_path / "plans").plan.added == ("M2",)


def test_a_changed_running_box_with_the_same_members_stops_nothing(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1"))
    live = _live(estate, status={"BX": "RUNNING", "M1": "RUNNING"})
    _edit(
        estate,
        "a.jil",
        "insert_job: BX\njob_type: b\n",
        "insert_job: BX\njob_type: b\ndescription: x\n",
    )
    result = _reread(estate, live, tmp_path / "plans")
    assert [change.name for change in result.plan.changed] == ["BX"]
    assert result.stop == ()


def test_a_changed_running_command_job_is_in_the_stop_set(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate, status={"OPS_SPOOL_C": "RUNNING", "OPS_ARCHIVE_C": "STARTING"})
    _edit(estate, "global.jil", "OPS_SPOOL_C --sleep 240", "OPS_SPOOL_C --sleep 241")
    _edit(estate, "global.jil", "OPS_ARCHIVE_C --sleep 35", "OPS_ARCHIVE_C --sleep 36")
    result = _reread(estate, live, tmp_path / "plans")
    assert result.stop == ("OPS_ARCHIVE_C", "OPS_SPOOL_C")
    document = read_plan(tmp_path / "plans", result.token)
    assert document is not None
    assert document["stop"] == ["OPS_ARCHIVE_C", "OPS_SPOOL_C"]


def test_a_changed_job_that_is_not_up_is_not_stopped(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate, status={"OPS_SPOOL_C": "SUCCESS"})
    _edit(estate, "global.jil", "OPS_SPOOL_C --sleep 240", "OPS_SPOOL_C --sleep 241")
    assert _reread(estate, live, tmp_path / "plans").stop == ()


def test_the_token_changes_when_only_the_marked_set_changes(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate)
    plans = tmp_path / "plans"
    bare = _reread(estate, live, plans)
    (estate / "non-idempotent.toml").write_text('jobs = ["OPS_SPOOL_C"]\n')
    marked = _reread(estate, live, plans)
    assert marked.token != bare.token
    assert marked.plan == bare.plan
    assert (marked.plan_dir / "non-idempotent.toml").read_text() == 'jobs = ["OPS_SPOOL_C"]\n'


def test_the_token_changes_when_the_live_revision_moves(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    one = _reread(estate, _live(estate, revision=1), tmp_path / "plans")
    two = _reread(estate, _live(estate, revision=2), tmp_path / "plans")
    assert one.token != two.token
    assert decode((two.plan_json).read_bytes())["live_rev"] == 2  # type: ignore[call-overload]


def test_a_formatting_only_edit_is_unchanged(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate)
    _edit(
        estate, "global.jil", "insert_job: OPS_SPOOL_C\n", "/* a note */\ninsert_job: OPS_SPOOL_C\n"
    )
    result = _reread(estate, live, tmp_path / "plans")
    assert result.plan.changed == ()
    assert result.plan.unchanged == tuple(sorted(live.catalog.jobs))


def test_an_estate_setting_change_marks_every_job_changed(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate)
    _edit(estate, "estate.toml", 'timezone = "UTC"', 'timezone = "Europe/Zurich"')
    result = _reread(estate, live, tmp_path / "plans")
    assert len(result.plan.changed) == len(live.catalog.jobs)
    assert {change.reason for change in result.plan.changed} == {"estate setting timezone changed"}


def test_plan_json_round_trips_through_canon(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate, revision=7, status={"OPS_SPOOL_C": "RUNNING"})
    _edit(estate, "global.jil", "OPS_SPOOL_C --sleep 240", "OPS_SPOOL_C --sleep 241")
    result = _reread(estate, live, tmp_path / "plans")
    raw = result.plan_json.read_bytes()
    document = decode(raw)
    assert isinstance(document, dict)
    assert document["token"] == result.token
    assert document["live_rev"] == 7
    assert document["stop"] == ["OPS_SPOOL_C"]
    assert document["changed"][0]["name"] == "OPS_SPOOL_C"
    assert document["settings"]["timezone"] == "UTC"
    assert document["unchanged"] == list(result.plan.unchanged)
    names = [part["name"] for part in document["parts"]]
    assert names[0] == "estate.toml"
    assert names[-1] == "non-idempotent.toml"
    for part in document["parts"]:
        data = (result.plan_dir / part["name"]).read_bytes()
        assert part["sha256"] == hashlib.sha256(data).hexdigest()


def test_a_rerun_rewrites_the_same_bytes(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate)
    first = _reread(estate, live, tmp_path / "plans")
    before = {p.name: p.read_bytes() for p in first.plan_dir.rglob("*") if p.is_file()}
    second = _reread(estate, live, tmp_path / "plans")
    assert {p.name: p.read_bytes() for p in second.plan_dir.rglob("*") if p.is_file()} == before


def test_a_crash_before_plan_json_leaves_no_complete_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate)
    plans = tmp_path / "plans"
    real = reload_plan.durable_write

    def crash_at_plan_json(path: str, data: bytes, **kwargs: int) -> None:
        if Path(path).name == "plan.json":
            raise OSError("power cut")
        real(path, data, **kwargs)

    monkeypatch.setattr(reload_plan, "durable_write", crash_at_plan_json)
    with pytest.raises(OSError, match="power cut"):
        _reread(estate, live, plans)
    (token_dir,) = plans.iterdir()
    assert (token_dir / "estate.toml").exists()
    assert not (token_dir / "plan.json").exists()
    assert read_plan(plans, token_dir.name) is None
    monkeypatch.setattr(reload_plan, "durable_write", real)
    result = _reread(estate, live, plans)
    assert result.plan_dir == token_dir
    assert read_plan(plans, result.token) is not None


def test_a_plan_with_a_damaged_file_is_not_complete(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    result = _reread(estate, _live(estate), tmp_path / "plans")
    assert read_plan(tmp_path / "plans", result.token) is not None
    (result.plan_dir / "amer.jil").write_bytes(b"cut")
    assert read_plan(tmp_path / "plans", result.token) is None
    (result.plan_dir / "amer.jil").unlink()
    assert read_plan(tmp_path / "plans", result.token) is None


def test_a_lint_warning_refuses_under_strict(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1"))
    live = _live(estate)
    (estate / "a.jil").write_text(
        (estate / "a.jil").read_text()
        + "insert_job: LONE\njob_type: c\ncommand: true\nmachine: m1\nowner: tester\n"
    )
    with pytest.raises(RereadRefused) as refused:
        _reread(estate, live, tmp_path / "plans")
    assert any("L011" in finding for finding in refused.value.findings)


@pytest.mark.parametrize(
    ("manifest", "needle"),
    [
        ('name = "t"\nfiles = ["missing.jil"]\n', "missing.jil"),
        (
            'name = "t"\nfiles = ["a.jil"]\n[environments.x]\nproperties = ["a.jil"]\n',
            "environment",
        ),
        ('name = "t"\nfiles = ["a.jil"]\nbogus = 1\n', "bogus"),
        ('name = "t"\nfiles = ["a.jil"]\ntimezone = "Not/AZone"\n', "timezone"),
    ],
)
def test_a_manifest_that_cannot_declare_an_estate_is_refused(
    tmp_path: Path, manifest: str, needle: str
) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1"))
    (estate / "estate.toml").write_text(manifest)
    with pytest.raises(RereadRefused, match=needle):
        load_estate(estate)


def test_a_jil_error_is_refused_with_its_finding(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, "insert_job: J\njob_type: c\nbogus_attr: 1\n")
    with pytest.raises(RereadRefused, match="bogus_attr"):
        load_estate(estate)


def test_the_real_manifest_needs_an_environment_as_run_does(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    props = [tmp_path / "night.properties"]
    with pytest.raises(RereadRefused, match="choose an environment"):
        load_estate(estate, props)
    with pytest.raises(RereadRefused, match="no environment 'nope'"):
        load_estate(estate, props, "nope")
    assert load_estate(estate, props, "wargame").catalog.jobs


def test_the_site_layer_is_a_part_and_moves_the_token(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate)
    one = _reread(estate, live, tmp_path / "plans")
    (tmp_path / "night.properties").write_text(PROPERTIES.replace("/var/nb/logs", "/var/nb/log2"))
    two = _reread(estate, live, tmp_path / "plans")
    assert two.token != one.token
    assert len(two.plan.changed) > 0
    assert (two.plan_dir / "site" / "night.properties").exists()


def test_the_estate_without_its_site_layer_is_refused(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    with pytest.raises(RereadRefused, match="EOD_AMER"):
        load_estate(estate, (), "sandbox")


def test_a_calendar_only_change_puts_a_running_job_in_the_stop_set(tmp_path: Path) -> None:
    jil = (
        "calendar: HOL\n01/01/2026\n\n"
        "insert_job: J1\njob_type: c\ncommand: true\nmachine: m1\nowner: tester\n"
        'date_conditions: 1\ndays_of_week: all\nexclude_calendar: HOL\nstart_times: "01:00"\n'
    )
    estate = _tiny(tmp_path, jil)
    live = _live(estate, status={"J1": "RUNNING"})
    _edit(estate, "a.jil", "01/01/2026", "12/25/2026")
    result = _reread(estate, live, tmp_path / "plans")
    (change,) = result.plan.changed
    assert change.name == "J1"
    assert change.reason == "calendar HOL changed"
    assert result.stop == ("J1",)


def test_a_removed_running_job_is_in_the_stop_set(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1") + _member("M2"))
    live = _live(estate, status={"M2": "RUNNING"})
    _edit(estate, "a.jil", _member("M2"), "")
    result = _reread(estate, live, tmp_path / "plans")
    assert result.plan.removed == ("M2",)
    assert result.stop == ("M2",)


def test_the_token_changes_when_only_the_timezone_map_changes(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1"), 'timezone_map = "tz-aliases"\n')
    (estate / "tz-aliases").write_text("ZRH Europe/Zurich\n")
    live = _live(estate)
    one = _reread(estate, live, tmp_path / "plans")
    (estate / "tz-aliases").write_text("ZRH Europe/Zurich\nLON Europe/London\n")
    two = _reread(estate, live, tmp_path / "plans")
    assert two.token != one.token
    assert len(two.plan.changed) == len(live.catalog.jobs)
    assert (two.plan_dir / "tz-aliases").exists()


def test_an_input_that_changes_while_loading_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = _nightbank(tmp_path)
    live = _live(estate)
    real = reload_plan.lower_catalog

    def edit_then_lower(*args: Any, **kwargs: Any) -> Any:
        _edit(estate, "estate.toml", 'timezone = "UTC"', 'timezone = "Asia/Tokyo"')
        return real(*args, **kwargs)

    monkeypatch.setattr(reload_plan, "lower_catalog", edit_then_lower)
    with pytest.raises(RereadRefused, match="changed while it was being read"):
        _reread(estate, live, tmp_path / "plans")
    assert not (tmp_path / "plans").exists()


def test_the_manifest_is_parsed_from_the_bytes_that_are_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = _nightbank(tmp_path)
    real = reload_plan.resolve_manifest

    def edit_after_parse(*args: Any, **kwargs: Any) -> Any:
        inputs = real(*args, **kwargs)
        _edit(estate, "estate.toml", 'timezone = "UTC"', 'timezone = "Asia/Tokyo"')
        return inputs

    monkeypatch.setattr(reload_plan, "resolve_manifest", edit_after_parse)
    with pytest.raises(RereadRefused, match="changed while it was being read"):
        load_estate(estate, [tmp_path / "night.properties"], "sandbox")


def test_a_symlinked_jil_file_is_followed_as_run_follows_it(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1"))
    real = tmp_path / "shared.jil"
    (estate / "a.jil").rename(real)
    (estate / "a.jil").symlink_to(real)
    assert "M1" in load_estate(estate).catalog.jobs


def test_a_reserved_name_is_refused_whatever_its_case(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1"))
    (estate / "Plan.JSON").write_bytes((estate / "a.jil").read_bytes())
    (estate / "estate.toml").write_text('name = "t"\nfiles = ["a.jil", "Plan.JSON"]\n')
    with pytest.raises(RereadRefused, match="reserved"):
        load_estate(estate)


def test_two_names_that_differ_only_in_case_are_refused(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1"))
    site_a = tmp_path / "x" / "Site.properties"
    site_b = tmp_path / "y" / "site.properties"
    for path in (site_a, site_b):
        path.parent.mkdir()
        path.write_text("K=v\n")
    with pytest.raises(RereadRefused, match="one name"):
        load_estate(estate, [site_a, site_b])


def test_read_plan_refuses_a_plan_whose_token_does_not_match_its_parts(tmp_path: Path) -> None:
    estate = _nightbank(tmp_path)
    result = _reread(estate, _live(estate, revision=3), tmp_path / "plans")
    document = decode(result.plan_json.read_bytes())
    assert isinstance(document, dict)
    document["live_rev"] = 4
    result.plan_json.write_bytes(canonical_bytes(document) + b"\n")
    assert read_plan(tmp_path / "plans", result.token) is None


def test_a_preflight_error_refuses_the_plan(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1"))
    live = _live(estate)
    text = (estate / "a.jil").read_text()
    (estate / "a.jil").write_text(
        text.replace("insert_job: BX", "insert_resource: R1\nres_type: R\n\ninsert_job: BX", 1)
        + "insert_job: FT\njob_type: c\nbox_name: BX\ncommand: true\nmachine: m1\n"
        "owner: tester\nresources: (R1, QUANTITY=1)\n"
    )
    with pytest.raises(RereadRefused) as refused:
        _reread(estate, live, tmp_path / "plans")
    assert any("preflight ERROR [resources] FT" in finding for finding in refused.value.findings)
    assert not (tmp_path / "plans").exists()


def test_a_preflight_warning_goes_into_plan_json(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1"))
    live = _live(estate)
    _edit(
        estate,
        "a.jil",
        "insert_job: M1\njob_type: c\n",
        "insert_job: M1\njob_type: c\nn_retrys: 2\n",
    )
    result = _reread(estate, live, tmp_path / "plans")
    document = read_plan(tmp_path / "plans", result.token)
    assert document is not None
    (warning,) = document["preflight_warnings"]
    assert warning["code"] == "n-retrys"
    assert warning["job"] == "M1"


def test_a_job_with_envvars_that_run_refuses_is_refused(tmp_path: Path) -> None:
    estate = _tiny(tmp_path, _BOX + _member("M1"))
    live = _live(estate)
    _edit(
        estate,
        "a.jil",
        "insert_job: M1\njob_type: c\n",
        "insert_job: M1\njob_type: c\nenvvars: A=1\n",
    )
    with pytest.raises(RereadRefused) as refused:
        _reread(estate, live, tmp_path / "plans")
    assert any("preflight ERROR [envvars] M1" in finding for finding in refused.value.findings)
    assert not (tmp_path / "plans").exists()
