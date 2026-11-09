"""Synthetic command attempts with optional ledger or bounded filesystem effects."""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import sys

if __package__:
    from . import workflow_files
else:
    import workflow_files


OUTCOMES = {"success", "fail_before_effect", "fail_after_effect"}
IDENTITY = ("DSL41_JOB", "DSL41_RUN_NUMBER", "DSL41_ATTEMPT", "DSL41_SCHEDULED_AT")
SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
    id INTEGER PRIMARY KEY,
    scenario TEXT NOT NULL,
    batch TEXT NOT NULL,
    job TEXT NOT NULL,
    effect_key TEXT,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    business_date TEXT,
    payload TEXT NOT NULL,
    UNIQUE (scenario, batch, job, effect_key)
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    scenario TEXT NOT NULL,
    batch TEXT NOT NULL,
    job TEXT NOT NULL,
    identity TEXT NOT NULL,
    outcome TEXT NOT NULL,
    effect_id INTEGER REFERENCES effects(id),
    effect_created INTEGER NOT NULL
);
"""


def object_fields(value: object, allowed: set[str], label: str) -> dict:
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ValueError(f"{label}: expected object with fields {sorted(allowed)}")
    return value


def string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{label}: expected nonempty string without NUL")
    return value


def reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_config(path: Path | None) -> dict:
    config = (
        {}
        if path is None
        else json.loads(
            path.read_text(), parse_constant=reject_constant, object_pairs_hook=unique_object
        )
    )
    config = object_fields(config, {"scenario", "batch", "jobs"}, "config")
    for field in ("scenario", "batch"):
        if field in config:
            string(config[field], field)
    jobs = config.get("jobs", {})
    if not isinstance(jobs, dict):
        raise ValueError("jobs: expected object")
    for job, raw in jobs.items():
        string(job, "job")
        spec = object_fields(raw, {"outcome", "effect", "effect_key", "file_action"}, f"job {job}")
        outcome = spec.get("outcome", "success")
        if not isinstance(outcome, str) or outcome not in OUTCOMES:
            raise ValueError(f"job {job}: unknown outcome")
        if outcome == "fail_after_effect" and not ({"effect", "file_action"} & spec.keys()):
            raise ValueError("fail_after_effect requires an effect")
        if "file_action" in spec:
            if "effect" in spec or "effect_key" in spec:
                raise ValueError("file_action and ledger effects cannot be combined")
            spec["file_action"] = workflow_files.validate_action(spec["file_action"])
        if "effect_key" in spec:
            string(spec["effect_key"], "effect_key")
            if "effect" not in spec:
                raise ValueError("effect_key requires an effect")
        if "effect" in spec:
            effect = object_fields(
                spec["effect"], {"kind", "target", "business_date", "payload"}, "effect"
            )
            for field in ("kind", "target"):
                string(effect.get(field), field)
            if "business_date" in effect:
                string(effect["business_date"], "business_date")
            json.dumps(effect.get("payload"), allow_nan=False)
    return config


def record(ledger: Path, job: str, scenario: str, batch: str, spec: dict) -> dict:
    outcome = spec.get("outcome", "success")
    identity = {key: os.environ[key] for key in IDENTITY if key in os.environ}
    connection = sqlite3.connect(ledger, timeout=5)
    try:
        workflow_files.initialize_wal(connection)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        effect_id = None
        created = False
        if "effect" in spec and outcome != "fail_before_effect":
            effect = spec["effect"]
            values = (
                effect["kind"],
                effect["target"],
                effect.get("business_date"),
                json.dumps(effect.get("payload"), sort_keys=True, allow_nan=False),
            )
            key = spec.get("effect_key")
            previous = None
            if key is not None:
                previous = connection.execute(
                    "SELECT id, kind, target, business_date, payload FROM effects "
                    "WHERE scenario=? AND batch=? AND job=? AND effect_key=?",
                    (scenario, batch, job, key),
                ).fetchone()
            if previous is not None:
                if previous[1:] != values:
                    outcome = "refused"
                else:
                    effect_id = previous[0]
            else:
                cursor = connection.execute(
                    "INSERT INTO effects "
                    "(scenario, batch, job, effect_key, kind, target, business_date, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (scenario, batch, job, key, *values),
                )
                effect_id = cursor.lastrowid
                created = True
        cursor = connection.execute(
            "INSERT INTO attempts "
            "(recorded_at, scenario, batch, job, identity, outcome, effect_id, effect_created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(),
                scenario,
                batch,
                job,
                json.dumps(identity, sort_keys=True),
                outcome,
                effect_id,
                created,
            ),
        )
        receipt = {
            "version": 1,
            "attempt_id": cursor.lastrowid,
            "job": job,
            "scenario": scenario,
            "batch": batch,
            "outcome": outcome,
            "effect_id": effect_id,
            "effect_created": created,
        }
        connection.commit()
        return receipt
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record a synthetic command attempt in a local SQLite ledger. "
        "The default commits execution evidence and exits 0. Optional effects are ledger rows "
        "or bounded file actions; no scheduler status is written or inferred.",
        epilog='Config: {"scenario":"trial","batch":"day1","jobs":{"job_a":'
        '{"outcome":"fail_after_effect","effect_key":"day1-output","effect":'
        '{"kind":"report","target":"daily","business_date":"2026-01-01",'
        '"payload":{"rows":3}}}}}. Outcomes: success (exit 0), '
        "fail_before_effect (exit 1), fail_after_effect (exit 1). "
        "Omit effect_key for a separate effect on every attempt. Keys deduplicate within "
        "scenario, batch and job only; a changed effect under the same key is an error. "
        "Conflicting keyed effects commit a refused attempt without an effect reference. "
        "fail_after_effect requires a configured effect. "
        "Driver/config/database errors exit 2. SQLite lock waits are bounded to 5 seconds. "
        "file_action replaces effect: operation move/archive (source,destination), "
        "remove (paths), delay (seconds, 0..3600). archive/remove accept missing_ok. "
        "Archive destinations must be unique per intended cycle; the caller chooses them. "
        "Require an absolute, precreated, owned private --file-root; directories must exist. "
        "Ordinary file actions re-execute. file_action.key requires --namespace and a logical "
        "run (DSL41_RUN_NUMBER or --logical-run for manual calls). A namespace belongs to "
        "exactly one engine run root and one ledger. Fresh engine roots need fresh "
        "namespaces. Keyed safety covers replacement attempts of the same logical run "
        "only; FORCE_STARTJOB starts new physical work. Root recreation or ledger deletion "
        "invalidates evidence. No external writers may change selected paths. "
        "Do not watch reserved .workflow-stage or .workflow.lock paths. "
        "File precondition refusals, busy, uncertain keyed receipts and evidence errors "
        "exit 2; partial I/O and injected fail-before/fail-after outcomes exit 1; success "
        "exits 0. Receipt cause fields distinguish these results. No automatic retries.",
    )
    parser.add_argument(
        "--db", "--ledger", dest="ledger", type=Path, required=True, help="shared local SQLite path"
    )
    parser.add_argument(
        "--job", help="job name; defaults to DSL41_JOB and must agree if both exist"
    )
    parser.add_argument("--config", type=Path, help="optional JSON scenario with per-job effects")
    parser.add_argument("--scenario", help="override config scenario (default: default)")
    parser.add_argument("--batch", help="override config batch (default: default)")
    parser.add_argument("--file-root", type=Path, help="private precreated file-action root")
    parser.add_argument("--namespace", help="one engine-root and ledger identity for keyed actions")
    parser.add_argument(
        "--logical-run", help="manual logical run; must agree with scheduler identity"
    )
    args = parser.parse_args()
    try:
        env_job = os.environ.get("DSL41_JOB")
        if args.job is not None and env_job is not None and args.job != env_job:
            raise ValueError("--job disagrees with DSL41_JOB")
        job = string(args.job if args.job is not None else env_job, "job")
        config = load_config(args.config)
        scenario = string(
            args.scenario if args.scenario is not None else config.get("scenario", "default"),
            "scenario",
        )
        batch = string(
            args.batch if args.batch is not None else config.get("batch", "default"), "batch"
        )
        spec = config.get("jobs", {}).get(job, {})
        if "file_action" in spec:
            if args.file_root is None:
                raise ValueError("file_action requires --file-root")
            for name in ("namespace", "logical_run"):
                if getattr(args, name) is not None:
                    string(getattr(args, name), name)
            receipt = workflow_files.execute(
                workflow_files.FileRoot(args.file_root, args.ledger),
                spec["file_action"],
                schema=SCHEMA,
                job=job,
                scenario=scenario,
                batch=batch,
                namespace=args.namespace,
                logical_run=args.logical_run,
                outcome=spec.get("outcome", "success"),
                identity={key: os.environ[key] for key in IDENTITY if key in os.environ},
            )
            print(json.dumps(receipt, sort_keys=True))
            return workflow_files.exit_code(receipt)
        receipt = record(args.ledger, job, scenario, batch, spec)
        if receipt["outcome"] == "refused":
            print(
                json.dumps(
                    {
                        "version": 1,
                        "attempt_id": receipt["attempt_id"],
                        "error": "effect_key already records a different effect",
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        print(json.dumps(receipt, sort_keys=True))
        return 0 if receipt["outcome"] == "success" else 1
    except Exception as error:
        failure = {"version": 1, "error": str(error)}
        if isinstance(error, workflow_files.EvidenceError):
            failure.update(
                invocation_id=error.invocation,
                cause="evidence_error",
                uncommitted_cause=error.cause,
            )
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
