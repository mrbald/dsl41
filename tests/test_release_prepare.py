"""DL-407, DL-412: `dsl41 release prepare` and `release requalify`, and the service side's refusals. The site and the stub engine are tests/release_site.py's."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from release_site import ME, SMALL, Site, _script, site_at


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield from site_at(tmp_path)


def test_prepare_installs_a_read_only_release_with_its_qualification(site: Site) -> None:
    done = site.run("prepare", "v2", str(site.source("v2", add_job=True)))
    assert done.returncode == 0, done.stderr
    release = site.releases / "v2"
    assert not (site.releases / "v2.new").exists()
    prepared = site.receipt("v2", "prepared")
    assert prepared["lint"]["exit"] == 0 and prepared["rehearse"]["exit"] == 0
    assert prepared["jobs"] == 83 and prepared["max_jobs"] == 107
    assert prepared["grace_s"] == 10.0 and prepared["max_grace_s"] == 10.0
    assert prepared["dsl41_version"] and prepared["environment"] == "wargame"
    assert set(prepared["files"]) >= {"estate.toml", "global.jil", "added.jil"}
    assert prepared["files"]["added.jil"].startswith("sha256:")
    # the release's manifest under the launcher's environment, then the site's layer (DL-422)
    estate = ["--manifest", f"{release}/estate.toml", "--environment", "wargame"]
    night = ["-p", str(site.base / "night" / "night.properties")]
    assert prepared["lint"]["argv"] == [str(site.dsl41), "lint", *estate, *night]
    assert prepared["rehearse"]["argv"] == [
        str(site.dsl41),
        "rehearse",
        *estate,
        *night,
        "--format",
        "json",
    ]
    assert not os.access(release / "global.jil", os.W_OK)
    assert not os.access(release, os.W_OK)
    assert os.access(release / ".release", os.W_OK)
    # the check ran as the service account, through the launcher's DSL41
    # the copy and the check ran as the service account, through the launcher's DSL41
    assert site.sudo_log() == [f"dsl41 release service -- prepare v2 {site.base}/sources/v2"]


def test_prepare_seals_the_release_to_the_service_account(site: Site) -> None:
    """DL-412: the service side copies the inputs as its own, every directory 0555 and every
    file 0444, and .release 0755; the deployment identity writes nothing under the release."""
    source = site.source("v2")
    done = site.run("prepare", "v2", str(source))
    assert done.returncode == 0, done.stderr
    release = site.releases / "v2"
    for entry in [release, *release.rglob("*")]:
        info = entry.lstat()
        assert info.st_uid == os.geteuid(), entry
        if entry.name == ".release":
            assert stat.S_IMODE(info.st_mode) == 0o755
        elif ".release" not in entry.relative_to(release).parts:
            assert stat.S_IMODE(info.st_mode) == (0o555 if entry.is_dir() else 0o444), entry
    assert not list(site.releases.glob(".*")) and not (site.releases / "v2.new").exists()
    assert site.sudo_log() == [f"dsl41 release service -- prepare v2 {source}"]


def test_prepare_copies_only_what_the_caller_can_read(site: Site) -> None:
    """DL-412: the copy reads nothing the deployment identity could not read itself, and no
    link; a refused copy leaves nothing behind."""
    source = site.source("v2")
    (source / "global.jil").chmod(0o000)
    refused = site.run("prepare", "v2", str(source))
    assert refused.returncode == 2
    assert "global.jil is not readable by the deployment identity." in refused.stderr
    (source / "global.jil").chmod(0o644)
    (source / "linked.jil").symlink_to(source / "global.jil")
    refused = site.run("prepare", "v2", str(source))
    assert refused.returncode == 2
    assert "the release's inputs hold a symlink: linked.jil." in refused.stderr
    assert not (site.releases / "v2").exists()


def test_a_release_whose_entry_others_could_replace_is_refused(site: Site) -> None:
    """DL-412: a swap needs a writable entry; the verbs refuse a release that has one, and
    `requalify` seals it again."""
    site.prepared("v2")
    release = site.releases / "v2"
    release.chmod(0o755)
    (release / "global.jil").chmod(0o644)
    refused = site.run("apply", "v2")
    assert refused.returncode == 2, refused.stderr
    assert "v2 is not sealed to the service account: ., global.jil can be changed" in (
        refused.stderr
    )
    rid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    refused = site.run("service", "seal", "v2", rid)
    assert refused.returncode == 2 and "is not sealed to the service account" in refused.stderr
    done = site.run("requalify", "v2")
    assert done.returncode == 0, done.stderr
    assert "v2 is sealed to the service account." in done.stderr
    assert not os.access(release / "global.jil", os.W_OK)
    assert site.run("apply", "v2").returncode == 0


def test_prepare_refuses_a_name_that_exists_and_a_source_without_a_manifest(site: Site) -> None:
    """A prepared name is installed once; a name installed by hand points at requalify."""
    site.prepared("v1")
    refused = site.run("prepare", "v1", str(site.source("again")))
    assert refused.returncode == 2
    assert f"{site.releases / 'v1'} is prepared already" in refused.stderr
    refused = site.run("prepare", "r1", str(site.source("again2")))
    assert refused.returncode == 2
    assert f"{site.releases / 'r1'} exists and is not prepared" in refused.stderr
    assert "`dsl41 release requalify r1`" in refused.stderr
    empty = site.base / "empty"
    empty.mkdir()
    refused = site.run("prepare", "v9", str(empty))
    assert refused.returncode == 2 and "holds no estate.toml" in refused.stderr
    for name in ("../x", ".hidden", "a/b", "v9.new"):
        refused = site.run("prepare", name, str(site.source("other" + str(len(name)))))
        assert refused.returncode == 2 and "is not a release name" in refused.stderr
    assert sorted(path.name for path in site.releases.iterdir()) == ["r1", "v1"]


def test_prepare_and_requalify_name_their_missing_argument(site: Site) -> None:
    """DL-412, DL-423: a usage error names the argument it lacks; prepare needs its
    SOURCE_DIR, and requalify takes only NAME."""
    plain = {"NO_COLOR": "1", "TERM": "dumb"}

    def text(done: subprocess.CompletedProcess[str]) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", done.stderr)

    missing = site.run("prepare", "v2", env=plain)
    assert missing.returncode == 2 and "Missing argument 'source_dir'" in text(missing)
    nameless = site.run("requalify", env=plain)
    assert nameless.returncode == 2 and "Missing argument 'name'" in text(nameless)
    extra = site.run("requalify", "v2", "extra", env=plain)
    assert extra.returncode == 2 and "unexpected extra argument" in text(extra)
    nothing = site.run("apply", env=plain)
    assert nothing.returncode == 2 and "Missing argument" in text(nothing)


@pytest.mark.parametrize(
    ("values", "says"),
    [
        ({"MAX_JOBS": "81"}, "it holds 82 jobs, above the site's ceiling of 81 (MAX_JOBS)."),
        (
            {"MAX_GRACE_S": "5"},
            "the runtime's command grace, 10 s, is above the site's MAX_GRACE_S, 5 s.",
        ),
    ],
)
def test_prepare_refuses_a_release_above_the_launchers_ceiling(
    site: Site, values: dict[str, str], says: str
) -> None:
    text = site.launcher.read_text()
    for key, value in values.items():
        text = re.sub(rf"^{key}=\d+$", f"{key}={value}", text, count=1, flags=re.M)
    site.launcher.write_text(text)
    refused = site.run("prepare", "v2", str(site.source("v2")))
    assert refused.returncode == 2, refused.stderr
    assert says in refused.stderr
    assert "nothing is installed under v2, so the name is free again." in refused.stderr
    if "MAX_GRACE_S" in values:
        assert "ask the site package's owner" in refused.stderr
    assert not (site.releases / "v2").exists()


@pytest.mark.parametrize("verb", ["lint", "rehearse"])
def test_prepare_refuses_a_release_lint_or_rehearse_refuses(site: Site, verb: str) -> None:
    (site.stub / f"{verb}.exit").write_text("1")
    refused = site.run("prepare", "v2", str(site.source("v2")))
    assert refused.returncode == 2
    assert f"{verb} exited 1" in refused.stderr
    assert not (site.releases / "v2").exists()
    # a lint that fails runs no rehearse, so a lowering error prints once (DL-412)
    forwarded = (site.stub / "forwarded.log").read_text().splitlines()
    assert [line.split()[0] for line in forwarded].count("rehearse") == (verb == "rehearse")


def test_prepare_refuses_a_release_that_holds_a_symlink(site: Site) -> None:
    """DL-412: prepare's copy holds no symlink, so the service side refuses one in place."""
    raw = site.releases / "raw"
    shutil.copytree(SMALL, raw)
    (raw / "link.jil").symlink_to(raw / "global.jil")
    refused = site.run("requalify", "raw")
    assert refused.returncode == 2 and "raw holds symlinks: link.jil." in refused.stderr
    assert not (raw / ".release" / "prepared.json").exists()


