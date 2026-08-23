"""The access perimeter (docs/access-model.md, DL-146).

Three tiers at the perimeter; the engine, oracle and journal stay
authz-free. This module owns the pieces the gate is made of: the tier
enum, the principal, the role-map loader (access-model ss4), the closed
verb table (ss10), the perimeter journal (ss6) and the swappable policy
holder (ss7). `runner_control.ControlServer` calls it; nothing below the
control plane imports it.

Everything here fails closed: a configured map that is missing or
invalid refuses at load, an unmapped principal is denied, a `cmd`
outside the closed table is denied (the verb inside a listed cmd is
the dispatcher's to validate, ss10), a peer without a resolvable
credential is refused at accept (ss3).
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import socket as socket_mod
import stat as stat_lib
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any

from dsl41.runner_procid import fsync_dir
from dsl41.runner_supervisor import peer_uid

__all__ = [
    "AccessControl",
    "AccessError",
    "Principal",
    "Tier",
    "load_policy",
    "peer_principal",
    "required_tier",
]


class AccessError(Exception):
    """A role map that cannot be loaded, or a perimeter that cannot be
    armed. Startup answers it with a refusal (access-model ss4: a
    configured path never falls back to owner-wide authority)."""


class Tier(IntEnum):
    """read < ops < adm, strictly nested (access-model ss1). IntEnum so
    `granted >= required` IS the authorization check."""

    READ = 0
    OPS = 1
    ADM = 2


_TIER_BY_NAME = {"read": Tier.READ, "ops": Tier.OPS, "adm": Tier.ADM}


@dataclass(frozen=True)
class Principal:
    """(realm, name, groups) — fixed at connection accept, immutable for
    the connection's life (ss3). The realm keeps a web `root` from ever
    matching the OS `root` (ss1)."""

    realm: str
    name: str
    groups: frozenset[str]

    @property
    def spelling(self) -> str:
        """The canonical actor spelling that replaces `claimed_actor` in
        journaled records when access is configured (ss3)."""
        return f"{self.realm}/{self.name}"


@dataclass(frozen=True)
class Policy:
    """One immutable role-map snapshot (ss7). A request decides under
    exactly one of these; reload swaps the holder, never the snapshot."""

    bindings: dict[str, Tier]
    unmapped: Tier | None  # None = deny
    socket_group: str | None
    generation: int
    digest: str

    def resolve(self, principal: Principal) -> Tier | None:
        """access-model ss4 resolution order: exact user row, else the
        highest matching group row, else `unmapped`. None = denied."""
        user_row = self.bindings.get(f"user:{principal.realm}/{principal.name}")
        if user_row is not None:
            return user_row
        group_rows = [
            tier
            for group in principal.groups
            if (tier := self.bindings.get(f"group:{principal.realm}/{group}")) is not None
        ]
        if group_rows:
            return max(group_rows)
        return self.unmapped


def _subject_valid(subject: str) -> bool:
    kind, sep, rest = subject.partition(":")
    if not sep or kind not in ("user", "group"):
        return False
    realm, sep, name = rest.partition("/")
    return bool(sep) and bool(realm) and bool(name) and "*" not in subject


#: ss4/DL-149: a role map is tiny; past this the loader refuses rather
#: than reading on (the reload handler runs on the event loop)
_MAP_BYTES_CEILING = 1 << 20


def load_policy(path: Path, *, generation: int) -> Policy:
    """Load and validate the strict-TOML role map (access-model ss4).

    Refuses: a missing/unreadable file, a symlink, a group- or
    other-writable file or one owned by someone else, a descriptor I/O
    failure mid-read, a file over the 1 MiB ceiling, an unknown key, an
    unknown tier, a duplicate subject, a wildcard, a subject without a
    realm. Refusal is `AccessError` — the caller turns it into a startup
    refusal or a kept-old-policy reload receipt (ss7)."""
    try:
        parent = os.lstat(path.parent)
    except OSError as exc:
        raise AccessError(f"access map {path}: cannot stat parent: {exc}") from exc
    if stat_lib.S_ISLNK(parent.st_mode):
        # O_NOFOLLOW guards only the FINAL component; a symlinked parent
        # would let a rename redirect the whole path between check and open
        raise AccessError(f"access map {path}: parent directory is a symlink")
    if parent.st_uid not in (os.geteuid(), 0):
        raise AccessError(
            f"access map {path}: parent directory owned by uid {parent.st_uid},"
            " not this process or root"
        )
    if parent.st_mode & 0o022:
        raise AccessError(
            f"access map {path}: parent directory mode"
            f" {oct(parent.st_mode & 0o777)} is group- or other-writable --"
            " an ops user could swap the map by renaming over it"
        )
    try:
        # O_NONBLOCK: a FIFO at this path must refuse, not park startup
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise AccessError(f"access map {path}: cannot open: {exc}") from exc
    try:
        try:
            stat = os.fstat(fd)
            if not stat_lib.S_ISREG(stat.st_mode):
                raise AccessError(f"access map {path}: not a regular file")
            if stat.st_uid != os.geteuid():
                raise AccessError(
                    f"access map {path}: owned by uid {stat.st_uid}, not this process"
                )
            if stat.st_mode & 0o022:
                raise AccessError(
                    f"access map {path}: mode {oct(stat.st_mode & 0o777)} is group- or"
                    " other-writable; policy must be owner-writable only"
                )
            # read to EOF (DL-149): one os.read may return a prefix, and a
            # prefix that ends at valid TOML must never install. The ceiling
            # keeps a growing or mistaken file from parking the reload
            # handler, which runs on the event loop
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(fd, 1 << 16):
                total += len(chunk)
                if total > _MAP_BYTES_CEILING:
                    raise AccessError(f"access map {path}: over the 1 MiB ceiling")
                chunks.append(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(fd)
    except OSError as exc:
        # descriptor I/O joins the refusal path (DL-149): reload turns
        # AccessError into a policy_reload_failed receipt; a raw OSError
        # would escape into the handler-exception log with no receipt
        raise AccessError(f"access map {path}: descriptor I/O failed: {exc}") from exc
    try:
        table = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        # ValueError covers TOMLDecodeError and UnicodeDecodeError, and the
        # BARE ValueError a several-thousand-digit integer literal raises;
        # RecursionError is deep nesting. Both escaped reload before DL-149,
        # and reload must refuse, never raise (ss7)
        raise AccessError(f"access map {path}: not valid TOML: {exc}") from exc

    known_top = {"format_version", "unmapped", "socket_group", "binding"}
    unknown = set(table) - known_top
    if unknown:
        raise AccessError(f"access map {path}: unknown keys {sorted(unknown)}")
    version = table.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        # bool and 1.0 both compare == 1; strict TOML means strict TYPES
        raise AccessError(f"access map {path}: format_version must be the integer 1")
    unmapped_raw = table.get("unmapped", "deny")
    if unmapped_raw not in ("deny", "read"):
        raise AccessError(f"access map {path}: unmapped must be 'deny' or 'read'")
    socket_group = table.get("socket_group")
    if socket_group is not None and (not isinstance(socket_group, str) or not socket_group):
        raise AccessError(f"access map {path}: socket_group must be a non-empty string")

    bindings: dict[str, Tier] = {}
    rows = table.get("binding", [])
    if not isinstance(rows, list):
        raise AccessError(f"access map {path}: binding must be an array of tables")
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"subject", "tier"}:
            raise AccessError(f"access map {path}: binding {i}: exactly subject and tier")
        subject, tier_name = row["subject"], row["tier"]
        if not isinstance(subject, str) or not _subject_valid(subject):
            raise AccessError(
                f"access map {path}: binding {i}: subject {subject!r} must be"
                " user:<realm>/<name> or group:<realm>/<name>, no wildcards"
            )
        if not isinstance(tier_name, str) or tier_name not in _TIER_BY_NAME:
            raise AccessError(f"access map {path}: binding {i}: unknown tier {tier_name!r}")
        if subject in bindings:
            raise AccessError(f"access map {path}: duplicate subject {subject!r}")
        bindings[subject] = _TIER_BY_NAME[tier_name]

    return Policy(
        bindings=bindings,
        unmapped=None if unmapped_raw == "deny" else Tier.READ,
        socket_group=socket_group,
        generation=generation,
        digest="sha256:" + hashlib.sha256(raw).hexdigest(),
    )


def peer_principal(sock: socket_mod.socket) -> Principal | None:
    """The kernel-authenticated peer as a Principal, or None where the
    credential is absent or resolves to no passwd entry — the caller
    refuses the connection on None (ss3; the supervisor's permissive
    fallback is deliberately not copied)."""
    try:
        uid = peer_uid(sock)
    except OSError:
        return None  # getsockopt failed: no credential is no entry
    if uid is None:
        return None
    try:
        pw = pwd.getpwuid(uid)
        gids = os.getgrouplist(pw.pw_name, pw.pw_gid)
    except (KeyError, OSError):
        return None
    groups = set()
    for gid in gids:
        try:
            groups.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue  # a gid with no name cannot match a named row
    return Principal(realm="os", name=pw.pw_name, groups=frozenset(groups))


# --------------------------------------------------------------- verb table

#: access-model ss10: the closed table, cmd -> required tier. `sendevent`,
#: `host` and `seal` are OPS whatever their verb — the verb split inside
#: them is the dispatcher's business, not the perimeter's. A cmd absent
#: here is DENIED at runtime and a test failure at the completeness gate.
REQUIRED_TIER: dict[str, Tier] = {
    "status": Tier.READ,
    "trace": Tier.READ,
    "explain": Tier.READ,
    "spec": Tier.READ,
    "deps": Tier.READ,
    "timers": Tier.READ,
    "plan": Tier.READ,
    "global": Tier.READ,
    "globals": Tier.READ,
    "hosts": Tier.READ,
    "subscribe": Tier.READ,
    "sendevent": Tier.OPS,
    "host": Tier.OPS,
    "seal": Tier.OPS,
}


def required_tier(cmd: object) -> Tier | None:
    """The tier `cmd` needs, or None for a cmd outside the closed table
    (denied — default deny, ss10)."""
    if not isinstance(cmd, str):
        return None
    return REQUIRED_TIER.get(cmd)


# --------------------------------------------------------- perimeter journal


class PerimeterJournal:
    """Append-only `<run_root>/perimeter.jsonl` (access-model ss6). Its
    own `access_seq`, never the engine index — a denial is not an engine
    input and replay must not see policy. Denials are synced before they
    are answered; a storage failure still denies (the caller already has
    the refusal in hand when write() runs)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seq = self._recover_seq(path)
        #: a write that failed MID-LINE left a torn fragment; the next
        #: write starts with a newline so it never glues to the wreck
        self._dirty = False

    @staticmethod
    def _recover_seq(path: Path) -> int:
        """The last complete record's access_seq, so a resumed engine
        continues the series instead of reissuing 1 (a repeated audit key
        is a duplicate record by identity). A torn tail is skipped; an
        unreadable file starts at 0 -- the next write will fail loudly
        enough on a filesystem that lost the journal."""
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            # an existing journal this process cannot read must not be
            # silently restarted at 1 -- a repeated key is a forged identity
            raise AccessError(f"perimeter journal {path}: cannot recover seq: {exc}") from exc
        if raw and not raw.endswith(b"\n"):
            # heal a torn tail with a newline so the next append starts a
            # fresh line instead of gluing to the wreck
            try:
                with open(path, "ab") as handle:
                    handle.write(b"\n")
            except OSError:
                pass  # the next write() will report its own failure
        lines = raw.splitlines()
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
                continue  # the torn tail of a crashed writer
            if not isinstance(record, dict):
                continue  # valid JSON, but no record: ss6 reads records only
            seq = record.get("access_seq")
            # bool is an int in Python and `true` is not a sequence number;
            # neither is a non-positive one -- the series starts at 1 (DL-151)
            if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0:
                return seq
        return 0

    def write(self, rec: str, *, sync: bool, **fields: Any) -> bool:
        """True when the record landed (and, for sync, reached the disk).
        The reload path is receipt-gated on this answer (ss7); denial
        callers ignore it -- a storage failure still denies."""
        self._seq += 1
        record = {
            "rec": rec,
            "access_seq": self._seq,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **fields,
        }
        line = json.dumps(record, sort_keys=True) + "\n"
        try:
            fd, created = self._open_append()
            try:
                payload = line.encode("utf-8")
                if self._dirty:
                    payload = b"\n" + payload  # seal the torn fragment first
                view = memoryview(payload)
                while view:  # a short write must not pass the receipt gate
                    wrote = os.write(fd, view)
                    if wrote <= 0:
                        raise OSError("zero-byte write")
                    self._dirty = True  # bytes are out; a failure now tears the line
                    view = view[wrote:]
                self._dirty = False  # the line is whole even if the fsync below fails
                if sync:
                    os.fsync(fd)
            finally:
                os.close(fd)
            if created and sync:
                # a create is a directory-entry write, and without this the
                # NAME can vanish on power loss after arm() accepted the
                # receipt as synced -- access_seq would then restart and forge
                # duplicate audit keys (DL-137's one spelling; DL-151)
                fsync_dir(self.path.parent)
        except OSError:
            return False  # ss6: a storage failure still denies; the decision stands
        return True

    def _open_append(self) -> tuple[int, bool]:
        """(fd, created). O_EXCL first, so the create is known without a
        second stat: only a created journal owes its parent an fsync. 0600
        from birth -- the run root is group-traversable when access is armed
        (ss8) and receipts are owner-only like the WAL."""
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL
        try:
            return os.open(self.path, flags, 0o600), True
        except FileExistsError:
            return os.open(self.path, os.O_WRONLY | os.O_APPEND, 0o600), False


