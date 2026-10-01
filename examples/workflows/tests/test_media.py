"""Media: FFmpeg renditions published as immutable releases over HTTP."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from lane import Lane, Result, read_json, scheduler_rows, scheduler_run

TIMEOUT = 180
JOBS = [
    "MEDIA_HIGH",
    "MEDIA_INPUT",
    "MEDIA_LOW",
    "MEDIA_POSTER",
    "MEDIA_PUBLISH",
    "MEDIA_STAGE",
    "MEDIA_VERIFY",
]
ACTIONS = ["high", "input", "low", "poster", "publish", "stage", "verify"]

# README step 4: serve only the public directory, then run the HTTP checker
# against it inside the same runner container.
RECHECK = """
set -u
R=$1 N=$2 OUT=$3
python -m http.server 8080 --bind 127.0.0.1 --directory "$R/business/public" \
    >"$OUT-http.log" 2>&1 &
server=$!
tries=0
until python -c 'import urllib.request as u; u.urlopen(
    "http://127.0.0.1:8080/current.json", timeout=1)' 2>/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -ge 50 ]; then kill "$server"; echo "HTTP server did not start" >&2; exit 98; fi
    sleep 0.2
done
python examples/media/check.py --url http://127.0.0.1:8080 --revision "$N" \
    --source "$R/business/inputs/clip-r$N.mp4" --evidence "$OUT"
status=$?
kill "$server"
exit "$status"
"""


def recheck(lane: Lane, run_dir: str, revision: int, name: str) -> Result:
    out = f"{run_dir}/{name}"
    return lane.exec("sh", "-c", RECHECK, "sh", run_dir, str(revision), out, timeout=180)


def attempt_actions(wave: Path) -> Counter[str]:
    """One file per real worker entry, named after the worker's action."""
    return Counter(path.name.split("-", 1)[0] for path in (wave / "attempts").glob("*.json"))


def test_media_happy_publication(lane: Lane) -> None:
    result = lane.run("media", "--scenario", "happy", timeout=TIMEOUT).require()
    run_dir = result.run_dir()
    run = lane.collected(run_dir)

    lines = result.stdout.splitlines()
    assert "revision 1: three served artifacts validated" in lines
    assert "revision 2: three served artifacts validated" not in lines
    assert lines[-1] == f"media happy: passed; evidence retained at {run_dir}"
    outcome = read_json(run / "result.json")
    assert (outcome["scenario"], outcome["status"]) == ("happy", "passed")
    assert outcome["current"]["revision"] == 1
    assert not (run / "r2").exists()

    rows = scheduler_rows(run / "r1" / "runs.json")
    assert rows == [(job, 1, "SUCCESS", 0) for job in JOBS]

    assert attempt_actions(run / "r1") == dict.fromkeys(ACTIONS, 1)

    again = recheck(lane, run_dir, 1, "lane-recheck").require()
    assert again.stdout.strip() == "revision 1: three served artifacts validated"


def test_media_failed_rendition_incident(lane: Lane) -> None:
    result = lane.run("media", timeout=TIMEOUT).require()
    run_dir = result.run_dir()
    run = lane.collected(run_dir)

    lines = result.stdout.splitlines()
    assert "revision 1: three served artifacts validated" in lines
    assert "revision 2: three served artifacts validated" in lines
    assert lines[-1] == f"media failed-rendition: passed; evidence retained at {run_dir}"
    outcome = read_json(run / "result.json")
    assert (outcome["scenario"], outcome["status"]) == ("failed-rendition", "passed")
    assert outcome["current"]["revision"] == 2
    assert read_json(run / "business" / "faults" / "r2-high.fired")["action"] == "high"
    stale = read_json(run / "stale-publish.json")
    assert stale["exit"] != 0 and "stale publication" in stale["stderr"]

    rows = scheduler_rows(run / "r2" / "runs.json")
    assert [row for row in rows if row[0] == "MEDIA_HIGH"] == [
        ("MEDIA_HIGH", 1, "FAILURE", 23),
        ("MEDIA_HIGH", 2, "SUCCESS", 0),
    ]
    assert [row for row in rows if row[0] != "MEDIA_HIGH"] == [
        (job, 1, "SUCCESS", 0) for job in JOBS if job != "MEDIA_HIGH"
    ]

    expected = {**dict.fromkeys(ACTIONS, 1), "high": 2}
    assert attempt_actions(run / "r2") == expected
    high = [read_json(path) for path in (run / "r2" / "attempts").glob("high-*.json")]
    assert sorted(scheduler_run(row["scheduler_run"])["run_number"] for row in high) == [1, 2]

    again = recheck(lane, run_dir, 2, "lane-recheck").require()
    assert again.stdout.strip() == "revision 2: three served artifacts validated"


def test_media_checker_rejects_damaged_served_file(lane: Lane) -> None:
    run_dir = lane.run("media", "--scenario", "happy", timeout=TIMEOUT).require().run_dir()
    recheck(lane, run_dir, 1, "lane-recheck").require()

    served = f"{run_dir}/business/public/releases/r1/low.mp4"
    lane.exec("sh", "-c", 'printf x >>"$1"', "sh", served).require()

    refused = recheck(lane, run_dir, 1, "lane-damaged-check")
    assert refused.exit not in (0, None), refused.describe()
    assert "served file differs from manifest: low.mp4" in refused.stderr, refused.describe()


# Serve revision 1's low rendition as revision 2's, with the manifest entry and
# the pointer hash rewritten to match: every byte count and hash agrees.
OLDER_RENDITION = """
import hashlib, json, os, shutil, sys
from pathlib import Path

public = Path(sys.argv[1]) / "business" / "public"
r1, r2 = public / "releases" / "r1", public / "releases" / "r2"
manifest_path, pointer_path = r2 / "manifest.json", public / "current.json"
os.chmod(r2, 0o755)
for path in (r2 / "low.mp4", manifest_path):
    os.chmod(path, 0o644)
shutil.copyfile(r1 / "low.mp4", r2 / "low.mp4")
manifest = json.loads(manifest_path.read_text())
older = json.loads((r1 / "manifest.json").read_text())["files"]["low.mp4"]
manifest["files"]["low.mp4"] = older
manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\\n")
pointer = json.loads(pointer_path.read_text())
pointer["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
pointer_path.write_text(json.dumps(pointer, indent=2, sort_keys=True) + "\\n")
print(older["sha256"])
"""


def test_media_checker_rejects_older_rendition_with_recomputed_hashes(lane: Lane) -> None:
    run_dir = lane.run("media", timeout=TIMEOUT).require().run_dir()
    recheck(lane, run_dir, 2, "lane-recheck").require()

    swapped = lane.exec("python", "-c", OLDER_RENDITION, run_dir).require()
    assert re.fullmatch(r"[0-9a-f]{64}", swapped.stdout.strip()), swapped.describe()

    refused = recheck(lane, run_dir, 2, "lane-older-check")
    assert refused.exit not in (0, None), refused.describe()
    assert "fixture audio tone mismatch: low.mp4" in refused.stderr, refused.describe()
    assert "expected 880.00 Hz" in refused.stderr, refused.describe()