def test_apply_refuses_another_runtime_until_requalify(site: Site) -> None:
    site.prepared("v2")
    release = site.releases / "v2"
    prepared = release / ".release" / "prepared.json"
    prepared.chmod(0o644)
    prepared.write_text(json.dumps(json.loads(prepared.read_text()) | {"dsl41_version": "0.0.1"}))
    refused = site.run("apply", "v2")
    assert refused.returncode == 2 and "was prepared under dsl41 0.0.1" in refused.stderr
    assert "run `dsl41 release requalify v2` to check it again." in refused.stderr
    before = {path: path.read_bytes() for path in release.rglob("*.jil")}
    usage = site.run("requalify", "v2", "extra")
    assert usage.returncode == 2
    (site.stub / "lint.exit").write_text("1")
    refused = site.run("requalify", "v2")
    assert refused.returncode == 2 and "lint exited 1" in refused.stderr
    assert json.loads(prepared.read_text())["dsl41_version"] == "0.0.1"
    (site.stub / "lint.exit").unlink()
    done = site.run("requalify", "v2")
    assert done.returncode == 0, done.stderr
    assert {path: path.read_bytes() for path in release.rglob("*.jil")} == before
    assert not os.access(release / "global.jil", os.W_OK)
    requalified = site.receipt("v2", "prepared")
    assert requalified["dsl41_version"] != "0.0.1"
    assert requalified["source"] == str((site.base / "sources" / "v2").resolve())
    assert site.run("apply", "v2").returncode == 0