# ------------------------------------------------------------ the perimeter


@dataclass
class AccessControl:
    """The armed perimeter: the map path, the current policy snapshot and
    the receipt journal. `ControlServer` holds one (or None = today's
    owner-only model, byte-compatible). Reload (ss7) swaps `policy` for a
    freshly validated snapshot and keeps the old one on any failure."""

    map_path: Path
    policy: Policy
    journal: PerimeterJournal
    #: live subscribe streams, writer -> (principal, handler task), so
    #: reload can close exactly the streams that lost read AND wake the
    #: handler parked on queue.get() (ss7). The server maintains it.
    streams: dict[Any, tuple[Principal, Any]] = field(default_factory=dict)

    @classmethod
    def arm(cls, map_path: Path, run_root: Path) -> AccessControl:
        """Load generation 1 and open the journal, or raise AccessError
        (startup refusal — ss4)."""
        policy = load_policy(map_path, generation=1)
        journal = PerimeterJournal(run_root / "perimeter.jsonl")
        control = cls(map_path=map_path, policy=policy, journal=journal)
        if not journal.write(
            "policy_loaded",
            sync=True,
            generation=policy.generation,
            digest=policy.digest,
            bindings=len(policy.bindings),
        ):
            # ss7: a policy change that cannot be receipted does not happen --
            # at startup that means refusing to serve at all
            raise AccessError(f"cannot sync the arming receipt to {journal.path}")
        if policy.socket_group is not None:
            # access-model ss8: the run root is 0700, so the socket alone is
            # unreachable -- grant execute-only traversal to the socket group
            # here, at arming. The 0700 root was the fence for its children
            # (logs/ and runs/ are born 0755), so opening it TIGHTENS every
            # direct child to owner-only first; later artifacts land inside
            # those directories. Log visibility for a group is an explicit
            # operator grant afterwards (ss9), never the perimeter's act.
            try:
                for child in run_root.iterdir():
                    os.chmod(child, 0o700 if child.is_dir() else 0o600)
                gid = grp.getgrnam(policy.socket_group).gr_gid
                os.chown(run_root, -1, gid)
                os.chmod(run_root, 0o710)
            except (KeyError, OSError) as exc:
                raise AccessError(
                    f"cannot open the run root to group {policy.socket_group!r}: {exc}"
                ) from exc
        return control

    def decide(self, principal: Principal, cmd: object, verb: object) -> tuple[bool, str]:
        """One authorization decision under one policy snapshot (ss5).
        Returns (allowed, refusal-prose); writes the receipt either way
        for privileged verbs, denial receipts synced."""
        policy = self.policy  # one snapshot for the whole decision
        required = required_tier(cmd)
        granted = policy.resolve(principal)
        # the receipt carries a bounded LABEL, never request data (ss6): a
        # non-string cmd would stringify the caller's own JSON into the log
        action = cmd[:64] if isinstance(cmd, str) else "<non-string-cmd>"
        if isinstance(verb, str):
            action = f"{action}:{verb[:64]}"
        if required is None or granted is None or granted < required:
            self.journal.write(
                "access_denied",
                sync=True,
                realm=principal.realm,
                principal=principal.name,
                action=action,
                required_tier=required.name.lower() if required is not None else None,
                granted_tier=granted.name.lower() if granted is not None else None,
                policy_generation=policy.generation,
                policy_digest=policy.digest,
            )
            need = required.name.lower() if required is not None else "no listed"
            have = granted.name.lower() if granted is not None else "no"
            return False, (
                f"{principal.spelling} holds {have} tier; {action} needs {need} tier"
            )
        if required >= Tier.OPS:
            self.journal.write(
                "privileged_admitted",
                sync=False,
                realm=principal.realm,
                principal=principal.name,
                action=action,
                required_tier=required.name.lower(),
                granted_tier=granted.name.lower(),
                policy_generation=policy.generation,
                policy_digest=policy.digest,
            )
        return True, ""

    def reload(self) -> None:
        """SIGHUP handler body (ss7): validate the complete candidate;
        success installs it atomically and revokes exactly the live
        subscribe streams that lost read; failure keeps the old policy.
        Never raises — a reload defect must not kill the engine."""
        try:
            fresh = load_policy(self.map_path, generation=self.policy.generation + 1)
        except AccessError as exc:
            self.journal.write(
                "policy_reload_failed",
                sync=True,
                generation=self.policy.generation,
                error=str(exc),
            )
            return
        if fresh.socket_group != self.policy.socket_group:
            # ss8: the group and the modes are applied at arming; a reload
            # that named a different group would change the map and not the
            # kernel -- refuse it whole rather than half-apply it
            self.journal.write(
                "policy_reload_failed",
                sync=True,
                generation=self.policy.generation,
                error="socket_group is fixed at arming; restart the engine to change it",
            )
            return
        if not self.journal.write(
            "policy_loaded",
            sync=True,
            generation=fresh.generation,
            digest=fresh.digest,
            bindings=len(fresh.bindings),
        ):
            # ss7: install is receipt-gated -- a policy change that cannot be
            # receipted does not happen, and the old snapshot stays active
            self.journal.write(
                "policy_reload_failed",
                sync=True,
                generation=self.policy.generation,
                # the policy_loaded line may have LANDED before its fsync
                # failed; this names the generation it must not stand for
                orphaned_generation=fresh.generation,
                error="cannot sync the policy_loaded receipt",
            )
            return
        self.policy = fresh
        for writer, (principal, task) in list(self.streams.items()):
            granted = fresh.resolve(principal)
            if granted is None:  # lost read: close, with a receipt
                self.journal.write(
                    "stream_revoked",
                    sync=True,
                    realm=principal.realm,
                    principal=principal.name,
                    action="subscribe",
                    required_tier="read",
                    granted_tier=None,
                    policy_generation=fresh.generation,
                    policy_digest=fresh.digest,
                )
                self.streams.pop(writer, None)
                try:
                    writer.close()
                except Exception:  # noqa: BLE001 -- a dead transport is already revoked
                    pass
                if task is not None:
                    # the handler is parked on queue.get(); on a quiet
                    # estate nothing else would ever wake it (its finally
                    # does the connection cleanup)
                    task.cancel()
