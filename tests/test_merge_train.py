"""The train runs only in synthetic repositories, with fake services and no network."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/merge_train.py"
REAL_GIT = shutil.which("git")
GIT_ROUTING = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)
JOBS = ["classify", "docs", "test (3.12)", "test (3.13)", "goldens", "coverage", "explore-page"]


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in GIT_ROUTING}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    return env


def call(
    cmd: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in (env or clean_env()).items() if k not in GIT_ROUTING}
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def git(repo: Path, *args: str) -> str:
    assert REAL_GIT
    result = call([REAL_GIT, *args], repo)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def write(repo: Path, name: str, text: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# Every fake delegates git only to a local bare repository. All its children
# receive the same routing-variable scrub as the top-level subprocess.
FAKE = r"""
import json, os, pathlib, signal, subprocess, sys, time
P = pathlib.Path
tool = P(sys.argv[0]).name
args = sys.argv[1:]
cfgpath = P(os.environ['TRAIN_FAKE'])
cfg = json.loads(cfgpath.read_text())
env = {k: v for k, v in os.environ.items() if k not in (
    'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
    'GIT_COMMON_DIR', 'GIT_PREFIX', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')}
def cmd(*args):
    r = subprocess.run([cfg['git'], *args], env=env, capture_output=True, text=True,
                       timeout=10, check=False)
    if r.returncode: print(r.stderr, file=sys.stderr); sys.exit(r.returncode)
    return r.stdout
def save(): cfgpath.write_text(json.dumps(cfg))
with P(cfg['calls']).open('a') as f:
    f.write(json.dumps([tool, *args]) + '\n')
if cfg.get('queue_change') == tool:
    q = P(cfg['records']) / 'queue.json'
    queue = json.loads(q.read_text())
    queue['entries'][0]['body'] = 'bodies/other.md'
    (P(cfg['records']) / 'bodies/other.md').write_text('# Other body\n')
    q.write_text(json.dumps(queue))
    cfg.pop('queue_change'); save()
if cfg.get('fail') and cfg['fail'] in ' '.join([tool, *args]): sys.exit(9)
if cfg.get('hang') == tool:
    child = subprocess.Popen([sys.executable, '-c',
        'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'], env=env)
    P(cfg['ready']).write_text(json.dumps(dict(group=os.getpid(), child=child.pid)))
    time.sleep(60)
if tool == 'notify':
    with P(cfg['notifications']).open('a') as f: f.write(sys.stdin.read())
    sys.exit(9)
if tool == 'git':
    if args[0] in ('fetch', 'push', 'ls-remote'):
        if args[0] == 'push' and cfg.get('change_before_push'):
            cmd('--git-dir', cfg['bare'], 'update-ref', 'refs/heads/slice', cfg['change_before_push'])
        args = [cfg['bare'] if a == 'upstream' else a for a in args]
        if args[0] == 'fetch':
            args = ['fetch', cfg['bare'], '+refs/heads/*:refs/remotes/upstream/*']
    print(cmd(*args), end='')
    if args[0] == 'push':
        head, ref = args[-1].split(':')
        branch = ref.removeprefix('refs/heads/')
        if branch == 'slice': cfg['head'] = head
        for row in cfg.get('prs', []):
            if row['branch'] == branch: row['head'] = head
        save()
    if args[0] == 'push' and cfg.get('lost_push_reply'): sys.exit(9)
    sys.exit(0)
if tool == 'wt':
    blocks = cmd('worktree', 'list', '--porcelain').strip().split('\n\n')
    trees = []
    for i, block in enumerate(blocks):
        d = dict(line.split(' ', 1) for line in block.splitlines() if ' ' in line)
        trees.append(dict(path=d['worktree'], branch=d['branch'].removeprefix('refs/heads/') if 'branch' in d else None,
                          head=d['HEAD'], is_main=i == 0, bare=False,
                          prunable=False, locked=cfg.get('locked', False) and i > 0))
    if args == ['--json', 'list']:
        print(json.dumps(dict(schema=1, ok=True, result=trees)))
    elif args[0] == 'path':
        print(next(t['path'] for t in trees if t['branch'] == args[1]))
    elif args[:2] == ['remove', '--discard-ignored']:
        t = next(t for t in trees if t['branch'] == args[2])
        if cfg.get('dirty_cleanup'):
            (P(t['path']) / 'untracked').write_text('keep')
        print(cmd('worktree', 'remove', t['path']), end='')
    else: sys.exit(90)
    sys.exit(0)
if tool == 'uv':
    if '--classify' in args:
        print(cfg.get('classification', 'code=false' if cfg['docs'] else 'code=true'))
    elif 'scripts/render_decision_index.py' in args:
        index = P('docs/decision-index.md')
        index.write_text(P('docs/decision-log.md').read_text())
        if cfg.get('render_marker'): index.write_text('||||||| base\n')
        if cfg.get('render_edit'): P('docs/decision-log.md').write_text('rewritten\n')
    elif 'tests/test_docs_hygiene.py' in args:
        if cfg.get('hygiene_fail'): sys.exit(1)
    sys.exit(0)
if tool != 'gh': sys.exit(91)
if args[:2] == ['repo', 'view']:
    assert args[2] == 'example.invalid/fixture/project'
else:
    assert args[:2] == ['--repo', 'example.invalid/fixture/project']
    args = args[2:]
if args[:2] == ['repo', 'view']:
    print(json.dumps(dict(nameWithOwner='fixture/project', url='https://example.invalid/fixture/project',
                         defaultBranchRef=dict(name='trunk'), deleteBranchOnMerge=cfg.get('auto_delete', False))))
elif args[:2] == ['pr', 'list']:
    prs = cfg.get('prs', [dict(number=1, branch='slice', head=cfg['head'], state=cfg['state'])]
                  if cfg['pr'] else [])
    if '--head' not in args:
        rows = [dict(number=p['number'], headRefName=p['branch']) for p in prs if p['state'] == 'OPEN']
        if cfg.get('other_pr'): rows.append(dict(number=99, headRefName='dependabot/fixture'))
    else:
        branch = args[args.index('--head') + 1]
        rows = [dict(number=p['number'], headRefOid=p['head'], state=p['state']) for p in prs if p['branch'] == branch]
    print(json.dumps(rows))
elif args[:2] == ['pr', 'view']:
    cfg['views'] = cfg.get('views', 0) + 1
    if cfg.get('advance_on_view') == cfg['views']:
        cmd('--git-dir', cfg['bare'], 'update-ref', 'refs/heads/trunk', cfg['advanced_base'])
    if cfg.get('enqueue_on_view') == cfg['views']:
        save()
        r = subprocess.run([sys.executable, cfg['script'], '--enqueue', cfg['enqueue_file']],
                           env=env, capture_output=True, timeout=10, check=False)
        assert r.returncode == 0, r.stderr
    save()
    number = int(args[2])
    p = next(p for p in cfg.get('prs', [dict(number=1, branch='slice', head=cfg['head'], state=cfg['state'],
                                            merge_commit=cfg.get('merge_commit'))]) if p['number'] == number)
    checks = [dict(__typename='CheckRun', name=n, status='COMPLETED', conclusion='SUCCESS')
              for n in cfg['jobs']]
    if 'checks' in cfg: checks = cfg['checks']
    if cfg.get('check_sequence'):
        checks = cfg['check_sequence'].pop(0); save()
    base = cmd('--git-dir', cfg['bare'], 'rev-parse', 'refs/heads/trunk').strip()
    row = dict(number=number, headRefName=cfg.get('view_branch', p['branch']), baseRefName='trunk', baseRefOid=base,
               headRefOid=p['head'], state=p['state'], isCrossRepository=False,
               isDraft=False, reviewDecision=cfg.get('review', ''), statusCheckRollup=checks,
               mergeCommit=dict(oid=p['merge_commit']) if p.get('merge_commit') else None)
    print(json.dumps(row))
elif args[:2] == ['pr', 'create']:
    branch = args[args.index('--head') + 1]
    if 'prs' in cfg:
        head = cmd('--git-dir', cfg['bare'], 'rev-parse', 'refs/heads/' + branch).strip()
        cfg['prs'].append(dict(number=len(cfg['prs']) + 1, branch=branch, head=head, state='OPEN'))
    cfg['pr'] = True; save(); print('https://example.invalid/fixture/project/pull/1')
elif args[:2] == ['pr', 'merge']:
    assert '--rebase' in args and '--delete-branch' not in args
    number = int(args[2])
    p = next(p for p in cfg.get('prs', [dict(number=1, branch='slice', head=cfg['head'])]) if p['number'] == number)
    head = p['head']
    assert args[args.index('--match-head-commit') + 1] == head
    assert '--expected-base-commit' in args
    base = args[args.index('--expected-base-commit') + 1]
    if cfg.get('advance_at_merge'):
        cmd('--git-dir', cfg['bare'], 'update-ref', 'refs/heads/trunk', cfg['advanced_base'])
    if not cfg.get('false_merge'):
        cmd('--git-dir', cfg['bare'], 'update-ref', 'refs/heads/trunk', head, base)
    cfg['state'] = 'MERGED'; cfg['merge_commit'] = head
    if 'prs' in cfg: p.update(state='MERGED', merge_commit=head)
    save()
    if cfg.get('kill_merge'): os.kill(os.getppid(), signal.SIGKILL)
    if cfg.get('lost_merge_reply'): sys.exit(9)
    if cfg.get('dirty_after_merge'): (P(cfg['slice']) / 'untracked').write_text('keep')
else: sys.exit(92)
"""

# Production merging requires an atomic guard for head and base (rule 46).
# This test-only server simulation has
# an atomic old-base compare-and-swap; it is never a production CLI option.
DRIVER = r"""
import json, os, pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]).parent))
import merge_train
class AtomicServerTrain(merge_train.Train):
    def merge(self, number, entry, raw):
        assert self.state['ci'] == self.state['tested']
        assert self.state['ci']['head'] == self.head(entry)
        self.intent('merge', entry, raw, pr=number)
        self.run('gh', '--repo', self.slug, 'pr', 'merge', str(number), '--rebase',
                 '--match-head-commit', self.head(entry), '--expected-base-commit', self.state['ci']['base'])