def test_requalify_checks_a_release_installed_but_never_prepared_in_place(site: Site) -> None:
    """DL-412: the refusal of an unprepared installed release names requalify, and
    requalify checks it where it is, its inputs made read-only, its files unchanged."""
    raw = site.releases / "raw"
    shutil.copytree(SMALL, raw)
    before = {path.name: path.read_bytes() for path in raw.glob("*.jil")}
    (site.stub / "lint.exit").write_text("1")
    refused = site.run("requalify", "raw")
    assert refused.returncode == 2 and "raw stays unprepared." in refused.stderr
    assert not (raw / ".release" / "prepared.json").exists()
    (site.stub / "lint.exit").unlink()
    done = site.run("requalify", "raw")
    assert done.returncode == 0, done.stderr
    prepared = site.receipt("raw", "prepared")
    assert prepared["source"] is None and set(prepared["files"]) >= set(before)
    assert {path.name: path.read_bytes() for path in raw.glob("*.jil")} == before
    assert not os.access(raw / "global.jil", os.W_OK) and not os.access(raw, os.W_OK)
    assert site.run("apply", "raw").returncode == 0
    assert site.active.read_text() == "raw\n"


def test_the_service_side_takes_only_release_names_a_request_id_and_a_job(site: Site) -> None:
    rid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    for args in (
        ("service", "seal", "v2", "not-an-id"),
        ("service", "seal", "../x", rid),
        ("service", "seal", "r1", rid, "os/dsl41"),
        ("service", "seal", "r1"),
        ("service", "committed"),
        ("service", "pins", "x"),
        ("service", "holds", "../x", rid),
        ("service", "holds", ""),
        ("service", "check", "../x"),
        ("service", "changes", "r1", "../x"),
        ("service", "run", "v2"),
        ("service",),
    ):
        refused = site.run(*args)
        assert refused.returncode == 2, args
    assert site.seals() == []
    shown = site.run("service", "committed", rid)
    assert shown.returncode == 0 and json.loads(shown.stdout) == {"committed": False}
    # a job name takes the engine's rule, `#` included (DL-412)
    held = site.run("service", "holds", "EOD#1")
    assert held.returncode == 0 and json.loads(held.stdout) == {"changes": []}


