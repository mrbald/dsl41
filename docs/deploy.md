# Run one engine on one Linux box

For the operator installing, starting, watching and recovering one engine on
one Linux box. The rest of the [operator track](../README.md#documents)
covers incidents and worked estates. This page leaves application authoring
to the [job author guide](authoring.md) and site qualification to the
[qualification items](../TODO.md#qualification).
A Packager uses the install steps as the reference implementation (DL-439).
For one RPM carrying the engine and JIL, use the
[packager recipe and templates](../examples/single-package/README.md).

## Install the package and unit

For the Packager: the supported production interpreter is Python 3.12.x.

On Debian 13, Ubuntu 24.04 and later, install `tzdata-legacy` if the estate uses legacy zone
names such as `US/Eastern` (DL-477). Older releases include these names in `tzdata` itself.

On a VM or host, install its clock synchronization service and enable its waiter
before starting the engine: `chrony-wait.service` with chrony (as on RHEL 8), or
`systemd-time-wait-sync.service` with systemd-timesyncd.
The engine unit orders after `time-sync.target`, but that target waits for
real synchronization only when an enabled waiter runs successfully.
A container cannot synchronize its own clock. Its host or hypervisor must keep
the clock synchronized. Before starting the engine in a container, run the
host checks in step 7 on the host that supplies its clock.
The [chrony waiter](https://github.com/mlichvar/chrony/blob/4.2/examples/chrony-wait.service)
sets `TimeoutStartSec=180`; if it times out, the engine can still start
without synchronization.
The upstream [timesyncd waiter](https://github.com/systemd/systemd/blob/main/units/systemd-time-wait-sync.service.in)
sets `TimeoutStartSec=infinity`. With that default, engine startup waits
until synchronization and can be delayed indefinitely if synchronization fails.
Check the host's installed unit and overrides for its effective timeout.
Without it, the engine can start with an unsynchronized clock.
A clock set ahead stamps the store ahead; after the clock is corrected,
the engine holds ticks and timers until the wall clock catches up, with no bound.
On a backward gap, the engine reports `dsl41: WARNING:` with the wall reading,
the instant engine time resumes at and the gap in seconds in the
[engine's output](#read-the-engines-output). `dsl41 query status` shows the
hold; its JSON answer carries `clock_hold` (DL-493).

The engine and every client must change together (DL-498).
Restart every terminal UI from the selected build. A terminal UI started from
an old build is refused by name. Restart the served UI with the engine too.
See the [control dialect](control-protocol.md) for version refusals.

1. As root, create the release environment and install the selected version.
   Substitute a concrete version for `<version>` in every command and in
   the unit. The [package instructions](../README.md#install) describe extras.

   ```sh
   python3.12 -m venv /opt/dsl41/releases/<version>
   /opt/dsl41/releases/<version>/bin/pip install dsl41==<version>
   ```

2. Prepare the `dsl41` system user and group using the site's account tools.
   Jobs run as this user; an `owner` naming another user refuses preflight.
   As root, create the service and application directories:

   ```sh
   install -d -m 0755 /etc/dsl41 /opt/nightbank /opt/nightbank/cfg
   install -d -m 0755 /srv/dsl41 /srv/dsl41/runs /srv/dsl41/nightbank
   install -d -o dsl41 -g dsl41 -m 0700 /srv/dsl41/runs/nightbank
   ```

3. From the checkout, install the synthetic small Nightbank inputs as root:

   ```sh
   cp examples/nightbank/estate/small/*.jil /opt/nightbank/cfg/
   cp -R examples/nightbank/bin /opt/nightbank/
   install -m 0644 examples/nightbank/deploy/dsl41.toml /etc/dsl41/dsl41.toml
   ```

   Use an empty application config directory so its glob cannot select stray
   JIL. The Python package does not install this repo-only estate.
   Read [what the main config selects](#what-the-main-config-selects).

4. Generate the night's properties and directories with the installed
   interpreter, from the same checkout. Replace `<version>` first.
   Choose a fresh night directory; preparing over an existing night's data
   would mix its incident counters and outputs.

   ```sh
   /opt/dsl41/releases/<version>/bin/python - <<'PY'
   import runpy
   import sys
   from datetime import UTC, datetime, timedelta
   from pathlib import Path

   night = Path("/srv/dsl41/nightbank/night")
   if night.exists():
       raise SystemExit("Choose a fresh night directory")
   prepare = runpy.run_path("tests/nightbank_props.py")["prepare_night"]
   props = prepare(night, "small", anchor_utc=datetime.now(UTC) + timedelta(minutes=15))
   props["OWNER"] = "dsl41"
   (night / "night.properties").unlink()
   Path("/opt/nightbank/cfg/night.properties").write_text(
       "".join(f"{key}={value}\n" for key, value in props.items())
   )
   (night / "profile.env").write_text(
       f'export NIGHTBANK_DATA="{night}/data"\n'
       f'export NIGHTBANK_STATE="{night}/data/state"\n'
       f'export NIGHTBANK_INCIDENTS="{night}/incidents.conf"\n'
       'export NIGHTBANK_SOCKET="/srv/dsl41/runs/nightbank/control.sock"\n'
       f'export PATH="/opt/nightbank/bin:{Path(sys.executable).parent}:$PATH"\n'
   )
   PY
   ```

5. Give the service user its night's files:

   ```sh
   chown -R dsl41:dsl41 /srv/dsl41/nightbank/night
   chmod 0600 /srv/dsl41/nightbank/night/profile.env
   chmod 0644 /opt/nightbank/cfg/night.properties
   ```

   Inspect the copied `incidents.conf`; it intentionally injects training
   failures. The profile puts `fakework` and the installed engine on PATH
   and supplies the socket used by the estate's `SET_GLOBAL` commands.
   The [Nightbank preparation](../examples/nightbank/README.md#quick-start)
   gives a checkout-only alternative.

6. Check the installed inputs as the service user before starting the unit:

   ```sh
   sudo -u dsl41 /opt/dsl41/releases/<version>/bin/dsl41 lint --config /etc/dsl41/dsl41.toml
   ```

   Inspect findings even after exit 0; the shipped lint policy is `none`.

7. Copy the [engine unit](../examples/nightbank/deploy/dsl41-engine.service)
   to `/etc/systemd/system/dsl41-engine.service`.
   Copy the [failure unit](../examples/nightbank/deploy/dsl41-engine-down.service)
   to `/etc/systemd/system/dsl41-engine-down.service`.
   Install the site's executable hook at `/etc/dsl41/status-hook`, executable
   by `dsl41`. Set that same absolute path in TOML and the failure unit's
   literal arguments. Set its run root and engine-unit arguments to match.
   Substitute `<version>` in all three executable paths. Set `RequiresMountsFor`
   to the configured run root and every required input filesystem.
   Set `ExecStop`'s socket to that root's `control.sock`.
   Include the profile, worker and data filesystems in the mount dependencies.
   Keep the unit's time-sync ordering. On a VM or host, run the following
   checks there as root. For an engine in a container, run them on its host
   before starting the engine. A waiter's `ConditionVirtualization=!container`
   or `ConditionCapability=CAP_SYS_TIME` can skip it inside a container or
   under a manager without that capability, even when `is-enabled` says
   `enabled`. With chrony on RHEL 8:

   ```sh
   systemctl enable --now chronyd.service
   systemctl enable --now chrony-wait.service
   systemctl is-enabled chrony-wait.service
   systemctl status chrony-wait.service
   systemctl show -p ActiveState,Result,ConditionResult chrony-wait.service
   chronyc tracking
   ```

   With systemd-timesyncd instead:

   ```sh
   systemctl enable --now systemd-timesyncd.service
   systemctl enable --now systemd-time-wait-sync.service
   systemctl is-enabled systemd-time-wait-sync.service
   systemctl status systemd-time-wait-sync.service
   systemctl show -p ActiveState,Result,ConditionResult systemd-time-wait-sync.service
   timedatectl show -p NTPSynchronized
   ```

   `is-enabled` must print `enabled`. Require `ActiveState=active`,
   `Result=success` and `ConditionResult=yes` from `show`: the waiter must be
   active after exiting successfully, not skipped. Check `status` for a
   timeout or failure. With chrony, require `Leap status` to be `Normal` in
   `tracking`; with timesyncd, require `NTPSynchronized=yes`.
   Enabling the waiter alone does not prove synchronization succeeded.
   Verify the installed engine unit as root:

   ```sh
   systemd-analyze verify /etc/systemd/system/dsl41-engine.service /etc/systemd/system/dsl41-engine-down.service
   ```
8. As root, reload systemd's unit definitions:

   ```sh
   systemctl daemon-reload
   ```

9. After the checks and unit edits, set the training start times last.
   As root, refresh only the properties using the same checkout:

   ```sh
   /opt/dsl41/releases/<version>/bin/python - <<'PY'
   import runpy
   from datetime import UTC, datetime, timedelta
   from pathlib import Path

   compute = runpy.run_path("tests/nightbank_props.py")["compute_properties"]
   props = compute(Path("/srv/dsl41/nightbank/night"), datetime.now(UTC) + timedelta(minutes=15), 3)
   props["OWNER"] = "dsl41"
   Path("/opt/nightbank/cfg/night.properties").write_text(
       "".join(f"{key}={value}\n" for key, value in props.items())
   )
   PY
   ```

10. As root, enable the unit and start it within ten minutes of step 9:

    ```sh
    systemctl enable --now dsl41-engine.service
    ```

    The [systemctl manual](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)
    defines these unit-management commands.

If the first region's start time passes before the first start, remove the
training night directory and repeat steps 4–6 and 9 before starting.
Removing `/srv/dsl41/nightbank/night` discards its synthetic data and counters;
keep any evidence you need first. A root with `engine.db` keeps its stored
revision. For that root, use [reread and update](#reload-a-changed-estate)
while the engine runs to adopt refreshed properties, or select a fresh run
root in both the main config and the unit's `ExecStop` socket before starting.
A restart alone does not adopt the refreshed times.

Read the unit in these groups:

| Lines | What they do for the operator |
| --- | --- |
| `[Unit]`, `Description`, `RequiresMountsFor` | Name the service and require the run root's filesystem before starting. An absent mount must not look like a fresh root. |
| `Wants`, `After` | Order startup after `time-sync.target`; the host must enable the synchronization waiter described above. |
| `OnFailure` | Start the failure unit `dsl41-engine-down.service` when the engine unit fails. The failure unit is `Type=oneshot` as `dsl41` with `UMask=0077`, `TimeoutStartSec=7` and `KillMode=control-group`. It runs the site's status hook with literal arguments (DL-446). |
| `StartLimitIntervalSec`, `StartLimitBurst` | Allow at most five starts in 300 seconds, then leave a repeated crash failed. |
| `[Service]`, `Type`, `User`, `Group` | Run the engine directly as `dsl41:dsl41`. |
| `ExecStart` | Read the main config. A root containing `engine.db` resumes its stored active catalog. |
| `ExecStop` | Ask for a drain with `engine stop --wait`. Wait for the engine process identified on that connection to exit. Without a connection or peer pid, confirmation uses `leader.lock` (DL-496). |
| `TimeoutStopSec` | Give the stop 45 seconds with the default graces. Revisit this if you change the engine's grace settings. |
| `Restart`, `RestartSec` | Restart after any exit, including 0, after five seconds. A CLI stop outside systemd therefore comes back on the same root. A systemd stop keeps the service down. |
| `RestartPreventExitStatus` | Keep exits 2, 6 and 7 down: startup refusal, a time-half fault before an engine input was logged, or a statechart assertion failure. Read the error before starting again. |
| `KillMode` | End remaining processes in the unit's control group after `ExecStop`, including jobs that escaped their process group. |
| `LimitNOFILE` | Set the open-file limit to 65536. |
| `[Install]`, `WantedBy` | Attach the enabled unit to `multi-user.target` for boot. |

After the start limit is hit, read the [unit's journal](#read-the-engines-output)
and fix the reported fault before starting again. As root, clear the limit:

```sh
systemctl reset-failed dsl41-engine.service
```

This resets the failed state and start counter; it does not start the engine.
Then follow [recovery](#recover-after-the-engine-or-box-dies).
See [systemctl reset-failed](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html#reset-failed%20%5BPATTERN%E2%80%A6%5D).

## Serve people behind the authenticating proxy

For the Packager: this synthetic example serves `https://control.example.invalid`
under the engine account `dsl41` (DL-499, DL-507). Install the same release with the
`[ui]` extra for the engine and web service. The [access model §9](access-model.md#9-the-web-tier)
owns the wrapper rules. `dsl41 serve` remains an OS-account service.

Any process under `dsl41`, including a batch job, can state any person and roles.
Protect the start script, lookup code, release environment and access file.
The proxy must replace every header the lookup reads and protect every library route.

1. Create the OS group `dsl41-web` and add nginx's worker account to it using
   the site's account tools. Keep other accounts out. Restart nginx after a
   group-membership change. The web service uses this group for its socket;
   the engine's control socket can remain owner-only.

2. Add the `app` rows below to the site's access file and keep its `os` rows.
   This example uses `/etc/dsl41/web-access.toml`, owned by `dsl41` with mode `0600`.
   Its parent must satisfy [access §4](access-model.md#4-the-role-map).
   In the engine's main config, set `access_map = "/etc/dsl41/web-access.toml"`
   under `[engine]`, then restart the engine. After that, replace the file by
   rename and send the engine `SIGHUP` to load new contents without a restart
   ([access §7](access-model.md#7-reload-and-revocation)).
   `user:os/dsl41` needs adm so the engine unit's stop command can drain the engine.

   ```toml
   format_version = 1
   unmapped = "deny"

   [[binding]]
   subject = "user:os/dsl41"
   tier = "adm"

   [[binding]]
   subject = "group:app/observers"
   tier = "read"

   [[binding]]
   subject = "group:app/operators"
   tier = "ops"

   [[binding]]
   subject = "group:app/administrators"
   tier = "adm"
   ```

3. Supply an importable `site_people.resolve(headers)` in the release environment.
   It receives a read-only copy of the request's headers and returns
   `(user_id, roles)` or None. Roles are a list of non-blank scalar strings.
   `headers[name]` reads a name in any case. A name that was not sent raises
   `KeyError`, so `headers.get(name)` can fall back to a second name. A name sent
   more than once, or with a blank value, raises `ValueError`, which refuses the tab
   unless the lookup catches it.
   The service names no header; the lookup chooses which ones it reads.
   For a synthetic smoke check it can read `headers.get("X-DSL41-Email")` and return
   `("alice", ["operators"])` for `alice@example.invalid` and None for anything else.
   A proxy that passes roles sets them in a header the lookup reads.
   Two tabs opening together call `resolve` from two threads; it must be safe for that.
   The production lookup owns its finite database timeouts.
   The web service adds no timeout, cache or retry.
   `public_url` is `scheme://host[:port]` followed by zero or more `/segment`.
   A segment is one or more of `[A-Za-z0-9._~-]` and is neither `.` nor `..`.
   Any other scheme, port or path, a trailing slash included, refuses at startup.
   The host's spelling is not checked. Spell the part before the base path as a
   browser sends `Origin` (lowercase ASCII host, no default port); any other
   spelling starts and refuses every tab with 403.
   If people also reach the UI under another name, such as the short name `control`,
   list its origin in `extra_origins` (DL-509). Each entry is `scheme://host[:port]`,
   spelled the same way, with no path; the UI keeps `public_url`'s base path under it.
   No two origins may share `host[:port]` in any case, so two that differ only in
   scheme refuse at startup. A page links to, and admits WebSockets from, only the
   name its request's `Host` names; a `Host` that names none gets `public_url`'s.
   Write this root-owned start script at `/etc/dsl41/serve-people.py`:

   ```python
   from dsl41.runner_web import serve_people
   from site_people import resolve

   serve_people(
       resolve,
       socket_path="/run/dsl41-web/web.sock",
       public_url="https://control.example.invalid",
       control_socket="/srv/dsl41/runs/example/control.sock",
       # extra_origins=["https://control"],
   )
   ```

4. Install `/etc/systemd/system/dsl41-web.service` as root. Substitute the
   selected release version. The runtime directory and umask grant nginx's
   group access to the socket. They grant no access to the engine's files.

   ```ini
   [Unit]
   Description=dsl41 person-aware web UI
   After=dsl41-engine.service

   [Service]
   User=dsl41
   Group=dsl41-web
   RuntimeDirectory=dsl41-web
   RuntimeDirectoryMode=0750
   UMask=0007
   ExecStart=/opt/dsl41/releases/<version>/bin/python /etc/dsl41/serve-people.py

   [Install]
   WantedBy=multi-user.target
   ```

   systemd's default control-group kill mode ends remaining UI children at
   service stop, with TERM followed by KILL. The service has no custom shutdown
   timeout. The web process keeps aiohttp's signal handling.

5. Configure oauth2-proxy for the site's identity provider with
   `--set-xauthrequest`. In nginx's TLS server for `control.example.invalid`,
   put the library's whole site behind authentication. The root location covers
   `/`, `/ws`, `/static/` and `/download/`. These lines belong in that server:

   ```nginx
   location = /oauth2/auth {
       internal;
       proxy_pass http://127.0.0.1:4180/oauth2/auth;
       proxy_pass_request_body off;
       proxy_set_header Content-Length "";
       proxy_set_header X-Original-URL $scheme://$host$request_uri;
       proxy_set_header Host $host;
   }

   location /oauth2/ {
       proxy_pass http://127.0.0.1:4180;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Auth-Request-Redirect $scheme://$host$request_uri;
   }

   location / {
       auth_request /oauth2/auth;
       error_page 401 = /oauth2/sign_in;
       auth_request_set $dsl41_email $upstream_http_x_auth_request_email;
       proxy_set_header X-DSL41-Email $dsl41_email;
       proxy_set_header Host $host;
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
       proxy_pass http://unix:/run/dsl41-web/web.sock:;
   }
   ```

   The proxy replaces every occurrence of each header the lookup reads with the
   authenticated value; `proxy_set_header` does that here. The count is taken on
   the request the service receives. A proxy that joins two lines into one
   comma-separated value shows one, and the lookup owns the syntax of each value.
   nginx sends no header when the value it sets is empty, so the lookup sees that
   name as not sent.

   For two names, list both in the TLS server's `server_name`, for example
   `server_name control.example.invalid control;`, with a certificate for both.
   The proxy passes `Host` as the browser sent it. nginx's `$host` drops the port,
   so an origin with a port needs `proxy_set_header Host $http_host;` instead.
   A cache in front of the index page keys it by host name. The proxy's login
   belongs to one name, so a person signs in under each name they use.
   The identity provider accepts the sign-in callback under each name, and
   oauth2-proxy builds its redirect address from the request, with no single
   `--redirect-url`.

   To serve the UI under a base path instead, set
   `public_url="https://control.example.invalid/dsl41"` in the start script and
   use these two locations in place of the root location. The proxy maps both
   `/dsl41` and `/dsl41/` to the service's `/`, and strips `/dsl41` from every
   other path, so the service's routes do not move:

   ```nginx
   location = /dsl41 {
       return 308 /dsl41/;
   }

   location /dsl41/ {
       auth_request /oauth2/auth;
       error_page 401 = /oauth2/sign_in;
       auth_request_set $dsl41_email $upstream_http_x_auth_request_email;
       proxy_set_header X-DSL41-Email $dsl41_email;
       proxy_set_header Host $host;
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
       proxy_pass http://unix:/run/dsl41-web/web.sock:/;
   }
   ```

6. As root, verify and start the service, then reload the checked nginx config:

   ```sh
   systemd-analyze verify /etc/systemd/system/dsl41-web.service
   systemctl daemon-reload
   systemctl enable --now dsl41-web.service
   nginx -t
   systemctl reload nginx
   ```

   Open a new tab as the synthetic person.
   The header shows `app/alice (operators)`: the person and roles the lookup
   returns, not the access the engine grants (DL-510). Check the engine's perimeter journal
   for `realm: "app"`, `principal: "alice"` on a mutation. An unmapped person
   must get a closed tab and a denial under that person's name.

The service has these limits (DL-499, DL-507, DL-509):

- **L1.** Every process under the engine's account can state any person and role.
  With the engine's account at adm, a job gains no new command, only the means
  to put a person's name on what it does. If the account is unmapped, a job
  gains the levels granted to application roles, up to adm.
  It already has the account's filesystem rights, including the access file.
- **L2.** Roles are read once per tab. Reconnecting to the engine retains them.
  Logout or role removal does not end an open tab. A new tab reads them again.
- **L3.** An admitted READ request writes no receipt. A denial does, as does
  an admitted higher-tier request.
- **L4.** A UI process that hangs after its tab closes can remain until systemd
  stops the service's whole control group.
- **L5.** Every admitted person can read job logs through the UI, which reads
  files as the engine's account. Losing READ ends the UI on its next status denial.
- **L6.** Recovery hints give shell commands. A browser user cannot run them.
  An administrator using a shell acts under the administrator's own name.
  If the engine still retains the person's request, the administrator's retry
  is refused because the actor differs. Otherwise the administrator's command
  may be admitted as the administrator's own.
- **L7.** The person travels in one process argument. The OS argument-size limit
  can prevent a tab with an extreme number of roles from starting.
- **L8.** An older unarmed engine answers `as` with `unknown_cmd`. The UI shows
  the fault and keeps asking; no command runs. Install the engine and web service
  together and restart the engine to complete the upgrade.
- **L9.** Every page of an origin passes the `Origin` check for that origin under
  any path. Applications that share an origin are trusted like the UI.
- **L10.** The library's file-download link is root-relative, so a base path does
  not serve it. The command palette's command that saves an image of the screen
  delivers through that link and fails under a base path (BUG-91).

## What the main config selects

The main config selects these JIL files directly, not `estate.toml`.
Its properties path supplies schedule anchors, owner, data, log and
profile paths. It has no environment tables, so no environment selection
is needed. If a site's main config declares `[estate.environments.NAME]`,
set `engine.environment = "NAME"`; environment properties follow base
properties. Portable manifest fixtures select their environments with
`--environment NAME` instead.

Set `engine.run_root`, input patterns and properties in the main config.
For job environment values, add a literal string map to that config:

```toml
[engine.job_env]
AUTOSERV = "synthetic"
LOG_DIR = "/var/log/dsl41"
```

Apply edits with `reread` and `update`; a restart uses the applied revision.
`engine.fw_default_interval_s` sets the default poll interval for FW jobs that omit
`watch_interval`. Use a positive whole number of seconds; omission means 60 (DL-469).
The setting is revisioned and excluded from job-definition hashes. Its update stops no
watch: an in-flight watch keeps its interval until it ends or the engine restarts.
A resumed or new watch uses the applied default. Explicit job intervals take precedence.
A restart reads `engine.kill_signals` from the current configuration on disk;
its default is `[2, 9]`.
An edit only to `job_env` advances the revision without stopping jobs,
including while boxes run (DL-447). Later dispatches and replacements use
the new map; attempts already running keep their recorded environment.
`reread` and `update` require ADM and prepare inputs under the engine identity (DL-463).
`AUTOSERV`, `AUTOSYS`, `AUTOUSER` and `AUTOROOT` are unset unless this map
supplies them. Other names inherit the engine's startup environment, except
generated invocation values and `DSL41_SOCKET` (DL-447, DL-459).
Map values are literal strings. Names use `[A-Za-z_][A-Za-z0-9_]*`.
NULs and overrides of `DSL41_*`, `AUTO_JOB_NAME` or `AUTORUN` refuse.
Set path variables here; the author follows [Set stream paths](authoring.md#set-stream-paths).

The applied launch settings control every CMD attempt (DL-492):

- `engine.job_shell` selects an executable sh, dash, bash or ksh by absolute
  path. Its default is `/bin/sh`, independent of the account's passwd shell.
  The engine composes the line from POSIX text for sourcing profiles and the job's
  own command text; a site that sets a shell other than `/bin/sh` checks its own
  jobs under it (DL-497).
- `engine.job_profile` names a literal absolute readable profile. The shell
  sources it before the job's `profile`. Either profile's failure stops the
  command with that profile's exit status.
- `engine.std_file_mode` is `"append"` by default. `"truncate"` truncates an
  explicit unprefixed output before each launch. `>` truncates and `>>`
  appends independently of this setting. Default per-run logs always append.

The wrapper opens streams before sourcing profiles. The author's
[unsupported JIL inventory](authoring.md#2-check-which-constructs-dsl41-accepts)
lists attributes that produce warnings.

Input paths may be absolute or relative to that file; the run root must
be absolute. Patterns support `*`, `?` and brackets; `**` is refused.
The config is a regular file, never a symlink. For exception jobs, set
`estate.non_idempotent` to the exceptions file's path; its default is
`non-idempotent.toml` beside the main config, not beside the JIL.
Python inputs may supply `estate()` returning `CatalogBuilder`, as with
`dsl41 build`; see [authoring](authoring.md) for that input form.
Keep `estate.toml` for explicit portable fixtures and legacy `--manifest`
invocations. These input rules are [DL-437](decision-log.md).

## Start the estate

Do not launch a second engine on the service's root. For a foreground run
with the service stopped, run this as the service user:

```sh
/opt/dsl41/releases/<version>/bin/dsl41 run --config /etc/dsl41/dsl41.toml
```

The engine prints `engine up; control socket: ...` when startup finishes.
It keeps running and accepts commands from another terminal.
It stores inputs in the root's `engine.db` and creates `engine.toml` with
default settings on the first run. The same command resumes an existing root.
First start requires a root without surviving runtime evidence (DL-443, DL-445).
Settings and inputs may be provisioned first. A missing store beside attempt
spools, update material, root-local logs or receipts, legacy state, a nonempty
lock, SQLite sidecars or a control socket
refuses with exit 2 before changing the root or evaluating estate Python.
Unreadable evidence also refuses. See the [genesis and restart contract](runner-design.md#7-journal-and-recovery-e1-prod-grade).
Keep stop graces, snapshot interval and retention in `RUN_ROOT/engine.toml`.
The archive's two settings live there too ([Keep the archive](#keep-the-archive)).
With the default graces, the unit allows 45 seconds for a stop.
Check that timeout before changing either grace.

The main config's `[lint]` defaults are `refuse = "none"` and `ignore = []`.
`error` blocks unignored errors; `warn` also blocks warnings. Info never blocks.
First start and `reread` print errors and policy-blocked findings in full, count
the rest by code and severity, and name the command for the full list (DL-483).
Restart counts captured `known-unsupported` warnings. Its pointer names findings
from files on disk, which may differ from the applied estate until an update.
Other preflight warnings print in full. Captured `lint.ignore` still applies;
restart does not apply lint refusal (DL-483).
`lint` and `rehearse` print every finding. Only registered lint codes may be ignored.
This policy cannot suppress input, lowering, exception or preflight errors.
`dsl41 lint --config /etc/dsl41/dsl41.toml` exits 1 for blocking findings and
2 for refused inputs. Exit 0 under `none` may include findings.
`dsl41 rehearse --config /etc/dsl41/dsl41.toml` uses the same interpretation;
its optional output `--run-root` must neither equal, contain nor lie inside
the engine's root.
Python preparation executes authoring code. Captured generated JIL is the
authority for update and replay; imported dependencies are not fully tracked.
Use prebuilt JIL when the deployment requires reproducible builds.

| Exit | What it means when `run` returns |
| --- | --- |
| 0 | Operator stop, including a drain that ended uncleanly. |
| 1 | Engine or estate failure while running, including failure to record shutdown. |
| 2 | Startup refused: preflight, input or resume error. |
| 6 | The time-processing part of an engine input failed before the input was logged. |
| 7 | A declared state machine hit an assertion failure. |

## Read the engine's output

For the operator, read the engine's journal with:

```sh
journalctl -u dsl41-engine.service
```

Under the unit, read `engine stopped: ...`, startup refusals and exit-6/7
reports here. The [incident guide](incident-triage.md) covers collecting fault evidence.
The unit leaves output routing to systemd's defaults:
stdout goes to the journal and stderr follows it, unless the host overrides
those defaults. See [systemd output routing](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#StandardOutput=)
and [journalctl's unit filter](https://www.freedesktop.org/software/systemd/man/latest/journalctl.html#-u).
The foreground run prints the same shutdown record to its terminal.
Output and refusal handling: [run CLI](../src/dsl41/cli_run.py) and
[shared CLI](../src/dsl41/cli_common.py).

On restart, startup stderr reports scheduled starts missed while the engine
was down (DL-476):

```text
engine was down 2026-07-01T09:00:00 to 2026-07-01T11:05:00; 4 scheduled starts were missed and will not run late
  poll_30m: 4 missed, last 2026-07-01T11:00:00
```

One total precedes one line per affected job, sorted by job name.
Jobs on ice and jobs already armed on hold at start are left out.
Held jobs that are not armed and noexec jobs count (DL-483).
A first start or a restart with no remaining missed starts prints no such report.
Times use the engine host's local clock display (DL-473).
The report appears only in engine start output, including `dsl41 run --ui`
before the terminal UI attaches. Under systemd, read it with `journalctl`
as above. [Recover after an outage](#recover-after-the-engine-or-box-dies)
before choosing any manual catch-up.

Job output goes to each job's `std_out_file` and `std_err_file` when set.
Each unset path falls back to `<run_root>/logs/<job>.<run>.{out,err}`.
For stream placeholders, output prefixes and their limits, see [Set stream paths](authoring.md#set-stream-paths).
The engine removes default job output with the run's history (DL-557): a
run's `logs/<job>.<run>.out` and `.err` go with its `runs/<job>.<run>/` once
all of them are older, by a day, than the oldest record the live store keeps.
With the defaults that is about 90 days. [Keep expired files](#keep-expired-files)
moves them to a directory of the site's instead. The engine does not touch
output a job writes to its own `std_out_file` or `std_err_file`, and does not
monitor disk space.
The site's archiver may move finished job output from `logs/`, or take expired
entries from `expired_dir`.
It must leave everything else in the run root untouched, including `runs/`,
`plans/`, the store (`engine.db` and its sidecars), `leader.lock`,
`control.sock` and `engine.toml`.
The archive (`archive.db`, its `-wal` and `-shm`, `archive.db.lock`, and any
archive moved aside and kept) stays in place; see [Keep the archive](#keep-the-archive).
The host's monitoring must watch disk space.

## Watch the engine and jobs

Run the status query as the service user with this unit's owner-only socket.

```sh
/opt/dsl41/releases/<version>/bin/dsl41 query status --json \
  --socket /srv/dsl41/runs/nightbank/control.sock
```

The command prints complete UTC JSON. Omit `--json` for text with full local
timestamps (`YYYY-MM-DDTHH:MM:SS`). Local means the host running the CLI or
TUI; a served TUI uses the server host, not the browser timezone. A container
with no zone database shows UTC. Install zone data and configure the host
timezone before relying on local display (DL-473).

`engine` reports the engine state, including
STARTING during restart catch-up and DRAINING during a stop.
`jobs` contains job status and revisions. An unsettled command can have an
`attempt` block; a held interruption has `interrupted` evidence.
`config_rev` names the applied configuration; `update` appears while an
update is in progress. See the [status contract](control-protocol.md#4-query-verbs-frozen-response-shapes)
for field meanings.

`--style neutral` selects the neutral text layout, and `--brief` the neutral
table with per-job revisions.

Exit 0 means the engine answered, even if a job failed.
Exit 2 means refusal or failure to reach the engine.
For a configured access map, follow the [access model](access-model.md);
stop, reread and update require its `adm` tier.
A member of `socket_group` who may stop the engine can confirm its process exit with `engine stop --wait` when the peer pid is available (DL-496).
Without a peer pid, the stop is still sent, and confirmation uses `leader.lock`; an unreadable lock produces status 2.
Hidden process details reduce confirmation to pid disappearance; pid reuse can prolong the wait.
For AutoSys task equivalents, use the [operator command table](incident-triage.md#read-the-jobs-state).

`dsl41 serve --socket S` serves the terminal UI to a browser and starts one
`dsl41 ui` process per session. `--host` defaults to `127.0.0.1` and `--port` to
8000. The server has no authentication of its own: keep the loopback default, or
put an authenticating proxy or tunnel in front of it. Per-person access is in
[Serve people behind the authenticating proxy](#serve-people-behind-the-authenticating-proxy).

The optional status hook reports job failures, terminations, holds, deadline
alarms and long-running jobs (DL-446, DL-469). The engine attempts to report
startup refusals once valid hook settings load. The separate failure unit reports
unit failure even when the TOML or store is unreadable. A normal stop leaves it
inactive. A crash can restart without activating it.

Restart after editing `[engine] status_hook` or `status_hook_long_running_s`.
To disable engine notifications, first clear `send_notification` on every job
that enables it. [Apply the changed estate](#reload-a-changed-estate) while the
hook is still configured. Then remove `status_hook` from the main config and
restart. Startup preflight requires a configured hook while any applied job
enables `send_notification`.

To disable unit notifications, remove `OnFailure` from the engine unit and run
`systemctl daemon-reload` as root. Rehearsal never invokes the hook.

Delivery is best effort. The engine can drop events without a retry or spool.
Unconfirmed process cleanup disables delivery. Check the engine journal for
delivery failures. The hook runs as the service account. Keep it in its process
group and keep its writes outside the run root. Follow the
[hook contract](runner-design.md#4-engine-loop--single-writer) for thresholds,
alarm flags, payloads, delivery bounds and the hook environment.

## Keep expired files

The engine keeps the files of a run, and each update plan, as long as the
live store keeps the run's history, and one day more (DL-557). A run's files
are `runs/<job>.<run>/` (the wrapper's spool, a file watch's `watch.jsonl`)
and its default output `logs/<job>.<run>.out` and `.err`; a plan is
`plans/<token>/`. A run goes when all of its files are older, by a day,
than the oldest record the live store keeps, so `retention_days` (90) sets
the age and the archive does not lengthen it. A plan goes by its
`plan.json`'s age. The engine checks once a day, at a snapshot, and keeps
every run that is open, each job's last run, and every plan while an update
is prepared or stops. An entry that is a symbolic link is not the
engine's and stays. One line on standard error names what a check removed
or could not remove. `runs/`, `logs/` and `plans/` are the engine's alone:
share none of them with anything else, through a link or otherwise.

To keep the files instead, set a directory outside the run root in the
main config and restart:

```toml
[engine]
expired_dir = "/srv/dsl41/expired/nightbank"
```

The engine then moves each expired entry there, keeping its place and
name: `runs/<job>.<run>/`, `logs/<job>.<run>.out` and `.err`, and
`plans/<token>/` under that directory. A run's logs arrive before its
directory. A move never replaces. The engine first checks every name the
entry would take there, both logs and the directory, whether or not the run
has that part; a name already there is an error, nothing of the entry
moves, and it stays in the run root until the archiver has taken the
earlier one away. A file is linked there and then unlinked in the run root,
so a name that appears meanwhile is still not replaced. Give each run root
its own `expired_dir`. This is the place to hook the site's archiver: a
systemd path unit can watch the directory, and the archiver takes the
entries away. The engine makes the three subdirectories and never reads or
empties it. Create the directory, owned by the service account. `runs/`,
`logs/`, `plans/` and `expired_dir` must be on one file system for a move. A move that
fails leaves the entry in the run root, deletes nothing, and is named in the
check's line; the next check tries again. A move that fails partway, such
as a rename across file systems after the logs moved, leaves what moved
under `expired_dir` and the rest in the run root; a later check moves the
rest once the archiver has taken what moved. A path that is the run root or inside it, also
through a link, refuses the start; a missing directory does not, and shows
only in that line.

## Keep the archive

The archive holds copies of the records old enough to be pruned. The
engine copies a record when it becomes old enough to leave the live store,
and the record stays in both stores until the engine's prune step deletes
it from the live store. A record that has never been old enough to be pruned
is only in the live store: with the default `retention_days` of 90 that is
every record of the last 90 days. A record copied under a shorter retention
stays in the archive when the retention is raised. Read recent runs with
`dsl41 runs` or `dsl41 query history`. An empty answer from the archive does
not mean that nothing ran.

The archive answers audit questions after the live store has let a record
go: what ran, when, under which definition, with what outcome, and what
operators did. The live store keeps `retention_days` (90) of the engine's
records. Before retention deletes a record, the engine copies it to the
archive database, reads the copy back and compares it. Only a verified copy
lets the record go. The archive keeps the copy for `archive_days` (550). A
record therefore lives about `retention_days` plus `archive_days` in all:
about 640 days with the defaults. The newest `retention_days` are read from
the live store, not the archive.

| Setting in `engine.toml` | Default | Meaning |
| --- | --- | --- |
| `archive_days` | 550 | Days an archived row stays. 0 keeps no archive, and retention deletes without a copy. `--archive-days` overrides it for one run. |
| `archive_path` | `<run_root>/archive.db` | The archive database. A relative path is read under the run root. Use a local file system. |

A new root's `engine.toml` names neither key: an absent key is its
default. Write a key only to change it. The build before the archive
refuses an `engine.toml` that names either key, so before going back a
build, remove those lines; that build then prunes without copying.

The file's permissions are the access rule. The engine creates it mode 0600,
owned by the service account, and its `-wal` and `-shm` take that mode. SQL
on it shows every record the engine kept, dispatch environments included.
The engine refuses to write an archive file, `-wal` or `-shm` that group or
others may read or write, and the archive stops with that reason. Give a
reader a copy, made as the service account, only where those contents may
be seen.

**Open it** read-only, as the service account, while the engine runs or
not:

```sh
sqlite3 -readonly /srv/dsl41/runs/nightbank/archive.db
```

Keep reads short: a reader holding the log delays the engine's checkpoint
of the archive. Between rounds the engine leaves no `-shm` beside the file,
and some builds of the `sqlite3` shell then refuse a read-only open with
`unable to open database file`. Open it without `-readonly` then, and run
only `SELECT`: a write from the shell holds SQLite's write lock on the
file, and the next round stops with `OperationalError: database is
locked` and works again at the round after.

**Lines of history.** A root's records can have more than one line. After
the live store is put back to an older copy, the engine writes other
records at seqs the archive already holds. The archive keeps every version
of a seq it was shown, each under the history the live store gave it: one
history per engine start (DL-536). The newest history list the archive
recorded says which history holds each seq now; that is the current line.
Each seq up to the list's last seq has one of three states:

| State | Meaning |
| --- | --- |
| `not archived` | No version of the seq counts under the history that holds it, or, where no history holds the seq, no version with no history is stored. It was never received, it expired, it is still only in the live store, or only another line has it. |
| `line not known` | A version is stored and no history holds the seq, or several versions count under the history that holds it. The default views show every one, marked, and join nothing across the seq. |
| `on line` | One version of the seq counts under the history that holds it. Its rows are the current line's. |

The states are decided in that order: a seq with nothing stored reads
`not archived`, whether or not a history holds it.

A version counts unless another of the same seq, history and record says
the same and more: the archive keeps both, and the fuller one is shown. Two
versions that disagree both count, and the seq reads `line not known`. A
record the archive holds twice, once with its transitions and once with
other transitions, is such a pair.

**The views.** The default views show the current line. Each view from
`line_records` to `line_dispatches` in the table carries `root_id`, `seq`,
`state`, `history`, and `observation`, the number of the version a row
comes from; rows of one version share it.

| View | One row per | Its other columns |
| --- | --- | --- |
| `current_line` | Seq from 1 to the last seq, archived or not | `history`, `state` only. |
| `line_records` | Journal record | `at_utc`, `kind`, `actor`, `request_id`, `payload` (the record's JSON text), `archived_at`. |
| `line_transitions` | Transition a record made | `n`, `job`, `transition`, `entry` (JSON text). |
| `line_run_openings` | Run a record opened | `n`, `job`, `run_number`, `rev`, the configuration revision the run opened under. |
| `line_record_owners` | Record that belongs to a job's run | `job`, `run_number`; NULL for a record of no named run. |
| `line_configurations` | Record that applied a configuration | `rev`, `digest`, `binding` (`bound`, or `the live store holds no such rev`), `data`, the configuration's text. The configuration is bound to the record's own version, not joined across seqs, so a seq that is `line not known` shows each version's own configuration, marked. |
| `line_dispatches` | Dispatch | `at_utc`, `job`, `run_number`, `attempt`, and the outcome of the attempt: `outcome_seq`, `outcome_at`, `outcome`, `outcome_payload`. The outcome is NULL while the archive holds none on the current line, and when the dispatch, the outcome or any seq between them is `line not known`. An attempt with two outcome records has a row for each. |
| `line_not_known` | Seq that reads `line not known` | None beyond `root_id` and `seq`. |
| `line_ranges` | History of the current line | `history`, `first_seq`, `last_seq`: the seqs it holds. `history` NULL holds the seqs before every history. |

The all-rows views show everything stored, with its history and no state:
`all_records` (with `transitions`, `run_openings` and `record_owners`, each
`no claim`, `empty` or a set digest, and the configuration columns),
`all_transitions`, `all_run_openings`, `all_record_owners`,
`all_configurations` (every stored configuration, with each record that
applied it) and `all_lists` (every recorded version of the history list).
`coverage` says which seqs are `covered`, `never received` or `expired`.
`counted_observations` and `line_observations` are the views the others
build on.

The views compute the current line at each query. A query over a whole
view reads every archived row, which takes seconds on a million rows; a
query of the `line_` views on a `seq` range is quick. `line_dispatches`
reads every outcome of the current line in each query. `current_line`
starts at seq 1 at each query, and SQLite does not start it at a seq a
query names: a lookup of one seq there walks to the last seq. For one seq,
read its history and its state this way:

```sh
sqlite3 -readonly ARCHIVE "SELECT r.root_id, r.history,
  coalesce((SELECT min(o.state) FROM line_observations AS o
    WHERE o.root_id = r.root_id AND o.seq = 1234), 'not archived') AS state
  FROM line_ranges AS r WHERE 1234 BETWEEN r.first_seq AND r.last_seq"
```

The views and the worked queries need SQLite's JSON functions. The
SQLite of EL8 (3.26) has them.

**Worked queries.** `ARCHIVE` is the archive's path, for example
`/srv/dsl41/runs/nightbank/archive.db`. Times are UTC, as the engine stores
them. `json_extract` reads a field of a JSON column. Each query shows the
root and the state: one archive can hold several roots, and the same job
and run number can occur under each. Add `AND root_id = '...'` to read one.

What ran on a day the archive holds, older than `retention_days`:

```sh
sqlite3 -readonly ARCHIVE "SELECT root_id, seq, state, at_utc, job,
  run_number, outcome, outcome_at FROM line_dispatches
  WHERE at_utc >= '2026-05-04' AND at_utc < '2026-05-05' ORDER BY root_id, seq"
```

The outcome of one run, each attempt with the wrapper's status:

```sh
sqlite3 -readonly ARCHIVE "SELECT root_id, seq, state, attempt,
  outcome_seq, outcome, outcome_payload FROM line_dispatches
  WHERE job = 'nightly_load' AND run_number = 42 ORDER BY root_id, seq"
```

What operators did: each control input with whether it was applied or
refused, each update's `config` records (`intent` when it began, `applied`
when it took effect), and each drain. With access control on, the engine
sets `claimed_actor` to the checked identity of the person who sent the
request, in place of what the client sent. With access control off, it is
the name the client gave, not a checked identity, and it can be absent.
The journal does not name who asked for a drain.

```sh
sqlite3 -readonly ARCHIVE "SELECT r.root_id, r.seq, r.state, r.at_utc, r.kind,
  CASE r.kind WHEN 'input' THEN json_extract(r.payload, '$.claimed_actor')
    WHEN 'config' THEN r.actor END AS claimed_actor,
  json_extract(d.payload, '$.decision') AS decision,
  json_extract(d.payload, '$.reason') AS reason, r.payload
  FROM line_records AS r LEFT JOIN line_records AS d
  ON r.kind = 'input' AND r.state = 'on line' AND d.state = 'on line'
  AND d.root_id = r.root_id AND d.seq = r.seq + 1 AND d.kind = 'decision'
  WHERE (r.kind = 'input' AND r.actor = 'control') OR r.kind = 'config'
  OR (r.kind = 'engine' AND json_extract(r.payload, '$.event') = 'draining')
  ORDER BY r.root_id, r.seq"
```

The definition a run had. The opening gives the revision the run opened
under; the configuration is the one the record that applied that revision
is bound to, on the current line, with no seq between them that is `line
not known`. When there is no such record (it is still in the live store, it
expired, or its seq is not known), the archive does not know the
definition: `digest` is NULL. Do not pick one from `all_configurations` by
`rev`: after a restore one revision can have two bodies. A later attempt
of the run can be dispatched under a newer revision: its dispatch record's
`rev`, read with `json_extract(payload, '$.rev')` from `line_records`.

```sh
sqlite3 -readonly ARCHIVE "SELECT o.root_id, o.seq, o.state, o.rev,
  c.seq AS applied_seq, c.digest, c.data FROM line_run_openings AS o
  LEFT JOIN line_configurations AS c ON o.state = 'on line'
  AND c.state = 'on line' AND c.root_id = o.root_id AND c.rev = o.rev
  AND c.seq < o.seq AND NOT EXISTS (SELECT 1 FROM line_not_known AS u
    WHERE u.root_id = o.root_id AND u.seq > c.seq AND u.seq < o.seq)
  WHERE o.job = 'nightly_load' AND o.run_number = 42"
```

The records of a line the live store left: each record stored under
another history than the one that holds its seq, and each above the last
seq. `state` is the seq's state on the current line, NULL above the last
seq. Two records under one history, as a failed start leaves them, are
not told apart here; the default views show them at a seq that is `line
not known`.

```sh
sqlite3 -readonly ARCHIVE "SELECT a.root_id, a.seq, a.history,
  coalesce((SELECT min(o.state) FROM line_observations AS o
    WHERE o.root_id = a.root_id AND o.seq BETWEEN a.seq AND a.seq),
    'not archived') AS state,
  a.at_utc, a.kind FROM line_ranges AS r CROSS JOIN all_records AS a
  ON a.root_id = r.root_id AND a.seq >= r.first_seq AND a.seq <= r.last_seq
  WHERE a.history IS NOT r.history
  UNION ALL SELECT a.root_id, a.seq, a.history, NULL, a.at_utc, a.kind
  FROM lists AS l CROSS JOIN all_records AS a
  ON a.root_id = l.root_id AND a.seq > l.last_seq
  WHERE l.version = (SELECT max(m.version) FROM lists AS m
    WHERE m.root_id = l.root_id) ORDER BY 1, 2"
```

**What the archive does not promise.**

- A line's label is as good as the live store's history list (DL-536). The
  engine writes a start's history row best-effort: a start whose row could
  not be written appends its records under the earlier history. Where that
  leaves two versions of a seq, the seq reads `line not known`. Where it
  leaves one, the label is wrong and nothing shows it. Every row is kept
  either way.
- A root begun under a build that recorded no histories gets them at its
  first start under a build that does: each retained `engine started`
  record begins one, the first from seq 1. Only a root with no retained
  `engine started` record has seqs that no history holds, and those read
  `line not known`.
- Damage to an archived row is found when the row is next read for a
  batch, not before. There is no background check.
- The archive takes about one and a half to two times the space of the
  same rows keyed by seq alone; where in that range depends on the
  workload.

**Status.** `dsl41 query status --json` carries `archive`. `state` is
`working`, `stopped` with its `reason`, or `off`. `verified_through` is the
highest live seq this engine run has verified. `histories` is the count of
the root's histories the archive holds, and `last_seq` the last seq of the
newest history list it recorded, after the last round that completed. Each
is `null` until the engine knows it
([control protocol](control-protocol.md)).

The engine opens the archive for each snapshot round, holds a lock on
`<file>.lock` beside the database's real path while the round runs, and
closes it at the round's end. Each round opens the configured path again,
so a stop whose cause is removed ends at the next round, with no restart.

**A stopped archive** stops pruning. The live store then grows, and request
ids below the bound stay taken: a retry is answered from the kept
decision. The engine writes one line when the archive stops, with the
reason, and one when it works again. What to do, by reason:

| Reason | Do |
| --- | --- |
| `another engine holds the archive, or an operator holds its lock` | Find the holder: as root, `lsof <file>.lock` names the process. Give each engine its own `archive_path`; two names of one file share the lock. Release a lock an operator's tool holds. |
| `<path>: mode <mode> lets group or others read or write it; ...` | `chmod 600` the file and its `-wal` and `-shm`, as the service account. |
| `<path> is not a regular file` | Point `archive_path` at a regular file, or at a path where none exists yet. |
| `<path>: the archive file has <n> hard links; ...` | Remove the other names of the file. |
| `<path> is not a dsl41 archive: it names no layout` | Point `archive_path` at another file, or take the way out below. The engine never writes into this file. |
| `<path>: archive layout <name>; this build writes dsl41-archive-content-1 only` | The file is from another build or an earlier layout. Run the build that wrote it, or take the way out below. |
| `<path>: journal_mode is '<mode>', the archive needs WAL` | A tool changed the file's journal mode. With the engine stopped, run `sqlite3 <path> 'PRAGMA journal_mode=WAL'`. |
| `<what> is not UTF-8 text and cannot be archived: ...` | A live record the archive cannot hold verbatim. Report it as a defect; pruning waits. |
| `the live configuration rev <n> does not match its digest` | The live store's configuration row is damaged. Report it as a defect; pruning waits. |
| `the archive's <item> is damaged: expected digest <d>, found <d>, for root <root>; pruning stops` | The archive file is damaged. Take the way out below. |
| `the archive's <item> cannot be read as text, for root <root>; pruning stops` | As above. |
| `the archive's <table> set at seq <n> has rows and no header, for root <root>; pruning stops` | As above. |
| `the archive does not read back as the batch's image <inside the transaction, after the commit>: its <part> differ, for root <root>; pruning stops` | A batch the engine just wrote did not read back as written. Check the disk and the file system; the next round tries again. If it stops again, take the way out below. |
| `a verified range begins at seq <n>, above the live store's first seq <m>: ...` | An engine defect. Report it; the next round verifies again. |
| `DatabaseError: file is not a database` | `archive_path` names a file that is not an SQLite database. Point `archive_path` at another file, or take the way out below. |
| `<error type>: <text>`, an SQLite, disk or permission error | Fix the cause the text names; the next round tries again. `OperationalError: database is locked` is a reader writing to the file. |

**The way out for a damaged or unusable file**, with the engine stopped.
Nothing in the engine repairs an archive file. Stop the engine. Find the
database file `archive_path` resolves to: it may be a symbolic link, and
then the file it points to is the one to move. Move that file, and any
`-wal` and `-shm` beside that file, together to one new name in the same
directory: `<name>-kept-<date>`, with `-wal` and `-shm` after it. Keep them
with the audit records: they hold the only copies of what was pruned
before. Start the engine. Its first
round makes a new archive and records a gap over what that file never
received, and pruning goes on.

Each engine expires only its own root's copies, under its current
`archive_days`, and only rows its live store no longer holds. A root that
no engine runs any more (one a renewal replaced) keeps its copies until an
operator removes them. As the service account, with its `root_id` from the
`roots` table:

```sh
sqlite3 /srv/dsl41/runs/nightbank/archive.db "BEGIN;
DELETE FROM observations WHERE root_id = 'ROOT'; DELETE FROM records WHERE root_id = 'ROOT';
DELETE FROM sets WHERE root_id = 'ROOT'; DELETE FROM transitions WHERE root_id = 'ROOT';
DELETE FROM job_runs WHERE root_id = 'ROOT'; DELETE FROM job_records WHERE root_id = 'ROOT';
DELETE FROM configs WHERE root_id = 'ROOT'; DELETE FROM list_entries WHERE root_id = 'ROOT';
DELETE FROM lists WHERE root_id = 'ROOT'; DELETE FROM histories WHERE root_id = 'ROOT';
DELETE FROM coverage WHERE root_id = 'ROOT'; DELETE FROM roots WHERE root_id = 'ROOT';
COMMIT; PRAGMA incremental_vacuum;"
```

A round that meets this write stops once and goes on at the next.

An older build, or a run with `archive_days = 0`, prunes without copying.
So does an engine built without `dsl41 run`, such as a test harness; its
`status` shows `archive.state` as `off` with the reason.
The rows it pruned show as `never received` in the archive's coverage.

The archive holds the engine's own records: inputs and decisions,
dispatches and outcomes, configurations, the transition rows and the
history index. It does not hold the wrapper's spool evidence under
`runs/`, the file watcher's poll log or job output: those leave with the
live history, removed or moved to `expired_dir`
([Keep expired files](#keep-expired-files)). It does not yet hold
the control socket's access log.

## Drain the engine

A CLI stop of this systemd service triggers its restart policy.
To keep it down, run this step as root:

1. Stop the unit:

   ```sh
   systemctl stop dsl41-engine.service
   ```

   Systemd runs the drain through the `ExecStop` line of the
   [engine unit](../examples/nightbank/deploy/dsl41-engine.service).

For a foreground engine, request the drain directly:

```sh
/opt/dsl41/releases/<version>/bin/dsl41 engine stop --wait \
  --socket /srv/dsl41/runs/nightbank/control.sock
```

The command prints the engine's JSON answer. The engine refuses new
mutations with `engine_draining`, sends TERM to running commands, allows
the grace, then sends KILL where needed. It records how each attempt ended.
Queries remain available during the drain. A second stop moves the drain
deadline to the present; it does not grant more time.

| Default bound | Seconds |
| --- | --- |
| TERM to KILL (`grace_term_s`) | 10 |
| KILL to exit-record grace (`grace_kill_s`) | 5 |
| Engine drain deadline (both graces plus margin) | 20 |
| Unit stop timeout | 45 |

With `--wait`, exit 0 means the engine exited or no engine holds the root.
An already stopped engine prints `engine not running: ...`.
Without `--wait`, exit 0 means it took the stop request.
Exit 2 means refusal or a communication failure; a lost reply can leave
the outcome unknown. Read status before retrying.
Do not retry while status says DRAINING unless you intend to cut the grace:
a second stop moves the deadline to now (`_deadline_now` in
[engine_charts.py](../src/dsl41/engine_charts.py)).

Neither stop exit 0 nor `run` exit 0 proves a clean drain.
Read `engine stopped: ...` in the [unit's journal](#read-the-engines-output),
or in the foreground terminal. The stored record says how the stop ended:

| Record | Meaning |
| --- | --- |
| `clean_shutdown` | Every attempt settled. |
| `unclean_shutdown` | The drain deadline passed with unsettled attempts retained for restart. |
| `shutdown_during_startup` | The operator stopped restart catch-up; the next restart takes the remaining steps. This stop exits 0. |

The shutdown actions in [engine_charts.py](../src/dsl41/engine_charts.py)
(`_clean_shutdown`, `_unclean_shutdown`, `_shutdown_during_startup`) define these
records. `_drop_catch_up` in [runner.py](../src/dsl41/runner.py) drops the
catch-up steps not taken, and the next restart takes them again.

The TUI does not show the drain, its deadline, the stored plan or the
interruption evidence. Use `dsl41 query status` and the exit status of
`dsl41 engine stop` as the workaround. Status exposes the engine state and
attempt evidence; it does not provide a drain deadline or a full stored-plan
view. Once the engine exits, its socket cannot answer status.

## Take a backup for restore

Choose a quiet window. [Draining](#drain-the-engine) can interrupt jobs.
As root, follow these steps for the installed Nightbank service.

1. Stop the unit and keep it stopped until the copy finishes.

   ```sh
   systemctl stop dsl41-engine.service
   ```

   For a foreground engine, use the [direct drain command](#drain-the-engine).

2. Read the [shutdown record](#drain-the-engine) and check independently that
   no engine, wrapper or job writer remains. For this dedicated service
   account, run:

   ```sh
   systemctl show -p ActiveState -p MainPID dsl41-engine.service
   pgrep -a -u dsl41
   ```

   For a drained backup, require `ActiveState=inactive`, `MainPID=0`, and `pgrep` exit 1
   with no output. If the account is shared with other services, identify any
   processes that can still write these files and wait for them to stop.

3. Copy the complete set into a fresh backup directory. This command uses GNU
   `cp` and `flock` from util-linux. Replace `VERSION` with the release
   directory named by the installed unit's `ExecStart`.

   ```sh
   (
     set -eu
     install -d -m 0700 /srv/backups
     backup=$(mktemp -d /srv/backups/nightbank.XXXXXXXX)
     cd /
     flock -n -x 9
     cp -a --parents srv/dsl41/runs/nightbank etc/dsl41 opt/nightbank \
       srv/dsl41/nightbank/night opt/dsl41/releases/VERSION \
       etc/systemd/system/dsl41-engine.service \
       etc/systemd/system/dsl41-engine-down.service "$backup"
     printf 'Backup complete: %s\n' "$backup"
   ) 9</srv/dsl41/runs/nightbank/leader.lock
   ```

   The leader lock excludes another engine during the copy. Keep the whole
   run root, including the store, sidecars, attempts and plans, together.
   The Nightbank copy also keeps the JIL, properties, worker scripts, job
   profile and incident settings at their original relative paths. For a
   site with different paths, include its required inputs, access map, hook,
   unit drop-ins and runtime dependencies. Keep business data and external
   job output under the site's backup policy.

4. After a successful copy, restart the service when ready. For a package
   upgrade, leave it stopped until the new runtime is ready.

   ```sh
   systemctl start dsl41-engine.service
   ```

If the engine must remain up, use an atomic filesystem snapshot of the whole
run root. A restore from a live snapshot recovers as after a crash. Retain
the matching config, release and external inputs from step 3 with the snapshot.

To restore on an existing host, stop the service and repeat the no-writer
check in step 2. On a replacement host, prepare the filesystems and recreate
the service account with its original UID and GID. Keep the service stopped.
Move any damaged root aside to an unused path on the same filesystem and keep
it for diagnosis. Restore the complete backup tree to the original paths,
keeping ownership and modes. Restore the run root as one directory at its
original, now vacant path. Provide the same Python build the saved release venv uses at the
same path. Restore any unit drop-ins. If you restore unit files, run
`systemctl daemon-reload`. On a replacement host, run
`systemctl enable dsl41-engine.service` if the service should start at boot. Follow
[recovery](#recover-after-the-engine-or-box-dies) to start and check the engine.

A restore of the live store to an older copy beside a newer archive (an
`archive_path` outside the run root, or an `archive.db` kept from after the
backup) needs no step for the archive: its first rounds record the restored
line's history list, and the archive keeps both lines
([Keep the archive](#keep-the-archive)).

## Reload a changed estate

To decide who may apply a change, when and how to go back, see
[Operate an estate](operations.md).

Use the engine's socket. Every engine command takes `-S`/`--socket`, with
`DSL41_SOCKET` as its default (DL-459, DL-463).

```sh
dsl41 reread -S /path/to/control.sock
dsl41 update -S /path/to/control.sock --wait
```

Both commands default to focused text. Add `--style neutral` for per-job
rows over the same prepared data (DL-482). Reread groups identical job changes
and changed references once, with affected job counts. Labels use model field
names without structural containers: `machine`, `command`, `condition`.
Shared settings have no job list. Environment edits show variable names only.
Long values end in `[cut]`; large lists and condition trees say `changed`.
A 1 MiB byte budget bounds the whole difference field; omitted differences
are counted as `N more differences not shown`. The `read from:` line is capped
at 240 characters and marks a cut.
The footer offers `update` only when the shared candidate check passes and
the engine is neither draining nor already preparing or stopping an update.
Otherwise it explains the refusal while retaining the passive difference.
When update is allowed, source-only edits say that update still applies the estate files. A header
saying no job, setting or estate file changed reports those comparisons;
only update's `unchanged` answer proves no application (DL-478).
Update prints `stop requested for N running jobs: NAMES` for a nonempty
stop set, whether pending or applied. An empty set adds no stop line.
Per-job outcome lines come from the application answer. The answer does not
report box outcomes; use the box rules below.
Its applied and unchanged lines both name the catalog revision and hash.

Reread only reports current launch-config and glob inputs: added, changed,
removed and unchanged jobs, findings, and what update would stop, rerun, hold
or remove. A running box does not refuse a preview; when the update would be
refused `tree_active`, reread prints one line that names the jobs in the way.
A job the update retypes is shown as replaced by a new job of its name.
Broken input explains why no diff is available. Exit 0 means no running stop
and no forecast `tree_active` refusal, 1 means running stops or that refusal,
and 2 means no diff or failure to reach the engine. A finding that would make
update refuse at its preparation (a blocking lint finding, changed host launch
settings, a preflight error) is printed as `update would refuse: ...` and does
not change the exit status; reread then forecasts no `tree_active`, since
update refuses first.

A kept job whose only edit is its `status:` line is counted apart, as
`status line only N`, in both commands. A box whose own edit is its `status:`
line and whose members change is counted as changed (membership) by `reread`
and under `status line only` by `update`; each command's counts add up. Its row shows the line's old and new value and says the
edit itself sets no flag. Update stops nothing and changes no flag or status for
that edit; use `sendevent` to hold or release the job. For a line to or from
SUCCESS, FAILURE or TERMINATED the row says that the line is a seed and the
update leaves the live job as it is. A flag is changed with its own `sendevent`
event and a status with `sendevent CHANGE_STATUS`. Either acts on the job as it
is now and can start it or the jobs that wait on it, so look at the job's
current state first (DL-561). Such a job inside a box whose
members the update changes counts as any member of that box (DL-533, DL-559).

Status in the CLI and TUI annotates changed and removed jobs with this forecast.
It describes reread time. Another reread replaces it when the revision it read
is still the applied one (DL-521); failed reread, applied update and engine
restart clear it. It is held only in engine memory.

Update rereads the files itself. Edits made after preview are included.
It prints a request ID before sending. Use `--request-id ID` only to look up
that original work after an unknown outcome. An unknown or expired ID submits
nothing. Do not blindly submit a fresh update after a lost reply.
A no-change update retains no request ID; a later lookup reports not retained (DL-478).
Both update forms print `nothing changed: catalog revision N (H)` and exit 0
when the prepared configuration equals the applied configuration and live profile (DL-478).
Without `--wait`, admission does not confirm application; use the printed
same-ID check command. With `--wait`, exit 0 means applied or unchanged and
1 means still pending. Exit 2 means
refusal/failure. No token or stop-consent argument is used.

Affected ordinary jobs stop through TERM, grace and KILL. A retained idempotent
job gets another attempt of the same run under the new definition; a marked
job is held; a removed job gets no replacement (DL-424, DL-432).
An update never restarts a box (DL-559). An update that changes box
membership (a job added, removed, moved to another box or retyped that is a
box or sits in one) is applied only when it stops nothing and every job of the
touched box trees is quiet: not QUE_WAIT, STARTING or RUNNING, and with no
process left. The touched trees are the outermost box or job holding each of
those jobs, before and after the edit, with everything in them. Otherwise update prints
`update: refused tree_active: NAMES` and exits 2; nothing is written. To apply
such a change to a busy tree, stop the named jobs (or wait for them), run
update, then start the box again. One busy job refuses the whole update. A job
outside every box is added, removed or retyped as before, with its stop.

Upgrade note: an update that restarted a running box in 2.0.0 is now refused.
An update the older build left stopping with box roots in its intent is
completed by this build as that build would have done it. A crash after its
`config applied` can leave such a box stopped: before force-starting it, put
its marked non-INACTIVE members on hold, including members that finished
earlier (DL-463, DL-489).

Preparation runs under the engine account and startup cwd/environment, in a
separate interpreter. Its deadline is 120 seconds; cancellation terminates,
allows two seconds, then kills and reaps. The client allows preparation and
update application time. Findings retain their actual source diagnostics.
Artifacts stay under the engine's actual run root.
Host-setting edits require restart before update. Restart restores the applied
capture, but future reload uses the config of that latest engine start.
The drift flag and startup pending count still compare against the applied
capture's inputs, which may differ from that launch file.
JIL-only engines refuse reload: drain and start a fresh run root to change
those definitions; the old root's state is not migrated.

## Upgrade the engine

This section upgrades one 2.x release to another. A 1.x site does not
upgrade in place: 2.0.0 refuses a 1.x run root and has no supervisor.
Under 1.x, stopping the engine unit stops no job, so drain and retire the
1.x estate as the 1.13.0 deployment runbook says under "Retiring an
estate", and remove its two units. Record the held and iced jobs first:
a fresh root does not know them, so a job held under 1.x would run on
schedule. Then install 2.0.0 as a new site, following
[Install the package and unit](#install-the-package-and-unit), on an empty
run root. Write a `non-idempotent.toml` that names every job that must not
rerun after an engine death, and hold again the jobs you recorded, before
the first start. Keep 1.13.0 at hand to read the old root's history. From
2.0.0 on, stopping the engine unit stops every running job.

Change the engine and every client together (DL-498).
Restart terminal UIs and the served UI from the selected build.
An old UI is refused by name and must be restarted.

Only select a release documented as compatible with the existing store and
state-machine version. The stored active estate must match what that release
can interpret. Compatibility is allocated per release; arbitrary upgrades
and rollbacks are not supported.
Finish an open update (`status.update` absent) before going back to an older
build: an older build can refuse to resume an update a newer one admitted (DL-533).

A root created through legacy files or a manifest has no main-config capture.
It cannot resume through `--config`. Keep its legacy invocation or select a
fresh root for the main-config service. There is no root migration command.

1. Drain the engine with `systemctl stop dsl41-engine.service`.
2. Install the new release in its own `/opt/dsl41/releases/<version>` directory.
3. Select that release in the engine's `ExecStart` and `ExecStop` and the
   failure unit's helper `ExecStart`.
4. Run `systemctl daemon-reload`.
5. Run `systemctl start dsl41-engine.service` and check status.

Application inputs and reviewed updates are independent of that selection.

The first start of a release with DL-557 checks the run root at its first
snapshot. That first round removes, over several steps, every finished
run's files and every plan older, by a day, than the oldest record the live
store keeps, or moves them to `expired_dir` when it is set. It keeps what
is always kept: open runs, each job's last run, and plans while an update
is prepared or stops. Set `expired_dir` before that start to keep the
files; the archive holds neither attempt records nor job output.

A release with DL-561 needs a fresh run root. It reads IR-F 0.4 only, and a
root an earlier release wrote stores its configuration at IR-F 0.3. Its first
start on such a root refuses, root unchanged, with `IR-F version 0.3 is retired
(DL-561)` and the remedy. `history`, `runs` and the other readers of that root
refuse the same way. Drain the root on the release that wrote it, keep that
release to read the old root's history, and start the new release on a fresh
run root with the [fresh-root steps](#upgrade-the-engine) below. Record the
held and iced jobs first: a fresh root does not know them. The owner ruled
this for now; it may change.

For one package that replaces the engine at the unit's fixed executable path,
use the following variant (DL-474). It also covers a package that carries estate
files. The unit's `ExecStart` must use `run --config`.
Replace `ENGINE` in every command with the absolute executable named by
the installed unit's `ExecStart`. Replace `PACKAGE.rpm` with the new package.
The repo's reference RPM prepares a runtime without selecting it (DL-389).
With that package, select the new runtime after installation and before starting;
on rollback, select the old runtime again. Installing or downgrading that RPM alone
does not select the runtime.

Choose a quiet window: restart serves the stored estate until an update applies.
Keep the release that wrote the root and a whole-root backup available.
Do not replace engine files while the service runs.
This variant follows the same [engine and client rollout rule](#upgrade-the-engine).

1. As root, [stop the service, check for writers and take the backup](#take-a-backup-for-restore).
   Use the installed runtime and site paths in the backup command. Leave the
   service stopped. Retain the old RPM and record the selected runtime. If the
   runtime path is a symlink, preserve its target as well as the link.

2. As root, install the package. Select its runtime if the package requires it:

   ```sh
   rpm -Uvh PACKAGE.rpm
   ```

   For the reference RPM's default prefix, the unit's executable must follow
   `/opt/dsl41/.venv/bin/dsl41`. As root, replace `<version>-<release>` with
   the installed delivery directory and use `ui` instead of `base` if the RPM
   was built with that profile. Use the package's system Python 3.12; these
   commands show its default path. Stop on any failure. The `.venv.next` path
   must be unused.

   ```sh
   delivery=/opt/dsl41/delivery/<version>-<release>
   profile=base
   candidate="$(/usr/bin/python3.12 -I "$delivery/tools/bundle.py" candidate "$delivery" /opt/dsl41 "$profile")"
   /usr/bin/python3.12 -I "$delivery/tools/bundle.py" verify "$delivery" "$candidate" "$profile"
   ln -s ".venvs/$(basename "$candidate")" /opt/dsl41/.venv.next
   mv -Tf /opt/dsl41/.venv.next /opt/dsl41/.venv
   ```

   The atomic replacement is manual: the repo has no activation command or
   regression test for replacing an existing active link.
   On rollback, reinstall the old RPM first: an upgrade removes its delivery
   directory. Then repeat these commands with the old delivery and its profile.

3. As root, start the service:

   ```sh
   systemctl start dsl41-engine.service
   ```

4. As the service user, repeat this query until it prints `RUNNING`.
   It needs `jq`. A successful query can still report `STARTING` during catch-up.
   If startup refuses, read the journal and follow the refusal guidance below.

   ```sh
   sudo -u dsl41 ENGINE query status --json \
     --socket /srv/dsl41/runs/nightbank/control.sock | jq -r .engine
   ```

5. Only when the package changed estate files, preview them as the service user:

   ```sh
   sudo -u dsl41 ENGINE reread \
     --socket /srv/dsl41/runs/nightbank/control.sock
   ```

   Restart keeps the applied revision. The TUI's `spec drift: estate files changed
   on disk` line directs reread and update. Review the preview's findings and
   affected jobs before applying it. Then run:

   ```sh
   sudo -u dsl41 ENGINE update --wait \
     --socket /srv/dsl41/runs/nightbank/control.sock
   ```

   Exit 0 prints `applied: catalog revision N (H)` or
   `nothing changed: catalog revision N (H)`;
   status reports that applied
   `config_rev`, and the drift indication clears when disk inputs match it.
   Completed statuses and existing holds are kept for retained jobs whose runs
   the update does not restart. Affected running jobs follow the
   [update interruption and box rules](#reload-a-changed-estate).

A state-version refusal leaves the root unchanged. Keep it and run the release
that wrote it. For a fixed-path replacement package, stop the unit, reinstall that
release with `rpm -Uvh --oldpackage COMPATIBLE_PACKAGE.rpm`, select its runtime
if required, then start and repeat the `RUNNING` check. A drain and snapshot do
not convert state (DL-424).

A unit that launches JIL files without a main config refuses a changed estate
at start with a catalog-hash message. For any refusal that names a fresh run
root, use the fresh-root steps below if choosing that reset.

A fresh root is a deliberate reset. It loses the active engine's:

- Job statuses.
- Hold, ice and noexec flags.
- Run and attempt identities and numbers.
- Exit codes.
- Timestamps.
- History.
- Captured invocations and log associations.
- Configuration revisions.
- Interruption evidence.
- Pending updates and request IDs.
- Globals.
- Scheduler frontier and deduplication.
- Armed latches.
- Timers.
- Capacity reservations.

The archived root keeps that evidence. External job effects remain.
Do not delete only `engine.db` or edit its version metadata.

To choose a fresh root, use an unused archive path. As root:

1. Keep the service stopped:

   ```sh
   systemctl stop dsl41-engine.service
   ```

2. Archive the whole root without nesting it inside an existing directory:

   ```sh
   mv -T /srv/dsl41/runs/nightbank /srv/dsl41/runs/nightbank-old
   ```

3. Create the replacement root and preserve its stop graces, snapshot interval
   and retention settings:

   ```sh
   install -d -o dsl41 -g dsl41 -m 0700 /srv/dsl41/runs/nightbank
   cp -a /srv/dsl41/runs/nightbank-old/engine.toml /srv/dsl41/runs/nightbank/
   ```

4. Clear any start limit and start the service:

   ```sh
   systemctl reset-failed dsl41-engine.service
   systemctl start dsl41-engine.service
   ```

   Repeat the `RUNNING` check above. The engine starts from the current configured
   estate with fresh state.

A `CHANGE_STATUS` can start dependent jobs, including through a member's change
to its box's derived status ([SEM-15](autosys-semantics.md#sem-15--member-status-changes-can-ripple-upward-vc)).
Never use a blanket hold-and-release loop as a neutral status restore:
releasing holds can start eligible jobs.
Per-job `sendevent CHANGE_STATUS --job JOB --status STATUS --exit-code CODE`
and `sendevent ON_HOLD --job JOB` can set a chosen status and hold flag.
Run them as `sudo -u dsl41 ENGINE` with
`--socket /srv/dsl41/runs/nightbank/control.sock`.
These are new events; they do not recover history or scheduler state.

## Recover after the engine or box dies

Keep the durable run root, including `engine.db`, `runs/` and `plans/`.
If a restore is needed, follow the [backup and restore checklist](#take-a-backup-for-restore)
before starting. Startup refuses a missing store with surviving runtime
evidence (DL-443). Preserve the damaged root and recover the complete backup.
Restore business data separately and account for effects already produced by jobs.
Startup validation uses Python's temporary directory selection (`TMPDIR`
when usable). Allow space for one complete
copy of `engine.db` and its present `-wal`, `-shm` and `-journal` files,
plus filesystem overhead. Validation can run more than once; copies are
removed after each pass. An uncatchable kill can leave a private
`dsl41-inspect-*` directory there. A copy or space failure refuses with
exit 2 and names the source evidence; it leaves that evidence unchanged.
For the service, run this step as root:

1. Start the same unit on that root:

   ```sh
   systemctl start dsl41-engine.service
   ```

   For a foreground engine, use the [start command](#start-the-estate) instead.

An engine death ends its running attempts; restart does not adopt them
as continuing jobs. After a box reboot, a changed boot identity proves
the old processes did not survive.

The engine restores its snapshot and later records, then reads each open
attempt's records once. A recorded natural completion keeps its actual
result. Without an exit record, the outcome is `unobservable` with cause
`engine_death`, unless the attempt already had a stop cause.
Restart does not wait for an old wrapper or a late exit record.
When the wrapper records termination after losing its parent, the attempt
has `kind: stopped` and `cause: lifeline`. See the
[wrapper's parent-loss record](../src/dsl41/runner_wrapper.py) (step 6 of its
module docstring) and its outcome mapping, `outcome_of` in
[engine_charts.py](../src/dsl41/engine_charts.py).

The interruption rule reruns an interrupted job as a new attempt of the
same run, holds a job marked in `non-idempotent.toml` or at the attempt limit,
or takes no action
when the run already ended or the job left the catalog.
The attempt limit (`MAX_ATTEMPTS_PER_RUN` in
[runner_attempts.py](../src/dsl41/runner_attempts.py)) is:

5 plus the CMD job's applied `n_retrys` count; never more than 25 attempts per run (DL-485).

An interruption at that limit or above holds even an unmarked job.
Its `interrupted` evidence carries that applied limit, as set by the
interruption rule (`follow_up_plan` in
[runner_attempts.py](../src/dsl41/runner_attempts.py)).
See the [job author's interruption contract](authoring.md#62-write-every-job-to-survive-interruption) before
deciding which jobs may rerun.
Unreadable or mismatched attempt records hold affected jobs with evidence.
Residual processes are cleaned up by the live engine; uncertain process
readings keep cleanup held and are checked again.

Status remains STARTING during catch-up. The engine takes up file watches,
owed plans, pending starts and any interrupted update before the final
time jump. It then admits deferred dispatches and announces startup.
A stop during catch-up leaves the remaining work for the next restart.

Read the missed-start report in [engine start output](#read-the-engines-output)
before choosing catch-up (DL-476). Its `from` is the largest durable record
timestamp, or the snapshot's saved `last_at` when no later records exist.
It is clamped to the earliest reported tick if that tick is earlier.
After an unclean stop, the time of death is unknown; an idle engine's last
record can precede its death by hours. `to` is the captured scheduler sweep
cutoff: engine time at the restart, including the latest retained store stamp
(DL-493).
Missed ticks are recorded and never fired late. For a required manual start,
check downstream effects and follow the
[missed-tick intervention](incident-triage.md#the-job-has-not-started).

A refused resume leaves the unit down on exit 2. Read its named refusal;
do not erase the root to bypass it. The restart checks the lock, store
version and identity, that the store holds a journal record, clock domain and
any pending update's captured plan.
A missing or unreadable interrupted-update plan refuses before a journal
write or process signal.

The refusal `the store holds no journal record` means the first start of the
root died before it finished. If the root has never run a job, stop the unit,
remove `engine.db` with its `-wal` and `-shm` files and start the unit again.
Otherwise restore the store from a backup (DL-504).
A first start that died while the store was being created leaves an empty `engine.db`, and the
next start refuses it with `is not a dsl41 store`. On a root's first start this is the same
case with the same remedy (DL-504).

Main-config restart restores the last applied catalog, profile and job order
from the store. Estate and lint edits remain pending, including invalid
`engine.environment` selections. Restart still reads the selected config
as TOML and requires its supported format. It applies current `as_machine`,
`machine_policy`, access-map, status-hook and `expired_dir` settings; invalid host settings refuse with
exit 2. An omitted access map selects owner-only access. These settings stay
effective while an interrupted update applies its captured plan.
Other runtime-profile fields retain their stored values and checks (DL-437).

Restart runs launch preflight against the stored catalog and current host
settings without applying lint refusal. It does not lower the applied v3
capture or import Python. Captured JIL is parsed only for `spec` views; if the
scanner rejects it, those views return `jil: null` and the engine still runs.
After startup a synchronous source scan reports
`N pending source changes; run dsl41 reread -S SOCKET` once when N is above zero (DL-483).
That count measures source drift, not compiler behavior. After a compiler
fix, restart keeps the applied IR. The next `reread` lists jobs whose lowered
definitions change, even if their sources did not change. `update` applies those
definitions, stopping and rerunning affected running jobs; non-idempotent jobs
follow the existing hold rule (DL-468). An interrupted update still uses its
captured plan; if the new binary rejects that candidate or changes its recorded
job diff, restart refuses before writes or signals. The drift refusal says:
"Finish the update using the binary that began it, then upgrade."
Patch compatibility requires the same state and format versions. Preserving
earlier behavior for absent settings is a release obligation. Recovery cannot
verify it from the retained record.
Legacy file and manifest starts keep their matching-estate checks.
See [runner recovery](runner-design.md#7-journal-and-recovery-e1-prod-grade)
and DL-433 in the [decision log](decision-log.md) for the restart rule.

2. Read the engine state as the service user:

   ```sh
   /opt/dsl41/releases/<version>/bin/dsl41 query status \
     --socket /srv/dsl41/runs/nightbank/control.sock
   ```

   Look for `engine: STARTING` during catch-up, then `engine: RUNNING`.
   A fast restart may already report RUNNING on the first query.
   The status response (`_status` in
   [runner_control.py](../src/dsl41/runner_control.py)) reads the engine
   lifecycle (`engine_lifecycle` in [engine_charts.py](../src/dsl41/engine_charts.py)).

## Exercise the unit before site use

Run these only on a disposable drill host with Podman, Git and uv available,
from the checkout. The scripts create a systemd container and install a
synthetic estate; they do not qualify a production workload.

1. Run the Ubuntu service drill:

   ```sh
   examples/nightbank/deploy/drill-local.sh --image ubuntu all
   ```

2. Run the EL8 service drill:

   ```sh
   examples/nightbank/deploy/drill-local.sh --image el8 all
   ```

The scripts print a result for each row and stop on failure. They cover
install, start, clock, storage, restart, container reboot, destructive restore,
engine kill, drain, reload, process leaks, restart faults, startup refusal,
start limit, malformed TOML and executable upgrade. `drill-steps.sh --list`
prints the rows in order.
The `clock` row checks the installed time-sync waiters and the serving Python
interpreter (`clock-drill.sh`, `clock-unsynced.c`). The `storage` row checks that a
frozen engine still excludes a second one, that a full filesystem gives an
`UNKNOWN` outcome, and exact recovery after it (`storage_probe.py`). The `restart-faults` row runs the installed
restart fault orderings (`restart_faults.py`).
The refusal row requires `engine_refused` and `engine_down`; crash-loop
exhaustion requires `engine_down`; normal stop requires no `engine_down`.
Malformed TOML requires the independent helper's `engine_down` and no
`engine_refused`. The rows check delivered payloads, not only journal text.
The `refusal` row verifies that a pending source edit leaves the stored
revision active after restart, then tests a foreign store version refusal.
The `leak` row checks that a restart TERMs a job's leftover process through
the cookie scan. A container reboot retains the underlying kernel boot ID;
the runtime may replace the container-visible ID. The drill records both
boundaries separately and does not qualify an actual host reboot.

[DRILL.md](../examples/nightbank/deploy/DRILL.md) supplies maintainer setup
and diagnostics; [drill-steps.sh](../examples/nightbank/deploy/drill-steps.sh)
defines the executable row checks. Use the row behavior above when reading
its refusal, manifest and upgrade descriptions. The scripts install a
main config and versioned executable, independently of application inputs.