merge_train.Train = AtomicServerTrain
original_atomic_json = merge_train.atomic_json
def atomic_json(path, value):
    original_atomic_json(path, value)
    cfgpath = pathlib.Path(os.environ['TRAIN_FAKE'])
    cfg = json.loads(cfgpath.read_text())
    state = pathlib.Path(cfg['records']) / 'state.json'
    if cfg.get('kill_pop') and path.name == 'queue.json' and state.exists():
        cfg['kill_pop'] = False; cfgpath.write_text(json.dumps(cfg))
        os.kill(os.getpid(), 9)
merge_train.atomic_json = atomic_json
sys.argv = [sys.argv[1], *sys.argv[2:]]
raise SystemExit(merge_train.main())
"""


@dataclass
class Fixture:
    root: Path
    slice: Path
    bare: Path
    records: Path
    config: Path
    env: dict[str, str]
    head: str

    def configure(self, **values: object) -> None:
        cfg = json.loads(self.config.read_text())
        cfg.update(values)
        self.config.write_text(json.dumps(cfg))

    def run(
        self,
        *,
        production: bool = False,
        recover: bool = False,
        check_timeout: str = "0.1",
        **env_changes: str | None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        for key, value in env_changes.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return call(
            [
                sys.executable,
                *([] if production else [str(self.config.parent / "driver.py")]),
                str(SCRIPT),
                "--command-timeout",
                "5",
                "--check-timeout",
                check_timeout,
                "--poll-interval",
                "0.01",
                *(["--recover"] if recover else []),
            ],
            self.root,
            env,
        )

    def state(self) -> dict:
        return json.loads((self.records / "state.json").read_text())

    def calls(self) -> list[list[str]]:
        return [
            json.loads(line) for line in (self.config.parent / "calls").read_text().splitlines()
        ]

    def refused(self, reason: str) -> subprocess.CompletedProcess[str]:
        previous = len(self.calls()) if (self.config.parent / "calls").exists() else 0
        result = self.run()
        assert result.returncode != 0, result.stdout
        assert self.state()["reason"] == reason, result.stderr
        assert not any(
            c[:3] == ["gh", "--repo", "example.invalid/fixture/project"]
            and c[3:5] == ["pr", "merge"]
            for c in self.calls()[previous:]
        )
        return result


@pytest.fixture
def train(tmp_path: Path, cli_env: None) -> Fixture:
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "-q", "-b", "trunk")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "merge.conflictStyle", "diff3")
    git(root, "config", "gc.auto", "0")
    git(root, "config", "maintenance.auto", "false")
    write(root, "README.md", "# Fixture\n")
    write(root, "docs/decision-log.md", "# Decisions\n\n- DL-01 Base\n")
    write(root, "docs/decision-index.md", "# Index\n")
    write(root, "src/code.py", "VALUE = 1\n")
    git(
        root,
        "add",
        "--",
        "README.md",
        "docs/decision-log.md",
        "docs/decision-index.md",
        "src/code.py",
    )
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    git(root, "clone", "-q", "--bare", str(root), str(bare))
    git(bare, "config", "gc.auto", "0")
    git(bare, "config", "maintenance.auto", "false")
    git(root, "remote", "add", "upstream", "https://example.invalid/fixture/project.git")
    git(root, "fetch", str(bare), "+refs/heads/*:refs/remotes/upstream/*")
    slice = tmp_path / "slice"
    git(root, "worktree", "add", "-qb", "slice", str(slice))
    write(slice, "README.md", "# Fixture\n\nAccepted.\n")
    git(slice, "add", "--", "README.md")
    git(slice, "commit", "-qm", "candidate")
    head = git(slice, "rev-parse", "HEAD")
    git(root, "push", "-q", str(bare), "slice")
    records = tmp_path / "records"
    (records / "bodies").mkdir(parents=True)
    write(records, "bodies/slice.md", "# Fixture slice\n\nAccepted.\n")
    write(
        records,
        "queue.json",
        json.dumps(
            {
                "entries": [
                    dict(
                        branch="slice",
                        accepted_commit=head,
                        accepted_base=base,
                        body="bodies/slice.md",
                        blocking_ruling=False,
                    )
                ]
            }
        ),
    )
    bin = tmp_path / "bin"
    bin.mkdir()
    for name in ("gh", "wt", "uv", "git", "notify"):
        path = bin / name
        path.write_text(f"#!{sys.executable} -S\n" + FAKE)
        path.chmod(0o755)
    config = tmp_path / "fake.json"
    write(tmp_path, "driver.py", DRIVER)
    config.write_text(
        json.dumps(
            dict(
                git=REAL_GIT,
                bare=str(bare),
                slice=str(slice),
                records=str(records),
                head=head,
                jobs=JOBS,
                pr=True,
                state="OPEN",
                docs=True,
                calls=str(tmp_path / "calls"),
                notifications=str(tmp_path / "notifications"),
            )
        )
    )
    env = clean_env()
    env.update(
        PATH=str(bin) + os.pathsep + env["PATH"],
        TRAIN_FAKE=str(config),
        TRAIN_RECORDS=str(records),
        TRAIN_NOTIFY=str(bin / "notify"),
    )
    return Fixture(root, slice, bare, records, config, env, head)


def test_merge_is_pinned_cleanup_is_scoped_and_notifier_failure_is_ignored(train: Fixture) -> None:
    other = train.root.parent / "other"
    git(train.root, "worktree", "add", "-qb", "other", str(other))
    write(other, "private", "keep\n")
    result = train.run()
    assert result.returncode == 0, result.stderr
    assert train.state()["status"] == "idle"
    assert json.loads((train.records / "queue.json").read_text()) == {"entries": []}
    assert not train.slice.exists() and other.exists()
    assert git(train.root, "rev-parse", "slice") == train.head
    assert git(train.root, "ls-remote", str(train.bare), "refs/heads/slice").startswith(train.head)
    calls = train.calls()
    assert ["wt", "remove", "--discard-ignored", "slice"] in calls
    merge = next(c for c in calls if "merge" in c)
    assert "--rebase" in merge and merge[merge.index("--match-head-commit") + 1] == train.head
    assert not any("pytest" in c and "-n" in c for c in calls)
    assert len((train.config.parent / "notifications").read_text().splitlines()) == 1


@pytest.mark.parametrize("which", ["unset", "relative", "inside", "linked", "symlink"])
def test_records_refusals(train: Fixture, which: str) -> None:
    records: str | None = None
    if which == "relative":
        records = "records"
    if which == "inside":
        records = str(train.root / "records")
    if which == "linked":
        records = str(train.slice / "records")
    if which == "symlink":
        records = str(train.records)
        (train.records / "state.json").symlink_to(train.root / "state.json")
    result = train.run(TRAIN_RECORDS=records)
    assert result.returncode != 0
    assert json.loads(result.stderr)["reason"].startswith("records-")
    assert not (train.root / "state.json").exists()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"accepted_commit": ""}, "acceptance-missing"),
        ({"blocking_ruling": True}, "blocking-ruling"),
        ({"body": "../checkout/README.md"}, "body-path"),
        ({"body": "bodies/missing.md"}, "body-path"),
        ({"branch": "trunk"}, "invalid-branch"),
        ({"accepted_commit": "a" * 40}, "local-head-stale"),
    ],
)
def test_queue_entry_refusals(train: Fixture, change: dict, reason: str) -> None:
    data = json.loads((train.records / "queue.json").read_text())
    data["entries"][0].update(change)
    (train.records / "queue.json").write_text(json.dumps(data))
    train.refused(reason)


def test_missing_queue_and_duplicate_entries_stop(train: Fixture) -> None:
    data = json.loads((train.records / "queue.json").read_text())
    data["entries"] *= 2
    (train.records / "queue.json").write_text(json.dumps(data))
    train.refused("invalid-branch")
    (train.records / "queue.json").unlink()
    train.refused("invalid-data-or-io")


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ({"head": "b" * 40}, "pr-head-stale"),
        ({"review": "CHANGES_REQUESTED"}, "review-not-accepted"),
        ({"review": "REVIEW_REQUIRED"}, "review-not-accepted"),
        ({"view_branch": "other"}, "pr-identity"),
        ({"locked": True}, "worktree-protected"),
        ({"classification": "code=false\ncode=false"}, "classification-invalid"),
        ({"classification": "code=true"}, "classification-invalid"),
        ({"fail": "--classify"}, "command-failed"),
        ({"fail": "pr view"}, "command-failed"),
        ({"fail": "git fetch"}, "command-failed"),
        ({"fail": "--docs"}, "command-failed"),
        ({"fail": "wt --json list"}, "command-failed"),
        ({"auto_delete": True}, "branch-auto-delete"),
    ],
)
def test_command_and_proof_refusals(train: Fixture, config: dict, reason: str) -> None:
    train.configure(**config)
    result = train.run()
    assert result.returncode != 0
    if (train.records / "state.json").exists():
        assert train.state()["reason"] == reason, result.stderr
    else:
        assert json.loads(result.stderr)["reason"] == reason


def checks(**changes: str) -> list[dict]:
    rows = [
        dict(__typename="CheckRun", name=n, status="COMPLETED", conclusion="SUCCESS") for n in JOBS
    ]
    rows[-1].update(changes)
    return rows


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([], "checks-timeout"),
        (checks()[:-1], "checks-timeout"),
        (checks() + checks()[:1], "checks-duplicate"),
        (checks(status="IN_PROGRESS", conclusion=""), "checks-timeout"),
        (checks(conclusion="CANCELLED"), "check-not-green"),
        (checks(conclusion="FAILURE"), "check-not-green"),
        (checks(conclusion="NEUTRAL"), "check-not-green"),
        (checks(conclusion="TIMED_OUT"), "check-not-green"),
        (checks(__typename="StatusContext"), "check-unrecognized"),
        (checks(status="UNKNOWN"), "check-unrecognized"),
        (
            checks()
            + [dict(__typename="CheckRun", name="extra", status="COMPLETED", conclusion="SKIPPED")],
            "check-not-green",
        ),
    ],
)
def test_check_refusals(train: Fixture, rows: list[dict], reason: str) -> None:
    train.configure(checks=rows)
    train.refused(reason)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "CLAUDE.md",
        "docs/guide.md",
        "examples/demo/README.md",
        ".claude/skills/example/SKILL.md",
    ],
)
def test_docs_only_allows_only_explained_skips(train: Fixture, path: str) -> None:
    write(train.slice, path, "# Accepted\n")
    git(train.slice, "add", "--", path)
    git(train.slice, "commit", "-qm", "docs")
    accept(train)
    train.configure(checks=checks(conclusion="SKIPPED"))
    result = train.run()
    assert result.returncode == 0, result.stderr


def accept(train: Fixture) -> None:
    train.head = git(train.slice, "rev-parse", "HEAD")
    data = json.loads((train.records / "queue.json").read_text())
    data["entries"][0]["accepted_commit"] = train.head
    (train.records / "queue.json").write_text(json.dumps(data))
    train.configure(head=train.head)
    git(train.root, "push", "-q", str(train.bare), "slice")


@pytest.mark.parametrize(
    "path", ["AGENTS.md", ".agents/SKILL.md", ".claude/settings.json", "src/code.py"]
)
def test_code_cannot_explain_a_skip(train: Fixture, path: str) -> None:
    write(train.slice, path, "changed\n")
    git(train.slice, "add", "--", path)
    git(train.slice, "commit", "-qm", "code")
    accept(train)
    train.configure(docs=False, checks=checks(conclusion="SKIPPED"))
    train.refused("check-not-green")
    assert any(c[:3] == ["uv", "run", "pytest"] and "-n" in c for c in train.calls())


@pytest.mark.parametrize("name", ["classify", "docs"])
def test_docs_gate_itself_cannot_skip(train: Fixture, name: str) -> None:
    rows = checks()
    next(c for c in rows if c["name"] == name)["conclusion"] = "SKIPPED"
    train.configure(checks=rows)
    train.refused("check-not-green")


def conflict(train: Fixture, midfile: bool = False, other: bool = False) -> None:
    if other:
        write(train.slice, "README.md", "# Candidate\n")
        write(train.root, "README.md", "# Upstream\n")
    else:
        base = (train.slice / "docs/decision-log.md").read_text()
        write(
            train.slice,
            "docs/decision-log.md",
            base.replace("Base", "Edited") + "\n- DL-03 Slice\n"
            if midfile
            else base + "\n- DL-03 Slice\n",
        )
        write(train.root, "docs/decision-log.md", base + "\n- DL-02 Upstream\n")
        write(train.slice, "docs/decision-index.md", "# Slice index\n")
        write(train.root, "docs/decision-index.md", "# Upstream index\n")
    names = ["README.md"] if other else ["docs/decision-log.md", "docs/decision-index.md"]
    git(train.slice, "add", "--", *names)
    git(train.slice, "commit", "-qm", "tail")
    accept(train)
    git(train.root, "add", "--", *names)
    git(train.root, "commit", "-qm", "upstream")
    git(train.root, "push", "-q", str(train.bare), "trunk")


def test_tail_append_index_render_and_checked_staging(train: Fixture) -> None:
    conflict(train)
    train.refused("integration-review-required")
    assert (train.slice / "docs/decision-log.md").read_bytes() == (
        b"# Decisions\n\n- DL-01 Base\n\n- DL-02 Upstream\n\n- DL-03 Slice\n"
    )
    assert (train.slice / "docs/decision-index.md").read_bytes() == (
        train.slice / "docs/decision-log.md"
    ).read_bytes()
    calls = train.calls()
    diff = calls.index(["git", "diff", "--check"])
    hygiene = calls.index(["uv", "run", "pytest", "-q", "tests/test_docs_hygiene.py"])
    stage = calls.index(["git", "add", "--", "docs/decision-log.md", "docs/decision-index.md"])
    cached = calls.index(["git", "diff", "--cached", "--check"])
    cont = calls.index(["git", "rebase", "--continue"])
    assert diff < hygiene < stage < cached < cont


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("midfile", "log-not-tail-append"),
        ("other", "rebase-conflict"),
        ("marker", "conflict-marker"),
        ("hygiene", "command-failed"),
        ("stage", "command-failed"),
        ("cached", "command-failed"),
        ("render", "command-failed"),
        ("render_edit", "log-render-changed"),
    ],
)
def test_rebase_refusals(train: Fixture, kind: str, reason: str) -> None:
    conflict(train, midfile=kind == "midfile", other=kind == "other")
    if kind == "marker":
        train.configure(render_marker=True)
    if kind == "hygiene":
        train.configure(hygiene_fail=True)
    if kind == "stage":
        train.configure(fail="git add")
    if kind == "cached":
        train.configure(fail="git diff --cached --check")
    if kind == "render":
        train.configure(fail="scripts/render_decision_index.py")
    if kind == "render_edit":
        train.configure(render_edit=True)
    train.refused(reason)
    calls = train.calls()
    assert ["git", "rebase", "--continue"] not in calls
    if kind in {"marker", "hygiene", "render", "render_edit"}:
        assert not any(c[:2] == ["git", "add"] for c in calls)
    if kind == "midfile":
        assert git(train.slice, "rev-parse", "HEAD") == train.head
        assert not git(train.slice, "diff", "--name-only", "--diff-filter=U")


@pytest.mark.parametrize("marker", ["<<<<<<< HEAD", "=======", ">>>>>>> slice", "||||||| base"])
def test_all_conflict_markers_stop_even_without_rebase(train: Fixture, marker: str) -> None:
    write(train.slice, "docs/bad.md", marker + "\n")
    git(train.slice, "add", "--", "docs/bad.md")
    git(train.slice, "commit", "-qm", "bad marker")
    accept(train)
    train.refused("conflict-marker")


def test_queue_is_not_rewritten_when_another_writer_changes_it(train: Fixture) -> None:
    original = (train.records / "queue.json").read_bytes()
    train.configure(queue_change="uv")
    train.refused("queue-changed")
    assert (train.records / "queue.json").read_bytes() != original


def test_records_are_bound_to_repository(train: Fixture) -> None:
    train.configure(fail="--classify")
    train.refused("command-failed")
    (train.records / "owner.json").write_text("{}")
    train.refused("records-owner")


def test_concurrent_start_is_refused_across_records_directories(train: Fixture) -> None:
    import fcntl

    for path in (train.records / "train.lock", train.root / ".git/merge-train.lock"):
        with path.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = train.run()
            assert result.returncode != 0
            assert json.loads(result.stderr)["reason"] == "already-running"
        assert train.slice.exists()
    assert not (train.records / "state.json").exists()
    refusals = list(train.records.glob("state-*.json"))
    assert len(refusals) == 2
    assert all(json.loads(path.read_text())["reason"] == "already-running" for path in refusals)


@pytest.mark.parametrize("kill", [True, False])
def test_interruption_after_merge_reconciles_without_second_merge(
    train: Fixture, kill: bool
) -> None:
    train.configure(kill_merge=kill, lost_merge_reply=not kill)
    result = train.run()
    assert result.returncode != 0
    assert train.state()["intent"]["kind"] == "merge"
    assert len(json.loads((train.records / "queue.json").read_text())["entries"]) == 1
    train.configure(kill_merge=False, lost_merge_reply=False)
    result = train.run()
    assert result.returncode == 0, result.stderr
    assert len([c for c in train.calls() if "merge" in c]) == 1
    assert not train.slice.exists()


def test_unproven_merge_is_never_retried(train: Fixture) -> None:
    train.configure(fail="pr merge")
    assert train.run().returncode != 0
    train.configure(fail=None)
    train.refused("recovery-required")
    assert len([c for c in train.calls() if "merge" in c]) == 1


def test_queue_ownership_survives_restart(train: Fixture) -> None:
    train.configure(lost_merge_reply=True)
    assert train.run().returncode != 0
    queue = json.loads((train.records / "queue.json").read_text())
    queue["entries"][0]["blocking_ruling"] = True
    (train.records / "queue.json").write_text(json.dumps(queue))
    result = train.run()
    assert result.returncode != 0
    assert train.state()["reason"] == "blocking-ruling"
    assert train.slice.exists()


def test_cleanup_refuses_dirty_candidate_after_merge(train: Fixture) -> None:
    train.configure(dirty_after_merge=True)
    result = train.run()
    assert result.returncode != 0
    assert train.state()["reason"] == "worktree-dirty"
    assert train.slice.exists()
    assert not any(c[:2] == ["wt", "remove"] for c in train.calls())


def test_publish_uses_external_body_and_derived_identity(train: Fixture) -> None:
    train.configure(pr=False)
    git(train.root, "--git-dir", str(train.bare), "update-ref", "-d", "refs/heads/slice")
    result = train.run()
    assert result.returncode == 0, result.stderr
    calls = train.calls()
    create = next(c for c in calls if "create" in c)
    assert create[1:3] == ["--repo", "example.invalid/fixture/project"]
    assert create[-1] == str(train.records / "bodies/slice.md")
    push = next(c for c in calls if c[:2] == ["git", "push"])
    assert push == [
        "git",
        "push",
        "--force-with-lease=refs/heads/slice:",
        "--force-if-includes",
        "upstream",
        f"{train.head}:refs/heads/slice",
    ]


def test_uncertain_push_requires_remote_proof(train: Fixture) -> None:
    train.configure(pr=False, fail="git push")
    git(train.root, "--git-dir", str(train.bare), "update-ref", "-d", "refs/heads/slice")
    assert train.run().returncode != 0
    train.configure(fail=None)
    train.refused("recovery-required")
    assert len([c for c in train.calls() if c[:2] == ["git", "push"]]) == 1


def test_empty_queue_is_idle_and_notifies_no_one(train: Fixture) -> None:
    (train.records / "queue.json").write_text('{"entries": []}')
    assert train.run().returncode == 0
    assert train.state()["status"] == "idle"
    assert not (train.config.parent / "notifications").exists()


def test_unconsumed_entries_are_preserved(train: Fixture) -> None:
    data = json.loads((train.records / "queue.json").read_text())
    data["entries"].append({**data["entries"][0], "branch": "later"})
    data["note"] = "queue owner metadata"
    (train.records / "queue.json").write_text(json.dumps(data))
    result = train.run()
    assert result.returncode != 0
    assert train.state()["reason"] == "worktree-missing"
    assert json.loads((train.records / "queue.json").read_text()) == {
        **data,
        "entries": data["entries"][1:],
    }


def test_journal_contains_queue_prefix(train: Fixture) -> None:
    raw = (train.records / "queue.json").read_bytes()
    train.configure(fail="pr merge")
    assert train.run().returncode != 0
    assert train.state()["intent"]["queue"] == json.loads(raw)


def test_merge_reply_without_remote_ancestry_cannot_authorize_cleanup(train: Fixture) -> None:
    train.configure(false_merge=True)
    result = train.run()
    assert result.returncode != 0
    assert train.state()["reason"] == "command-failed"
    assert train.state()["intent"]["kind"] == "merge"
    assert train.slice.exists()
    assert not any(c[:2] == ["wt", "remove"] for c in train.calls())


def test_uncertain_create_is_not_retried(train: Fixture) -> None:
    train.configure(pr=False, fail="pr create")
    assert train.run().returncode != 0
    train.configure(fail=None)
    train.refused("recovery-required")
    assert len([c for c in train.calls() if "create" in c]) == 1


def test_uncertain_push_with_remote_proof_can_resume(train: Fixture) -> None:
    train.configure(pr=False, lost_push_reply=True)
    git(train.root, "--git-dir", str(train.bare), "update-ref", "-d", "refs/heads/slice")
    assert train.run().returncode != 0
    train.configure(lost_push_reply=False)
    result = train.run()
    assert result.returncode == 0, result.stderr
    assert len([c for c in train.calls() if c[:2] == ["git", "push"]]) == 1


@pytest.mark.parametrize("failure", ["before", "during"])
def test_worktree_dirt_is_retained(train: Fixture, failure: str) -> None:
    if failure == "before":
        write(train.slice, "untracked", "keep\n")
        train.refused("worktree-dirty")
    else:
        train.configure(dirty_cleanup=True)
        result = train.run()
        assert result.returncode != 0
        assert train.state()["reason"] == "command-failed"
    assert (train.slice / "untracked").read_text() in {"keep", "keep\n"}


def test_markdown_symlink_does_not_stop_a_clean_candidate(train: Fixture) -> None:
    (train.slice / "AGENTS.md").symlink_to("CLAUDE.md")
    write(train.slice, "CLAUDE.md", "# Instructions\n")
    git(train.slice, "add", "--", "AGENTS.md", "CLAUDE.md")
    git(train.slice, "commit", "-qm", "instructions")
    accept(train)
    train.configure(docs=False)
    result = train.run()
    assert result.returncode == 0, result.stderr