def test_the_service_side_runs_only_as_the_service_account(site: Site) -> None:
    text = site.launcher.read_text().replace(f"SERVICE_ACCOUNT={ME}\n", "SERVICE_ACCOUNT=nobody\n")
    site.launcher.write_text(text)
    refused = site.run("service", "committed", "0" * 8 + "-0000-0000-0000-" + "0" * 12)
    assert refused.returncode == 2
    assert f"the service side runs as nobody, not as {ME}." in refused.stderr


def test_a_hand_run_service_seal_refuses_an_unprepared_or_changed_release(
    site: Site,
) -> None:
    """The deployment identity can run `service seal` through sudo by hand; it seals
    nothing prepare did not check."""
    rid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    refused = site.run("service", "seal", "r1", rid)
    assert refused.returncode == 2 and "r1 is installed but not prepared" in refused.stderr
    site.prepared("v2")
    jil = site.releases / "v2" / "global.jil"
    (site.releases / "v2").chmod(0o755)
    jil.chmod(0o644)
    jil.write_text(jil.read_text() + "\n")
    refused = site.run("service", "seal", "v2", rid)
    assert refused.returncode == 2 and "v2's files changed since prepare" in refused.stderr
    assert site.seals() == []


def _same_bytes_symlink(site: Site, name: str) -> None:
    """Replace a prepared input with a symlink to identical bytes outside the release: the
    digest check alone would pass it."""
    release = site.releases / name
    outside = site.base / "outside.jil"
    outside.write_bytes((release / "global.jil").read_bytes())
    release.chmod(0o755)
    (release / "global.jil").chmod(0o644)
    (release / "global.jil").unlink()
    (release / "global.jil").symlink_to(outside)


def test_a_service_seal_refuses_a_symlinked_input(site: Site) -> None:
    site.prepared("v2")
    _same_bytes_symlink(site, "v2")
    rid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    refused = site.run("service", "seal", "v2", rid)
    assert refused.returncode == 2 and "v2 holds symlinks: global.jil." in refused.stderr
    assert site.seals() == []


def test_service_changes_refuses_a_symlinked_input(site: Site) -> None:
    site.prepared("v2")
    _same_bytes_symlink(site, "v2")
    refused = site.run("service", "changes", "r1", "v2")
    assert refused.returncode == 2 and "v2 holds symlinks: global.jil." in refused.stderr


def test_an_apply_refuses_a_symlinked_input(site: Site) -> None:
    site.prepared("v2")
    _same_bytes_symlink(site, "v2")
    refused = site.run("apply", "v2")
    assert refused.returncode == 2 and "v2 holds symlinks: global.jil." in refused.stderr
    assert site.state()["sent"] == [] and site.record() is None


def test_service_changes_refuses_a_symlinked_properties_layer(site: Site) -> None:
    """DL-412: a layer linked to a file the service account can read prints nothing of it."""
    evil = site.releases / "evil"
    shutil.copytree(SMALL, evil)
    secret = site.base / "secret.properties"
    secret.write_text("first secret line\nsecond secret line\n")
    (evil / "layer.properties").symlink_to(secret)
    manifest = evil / "estate.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'timezone = "UTC"', 'properties = ["layer.properties"]\ntimezone = "UTC"'
        )
    )
    refused = site.run("service", "changes", "r1", "evil")
    assert refused.returncode == 2 and "evil holds symlinks: layer.properties." in refused.stderr
    assert "secret" not in refused.stderr


def test_a_forged_prepared_json_naming_a_file_outside_the_release_refuses(site: Site) -> None:
    site.prepared("v2")
    prepared = site.releases / "v2" / ".release" / "prepared.json"
    forged = json.loads(prepared.read_text())
    forged["files"]["../outside.jil"] = forged["files"]["global.jil"]
    prepared.chmod(0o644)
    prepared.write_text(json.dumps(forged))
    rid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    refused = site.run("service", "seal", "v2", rid)
    assert refused.returncode == 2
    assert "v2's prepared.json names files outside it: ../outside.jil." in refused.stderr
    assert site.seals() == []


def test_a_release_input_is_read_inside_the_release_without_following_a_link(
    tmp_path: Path,
) -> None:
    """DL-412: `read_input` refuses a link as the file itself (O_NOFOLLOW) and a real path
    outside the release, whatever planted it after the walk."""
    from dsl41.release_service import Stop, read_input

    release, outside = tmp_path / "release", tmp_path / "outside"
    release.mkdir()
    outside.mkdir()
    (outside / "secret").write_text("secret")
    (release / "plain").write_text("plain")
    assert read_input(release, release / "plain") == b"plain"
    (release / "link").symlink_to(outside / "secret")
    (release / "dir").symlink_to(outside)
    for file in (release / "link", release / "dir" / "secret"):
        with pytest.raises(Stop) as refused:
            read_input(release, file)
        assert refused.value.code == 2 and "is outside" in refused.value.lines[0]
    inner = release / "inner"
    inner.symlink_to(release / "plain")
    with pytest.raises(Stop) as refused:
        read_input(release, inner)
    assert "cannot be read as a release input" in refused.value.lines[0]


def test_an_unsearchable_source_path_names_the_account_that_must_read_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-412: a source the service account cannot reach is refused with the cause to act on;
    a source that is not there is refused without it."""
    import errno

    from dsl41.release_service import Stop, open_source

    source = tmp_path / "src"
    source.mkdir()
    who = (os.geteuid(), {os.getegid()})
    real_open = os.open

    def denied(path: object, flags: int, *, dir_fd: int | None = None) -> int:
        if dir_fd is not None and path == "src":
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return real_open(path, flags, dir_fd=dir_fd)  # type: ignore[arg-type]

    with monkeypatch.context() as patch:
        patch.setattr("dsl41.release_service.os.open", denied)
        with pytest.raises(Stop) as refused:
            open_source(source, who)
    assert refused.value.code == 2
    assert refused.value.lines[0] == (
        f"{source} cannot be read as a release: Permission denied"
        "; the service account must be able to read every directory on its path."
    )
    with pytest.raises(Stop) as missing:
        open_source(tmp_path / "absent", who)
    assert missing.value.lines[0] == (
        f"{tmp_path / 'absent'} cannot be read as a release: No such file or directory."
    )


def _receipt(site: Site, name: str) -> subprocess.CompletedProcess[str]:
    rid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    return subprocess.run(
        site.command("service", "--", "receipt", name, "applied", rid),
        input='{"outcome": "applied"}',
        capture_output=True,
        text=True,
        env=site.environ(),
    )


def test_a_receipt_is_refused_for_a_release_others_could_change(site: Site) -> None:
    """DL-412: the service side writes into .release only of a release sealed to it."""
    site.prepared("v2")
    assert _receipt(site, "v2").returncode == 0
    assert (site.releases / "v2" / ".release" / "latest").read_text().startswith("applied-")
    (site.releases / "v2").chmod(0o755)
    refused = _receipt(site, "v2")
    assert refused.returncode == 2 and "v2 is not sealed to the service account" in (refused.stderr)


def test_a_receipt_is_refused_through_a_symlinked_release_directory(site: Site) -> None:
    """DL-412: a .release that is a link reaches nothing: the write goes through the
    directory's own descriptor, opened without following a link."""
    site.prepared("v2")
    release, elsewhere = site.releases / "v2", site.base / "elsewhere"
    elsewhere.mkdir()
    release.chmod(0o755)
    shutil.rmtree(release / ".release")
    (release / ".release").symlink_to(elsewhere)
    release.chmod(0o555)
    refused = _receipt(site, "v2")
    assert refused.returncode == 2, refused.stderr
    assert list(elsewhere.iterdir()) == []


def test_a_missing_grant_names_the_sudoers_file(site: Site) -> None:
    """DL-412: sudo's own refusal is read as a grant to fix, not as a failed check."""
    _script(
        site.base / "path" / "sudo", "#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n"
    )
    refused = site.run("prepare", "v2", str(site.source("v2")))
    assert refused.returncode == 2
    assert "sudo did not run `" in refused.stderr
    assert "sudoers grant is missing or names another path" in refused.stderr
