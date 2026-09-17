# Simulation coverage register

This register lists every behaviour the simulation side of dsl41 can meet
and says what it does with it. The compiler side already fails closed:
lowering refuses unknown attributes (DL-07), the UC backend refuses R-rows
and records every A-row assumption, and `minify` refuses what it cannot
classify. The simulation side carried pinned defaults with `PENDING` markers
and unmarked choices. This register closes that gap by classification, not
by frequency. If a construct can occur, it is modelled, refused, or listed
here as an explicit assumption.

Each row has one of four classes.

- `supported`: the behaviour is modelled; the citation says how.
- `provisional`: a pinned default under an open question. The label names
  the question. `marker` means the code carries a `PENDING` marker for it.
- `refused`: the behaviour is refused loudly; the citation is the refusing
  site.
- `passthrough`: the input is carried verbatim; the effect column names what
  is not implemented.

Two readings are distinct. A static finding is exposure: the input may make
the behaviour apply. A runtime finding is an application: it did apply.
Rows here describe exposure. Collectors that record applications arrive
slice by slice; a row whose detector reads `none` has scope fixtures but no
collector yet, and no run output may claim it was assessed.

Three limits hold for every row. A replay against recorded history proves
agreement on that history only. A probe result binds to the scheduler and
agent version, platform, configuration, and preconditions it ran under. A
bounded search proves absence within its bound only; rows with a bound say
so.

The rows are data in `src/dsl41/simulation_register_rows.py`. The table
below is generated from them by `scripts/render_simulation_coverage.py`,
and `tests/test_simulation_register.py` fails when the two differ. The same
test derives the domain of every surface from the code's own inventories,
so a new attribute, token, event kind, status, profile field, adapter or
wrapper outcome, event provenance, trace marker, preflight code, calendar
serialization, closed Literal alternative, or `PENDING` marker site without
a row fails the suite.

<!-- register:begin -->

### statement

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| statement:calendar | supported | SEM-36, DL-36 | - | generic | a standard calendar's date rows become the day set holcal and run_calendar read |
| statement:cycle | supported | SEM-39 | - | generic | a cycle's start_date/end_date pairs become the periods cycle-scoped tokens count in |
| statement:delete_blob | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses delete_blob: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:delete_box | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses delete_box: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:delete_connectionprofile | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses delete_connectionprofile: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:delete_glob | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses delete_glob: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:delete_global | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses delete_global: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:delete_job | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses delete_job: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:delete_job_type | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses delete_job_type: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:delete_machine | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses delete_machine: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:delete_monbro | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses delete_monbro: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:delete_resource | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses delete_resource: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:delete_xinst | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses delete_xinst: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:ext_calendar | supported | SEM-36, DL-60 | - | generic | the Manage Calendars spelling of extended_calendar, accepted as input leniency |
| statement:extended_calendar | supported | SEM-36, DL-36 | - | generic | an extended calendar's rules compile to a day generator |
| statement:insert_blob | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses insert_blob: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:insert_connectionprofile | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses insert_connectionprofile: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:insert_glob | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses insert_glob: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:insert_global | supported | SEM-08 | - | generic | a global variable's declared value seeds the oracle's global store |
| statement:insert_job | supported | DL-29 | - | generic | a job definition is lowered whole: linkage, semantics, schedule and exec spec |
| statement:insert_job_type | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses insert_job_type: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:insert_machine | supported | DL-49 | - | generic | a machine definition is lowered to its type, node_name and pool members |
| statement:insert_monbro | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses insert_monbro: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:insert_resource | supported | DL-50 | - | generic | a resource definition sizes one capacity bucket |
| statement:insert_xinst | supported | SEM-07 | - | generic | an external-instance definition is carried; cross-instance atoms read it by name |
| statement:override_job | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses override_job: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:rename_job | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses rename_job: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:update_connectionprofile | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses update_connectionprofile: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:update_job | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses update_job: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:update_job_type | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses update_job_type: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:update_machine | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses update_machine: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:update_monbro | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses update_monbro: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:update_resource | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses update_resource: merging and out-of-scope object classes are semantics this compiler does not model |
| statement:update_xinst | refused | ir._Lowerer.run, DL-29 | - | generic | lowering refuses update_xinst: merging and out-of-scope object classes are semantics this compiler does not model |

### job_attr

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| job_attr:FREE | supported | DL-50 | - | generic | overrides the resource's release default for this one request |
| job_attr:QUANTITY | supported | DL-21, DL-50 | - | generic | the units one resources group demands; a group without it is refused |
| job_attr:alarm_if_fail | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:alarm_if_terminated | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:application | passthrough | dossier ss5, DL-32 | - | generic | the application tag is carried; nothing schedules or reports by it |
| job_attr:auto_delete | passthrough | dossier ss5, DL-32 | - | generic | the definition is never deleted; the catalog is static for the run |
| job_attr:auto_hold | supported | dossier ss5 | - | generic | the member enters ON_HOLD when its box starts, instead of starting with it |
| job_attr:avg_runtime | passthrough | dossier ss5, DL-32 | - | generic | the statistics seed is carried; no runtime estimate is computed from it |
| job_attr:box_failure | supported | SEM-12 | - | generic | overrides a box's failure verdict, evaluated on every member transition while the box is RUNNING; the default fold runs only if no override fired |
| job_attr:box_name | supported | SEM-11 | - | generic | names the box this job is a member of; the box's start starts the member |
| job_attr:box_success | supported | SEM-12 | - | generic | overrides a box's success verdict, evaluated on every member transition while the box is RUNNING, so an internal reference can finish the box early |
| job_attr:box_success#iced-member | provisional | SEM-12, SEM-20 | Q6 | none | an iced member is read as satisfied inside box_success, the same way it is read inside an ordinary condition |
| job_attr:box_terminator | supported | SEM-14 | - | generic | this member's failure terminates the whole box |
| job_attr:chk_files | passthrough | dossier ss5, DL-32 | - | generic | the pre-start disk-space gate is not evaluated; the job starts regardless |
| job_attr:command | supported | dossier ss6 | - | generic | the shell command the CMD adapter spawns, passed to /bin/sh verbatim |
| job_attr:condition | supported | SEM-02, SEM-08 | - | generic | the start gate: the job starts on the edge where its condition becomes true |
| job_attr:condition#queued-no-recheck | provisional | DL-50, oracle.Oracle._readmit | Qr6 | none | a job admitted out of QUE_WAIT does not re-evaluate its condition |
| job_attr:date_conditions | supported | SEM-30 | - | generic | the master switch: the time cluster is honoured only when it is truthy |
| job_attr:days_of_week | supported | SEM-30, SEM-31 | - | generic | the days a schedule tick may fall on, as two-letter tokens or `all` |
| job_attr:days_of_week#absent | provisional | SEM-30, runner_scheduler | E10 | none | a schedule with no days_of_week is read as every day |
| job_attr:description | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:elevated | passthrough | dossier ss5, DL-32 | - | generic | no privilege elevation happens; the child runs as the invoking user |
| job_attr:envvars | passthrough | ir._Lowerer._exec_spec, DL-32 | - | generic | carried verbatim on the exec spec; the child process environment is not modified; inert on a BOX (SEM-10) |
| job_attr:exclude_calendar | supported | SEM-30, DL-56 | - | generic | the named calendar whose days are subtracted from the schedule's day set |
| job_attr:exclude_calendar#two-year-probe | supported | DL-56, DL-57, runner_preflight._calendar_preflight | - | none | preflight WARNs when the exclusion covers every eligible day it probes -- 732 dates inclusive, anchor through anchor+731 days; absence is proven within that bound only, and the run is warned, not refused |
| job_attr:fail_codes | supported | SEM-09, DL-33 | - | generic | the explicit failure set; present, it is the only verdict source (Q7, DL-58) |
| job_attr:group | passthrough | dossier ss5, DL-32 | - | generic | the group tag is carried; nothing schedules or reports by group |
| job_attr:heartbeat_interval | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:interactive | passthrough | dossier ss5, DL-32 | - | generic | no interactive terminal is attached to the child process |
| job_attr:job_class | passthrough | dossier ss5, DL-32 | - | generic | job classes are not implemented; no class quota gates a start |
| job_attr:job_load | supported | DL-50 | - | generic | the machine-load units a start holds against the machine's max_load |
| job_attr:job_load#absent | provisional | DL-50, ir.JobIR.job_load_units | Qr4 | none | a job with no job_load demands zero machine-load units, so an unsized job never queues behind max_load |
| job_attr:job_terminator | supported | SEM-14 | - | generic | this member is terminated when its box fails |
| job_attr:job_type | supported | SEM-10 | - | generic | selects the modelled job kind: CMD, BOX or FW |
| job_attr:machine | supported | DL-49, DL-52 | - | generic | names the machine the job runs on; the resolver refuses a foreign one; inert on a BOX (SEM-10) |
| job_attr:machine_method | passthrough | dossier ss5, DL-32 | - | generic | per-member placement method is not implemented; DL-49 resolves placement |
| job_attr:max_exit_success | supported | SEM-09, DL-33 | - | generic | shifts the SUCCESS/FAILURE boundary: exit codes up to it are a success |
| job_attr:max_run_alarm | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:min_run_alarm | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:must_complete_times | supported | SEM-34 | - | generic | the RELATIVE form arms an alarm: a missed completion raises MUST_COMPLETE_ALARM and changes no status |
| job_attr:must_complete_times#unmatched-slot | provisional | SEM-34, ir._Lowerer._sla_attr, oracle.Oracle._sla_offset | - | none | an instant matching no start time uses the first offset; no label was opened for the corner |
| job_attr:must_start_times | supported | SEM-34 | - | generic | the RELATIVE form arms an alarm: a missed start raises MUST_START_ALARM and changes no status |
| job_attr:must_start_times#unmatched-slot | provisional | SEM-34, ir._Lowerer._sla_attr, oracle.Oracle._sla_offset | - | none | an instant matching no start time uses the first offset; no label was opened for the corner |
| job_attr:n_retrys | passthrough | DL-53 | - | generic | the job runs without retries; preflight WARNs that the attribute is unmodelled |
| job_attr:notification_alarm_types | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:notification_emailaddress | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:notification_emailaddress_on_alarm | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:notification_emailaddress_on_failure | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:notification_emailaddress_on_success | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:notification_emailaddress_on_terminated | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:notification_msg | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:notification_template | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:owner | refused | runner_preflight._owner_preflight | - | generic | an owner other than the invoking user is refused at preflight: there is no setuid; inert on a BOX (SEM-10) |
| job_attr:permission | passthrough | dossier ss5, DL-32 | - | generic | job permissions are not enforced; the run uses the invoking user's own |
| job_attr:priority | supported | DL-50 | - | generic | orders the QUE_WAIT queue; an undeclared priority sorts behind every declared one |
| job_attr:priority#direction | provisional | DL-50, capacity.CapacityPool.sorted_waiters | Qr2 | none | a lower priority number is assumed to mean higher priority |
| job_attr:profile | supported | runner_adapters._build_run_spec | - | generic | sourced before the command runs (`. <profile> && <command>`); inert on a BOX (SEM-10) |
| job_attr:profile#sourcing-failure | provisional | runner_adapters._build_run_spec | E5 | none | a profile that fails to source fails the job with sh's exit code |
| job_attr:resources | supported | DL-21, DL-50 | - | generic | the resource groups a start must satisfy before it may run |
| job_attr:run_calendar | supported | SEM-30, DL-56 | - | generic | the named calendar whose days are the schedule's day set |
| job_attr:run_window | supported | SEM-33 | - | generic | a gate, not a trigger: a start outside the window defers or drops, never fires early |
| job_attr:send_notification | passthrough | dossier ss5, DL-32 | - | generic | observability only: no alarm, notification or heartbeat is raised |
| job_attr:start_mins | supported | SEM-32 | - | generic | the minutes past each hour a schedule tick fires at on an eligible day |
| job_attr:start_times | supported | SEM-32 | - | generic | the wall-clock times a schedule tick fires at on an eligible day |
| job_attr:status | supported | SEM-24 | - | generic | the definition-time status: only the out-of-band states are modelled, run states refuse |
| job_attr:std_err_file | supported | runner_adapters.job_log_paths | - | generic | the child's stderr appends here instead of the default run log; inert on a BOX (SEM-10) |
| job_attr:std_in_file | supported | runner_adapters._build_run_spec | - | generic | the child reads stdin from here instead of /dev/null; inert on a BOX (SEM-10) |
| job_attr:std_out_file | supported | runner_adapters.job_log_paths | - | generic | the child's stdout appends here instead of the default run log; inert on a BOX (SEM-10) |
| job_attr:success_codes | supported | SEM-09, DL-33 | - | generic | the explicit success set; with no fail_codes beside it, it alone decides the verdict |
| job_attr:term_run_time | supported | dossier ss5, oracle.Oracle._arm_sla_and_term | - | generic | arms a timer that TERMINATEs the run after n minutes |
| job_attr:timezone | supported | SEM-35 | - | generic | the zone every schedule time on this job is read in |
| job_attr:timezone#dst-fold | provisional | SEM-35, runner_scheduler | E10 | none | a start time inside a DST fold or gap resolves by the pinned interpretation, not by a vendor-verified rule |
| job_attr:ulimit | passthrough | dossier ss5, DL-32 | - | generic | no resource limit is applied to the child process |
| job_attr:watch_file | supported | dossier ss6 | - | generic | the path an FW job polls; the job completes only once the file exists, reaches watch_file_min_size, and two consecutive polls agree on its size |
| job_attr:watch_file_min_size | supported | dossier ss6 | - | generic | the size the watched file must reach before the FW job completes |
| job_attr:watch_file_min_size#steady-size | provisional | runner_adapters.FileWatcherAdapter | E6 | none | two consecutive qualifying polls must report the SAME size before the watch completes; a file still growing resets the count |
| job_attr:watch_interval | supported | dossier ss6 | - | generic | the poll interval of an FW job, in seconds |
| job_attr:watch_interval#default | provisional | runner_adapters.FileWatcherAdapter | E6 | none | an FW job with no watch_interval polls at the profile's default interval |

### machine_attr

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| machine_attr:* | passthrough | ir._Lowerer._lower_machine, DL-28 | - | generic | any other machine attribute is carried verbatim; its effect is not implemented |
| machine_attr:factor | passthrough | DL-49 | - | generic | the per-member load factor is carried; per-member placement is unmodelled |
| machine_attr:machine | supported | DL-49 | - | generic | opens one pool member inside a virtual machine |
| machine_attr:max_load | supported | DL-50 | - | generic | sizes the machine's load bucket; an absent one is AutoSys's unlimited default |
| machine_attr:max_load#pool | provisional | DL-49, runner_preflight._resource_preflight | Qr3 | none | a pool machine carries no load throttle; preflight WARNs and the job runs |
| machine_attr:node_name | supported | DL-49, DL-52 | - | generic | the host an agent or real machine resolves to |
| machine_attr:type | supported | DL-49 | - | generic | selects the resolver's machine kind: a, r, n or v |

### resource_attr

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| resource_attr:* | passthrough | ir._Lowerer._lower_resource, DL-28 | - | generic | any other resource attribute is carried verbatim; its effect is not implemented |
| resource_attr:amount | supported | DL-50 | - | generic | sizes the resource's semaphore bucket |
| resource_attr:amount#absent | refused | runner_preflight._resource_preflight, DL-50 | - | none | an unsized resource a job requires refuses the run; the semaphore cannot be sized |
| resource_attr:res_type | supported | DL-50 | - | generic | sets the resource's default release: R renewable, D depletable, T threshold |
| resource_attr:res_type#absent | supported | DL-50, capacity.resource_type | - | none | a resource with no res_type reads as renewable |
| resource_attr:res_type#unknown | refused | runner_preflight._resource_preflight, DL-50 | - | none | a res_type outside R/D/T has unknown release semantics and refuses the run |

### xinst_attr

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| xinst_attr:* | passthrough | ir._Lowerer._lower_xinst, DL-28 | - | generic | any other external-instance attribute is carried verbatim; its effect is not implemented |
| xinst_attr:xtype | supported | SEM-07, DL-28 | - | generic | the external instance's type; a definition without it is refused |

### global_attr

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| global_attr:value | supported | SEM-08 | - | generic | the global's declared value, unquoted, as value() comparands read it |

### calendar_attr

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| calendar_attr:adjust | supported | SEM-36, SEM-38 | - | generic | a uniform blind day shift applied to every surviving day; the documented range is -9..+9 and anything outside it refuses the calendar |
| calendar_attr:adjust#absent | supported | SEM-36, autocal.compile_calendar | - | none | an absent or blank adjust is zero: no day is shifted |
| calendar_attr:adjust#with-replacement | provisional | SEM-38, DL-59 | Q8b | none | disposition replaces first, then the blind adjust shifts every survivor |
| calendar_attr:condition | supported | SEM-37, DL-57 | - | generic | one date-condition rule; the rules of a calendar union into its day set |
| calendar_attr:cyccal | supported | SEM-36, SEM-39 | - | generic | names the cycle whose periods the cycle-scoped tokens count in |
| calendar_attr:description | passthrough | SEM-36 | - | generic | carried on the calendar record; no rule reads it |
| calendar_attr:end_date | supported | SEM-39 | - | generic | closes the cycle period its preceding start_date opened |
| calendar_attr:holcal | supported | SEM-36 | - | generic | names the standard calendar whose days are this calendar's holidays |
| calendar_attr:holiday | supported | SEM-36, SEM-38 | - | generic | what happens to a generated day that is a holiday; it governs holcal dates outright |
| calendar_attr:holiday#absent | supported | SEM-38, DL-58, autocal.CompiledCalendar._dispose | - | none | with no holiday action a holcal date is handled by the non_workday action, if there is one, and otherwise kept |
| calendar_attr:non_workday | supported | SEM-36, SEM-38 | - | generic | what happens to a generated day that is not a workday: filter or replacement |
| calendar_attr:non_workday#absent | supported | SEM-38, autocal.CompiledCalendar._dispose | - | none | with no non_workday action a generated day is kept exactly as it falls |
| calendar_attr:start_date | supported | SEM-39 | - | generic | opens one cycle period; it pairs positionally with the end_date after it |
| calendar_attr:workday | supported | SEM-36 | - | generic | the weekday mask every workday-scoped token and W/P walk counts in |
| calendar_attr:workday#absent | supported | SEM-36, autocal.compile_calendar | - | none | an absent or blank workday is Monday to Friday |
| calendar_attr:workday#all | supported | SEM-36, DL-60 | - | none | the observed `all` serialization makes every day a workday |
| calendar_attr:workday#codes | supported | SEM-36 | - | none | the comma list of day codes is the third accepted serialization |
| calendar_attr:workday#mask | supported | SEM-36 | - | none | the positional seven-character mask reads Monday first |

### job_type

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| job_type:b | supported | SEM-10 | - | generic | the b spelling selects its modelled job kind |
| job_type:box | supported | SEM-10 | - | generic | the box spelling selects its modelled job kind |
| job_type:c | supported | SEM-10 | - | generic | the c spelling selects its modelled job kind |
| job_type:cmd | supported | SEM-10 | - | generic | the cmd spelling selects its modelled job kind |
| job_type:f | supported | SEM-10 | - | generic | the f spelling selects its modelled job kind |
| job_type:fw | supported | SEM-10 | - | generic | the fw spelling selects its modelled job kind |

### bool_spelling

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| bool_spelling:0 | supported | ir._Lowerer._bool_attr | - | generic | 0 is accepted as a boolean attribute value, case-insensitively |
| bool_spelling:1 | supported | ir._Lowerer._bool_attr | - | generic | 1 is accepted as a boolean attribute value, case-insensitively |
| bool_spelling:false | supported | ir._Lowerer._bool_attr | - | generic | false is accepted as a boolean attribute value, case-insensitively |
| bool_spelling:n | supported | ir._Lowerer._bool_attr | - | generic | n is accepted as a boolean attribute value, case-insensitively |
| bool_spelling:no | supported | ir._Lowerer._bool_attr | - | generic | no is accepted as a boolean attribute value, case-insensitively |
| bool_spelling:true | supported | ir._Lowerer._bool_attr | - | generic | true is accepted as a boolean attribute value, case-insensitively |
| bool_spelling:y | supported | ir._Lowerer._bool_attr | - | generic | y is accepted as a boolean attribute value, case-insensitively |
| bool_spelling:yes | supported | ir._Lowerer._bool_attr | - | generic | yes is accepted as a boolean attribute value, case-insensitively |

### day_token

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| day_token:all | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | all is accepted in days_of_week and folds to its two-letter token |
| day_token:fr | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | fr is accepted in days_of_week and folds to its two-letter token |
| day_token:friday | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | friday is accepted in days_of_week and folds to its two-letter token |
| day_token:mo | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | mo is accepted in days_of_week and folds to its two-letter token |
| day_token:monday | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | monday is accepted in days_of_week and folds to its two-letter token |
| day_token:sa | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | sa is accepted in days_of_week and folds to its two-letter token |
| day_token:saturday | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | saturday is accepted in days_of_week and folds to its two-letter token |
| day_token:su | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | su is accepted in days_of_week and folds to its two-letter token |
| day_token:sunday | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | sunday is accepted in days_of_week and folds to its two-letter token |
| day_token:th | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | th is accepted in days_of_week and folds to its two-letter token |
| day_token:thursday | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | thursday is accepted in days_of_week and folds to its two-letter token |
| day_token:tu | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | tu is accepted in days_of_week and folds to its two-letter token |
| day_token:tuesday | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | tuesday is accepted in days_of_week and folds to its two-letter token |
| day_token:we | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | we is accepted in days_of_week and folds to its two-letter token |
| day_token:wednesday | supported | SEM-30, ir._Lowerer._days_of_week | - | generic | wednesday is accepted in days_of_week and folds to its two-letter token |

### initial_status

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| initial_status:INACTIVE | supported | SEM-24 | - | generic | INACTIVE at definition time is modelled; run states are refused, never guessed |
| initial_status:ON_HOLD | supported | SEM-24 | - | generic | ON_HOLD at definition time is modelled; run states are refused, never guessed |
| initial_status:ON_ICE | supported | SEM-24 | - | generic | ON_ICE at definition time is modelled; run states are refused, never guessed |
| initial_status:ON_NOEXEC | supported | SEM-24 | - | generic | ON_NOEXEC at definition time is modelled; run states are refused, never guessed |

### machine_type

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| machine_type:a | supported | DL-49 | - | generic | machine type a resolves to a host the DL-52 identity rules compare |
| machine_type:n | supported | DL-49 | - | generic | machine type n resolves to a host the DL-52 identity rules compare |
| machine_type:r | supported | DL-49 | - | generic | machine type r resolves to a host the DL-52 identity rules compare |
| machine_type:v | supported | DL-49 | - | generic | machine type v resolves to a host the DL-52 identity rules compare |
| machine_type:v#load-throttle | provisional | DL-49, ir.MachineIR.max_load_units | Qr3 | none | a virtual machine carries no machine-load throttle of its own |

### res_type

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| res_type:D | supported | DL-50 | - | generic | res_type D sets the bucket's default release behaviour |
| res_type:R | supported | DL-50 | - | generic | res_type R sets the bucket's default release behaviour |
| res_type:T | supported | DL-50 | - | generic | res_type T sets the bucket's default release behaviour |

### free_code

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| free_code:A | supported | DL-50 | - | generic | FREE=A overrides the resource's release default for one request |
| free_code:N | supported | DL-50 | - | generic | FREE=N overrides the resource's release default for one request |
| free_code:Y | supported | DL-50 | - | generic | FREE=Y overrides the resource's release default for one request |

### release_policy

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| release_policy:completion | supported | DL-50, capacity.release_policy | - | generic | units are released on completion |
| release_policy:completion#free-absent | provisional | DL-50, capacity.release_policy | Qr1 | none | a request with no FREE takes the res_type default, renewable for an absent res_type |
| release_policy:never | supported | DL-50, capacity.release_policy | - | generic | units are released on never |
| release_policy:success | supported | DL-50, capacity.release_policy | - | generic | units are released on success |

### cond_rule

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| cond_rule:atom | supported | SEM-02, SEM-03 | - | generic | one of the three atom kinds |
| cond_rule:atom_or_group | supported | SEM-02, SEM-03 | - | generic | an atom or a parenthesised expression |
| cond_rule:binop | supported | SEM-02, SEM-03 | - | generic | two operands joined by one operator |
| cond_rule:exitcode_atom | supported | SEM-02, SEM-03 | - | generic | an exit-code comparison against a job's last run |
| cond_rule:expr | supported | SEM-02, SEM-03 | - | generic | an expression, flat left-to-right over & and \| |
| cond_rule:global_atom | supported | SEM-02, SEM-03 | - | generic | a global-variable comparison |
| cond_rule:global_name | supported | SEM-02, SEM-03 | - | generic | the global variable's name |
| cond_rule:global_value | supported | SEM-02, SEM-03 | - | generic | the comparand, quoted or bare |
| cond_rule:job_ref | supported | SEM-02, SEM-03 | - | generic | a job name, with an optional cross-instance suffix |
| cond_rule:job_ref#cross-instance | supported | SEM-07, oracle.Oracle._atom_true | - | none | a job^INST atom reads an instance-qualified pseudo-job that only an injected STATUS can set |
| cond_rule:lookback | supported | SEM-02, SEM-03 | - | generic | the SEM-04 lookback qualifier on an atom |
| cond_rule:op | supported | SEM-02, SEM-03 | - | generic | the operator between two operands |
| cond_rule:paren | supported | SEM-02, SEM-03 | - | generic | an explicitly grouped subexpression |
| cond_rule:start | supported | SEM-02, SEM-03 | - | generic | the whole condition expression is one parse |
| cond_rule:status_atom | supported | SEM-02, SEM-03 | - | generic | a job-status test, optionally qualified by a lookback |

### cond_terminal

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| cond_terminal:AND=& | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes AND=& and the transformer gives it its SEM meaning |
| cond_terminal:AND=and | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes AND=and and the transformer gives it its SEM meaning |
| cond_terminal:BARE_VALUE | supported | SEM-02, SEM-03, SEM-04 | - | generic | an unquoted comparand: anything but whitespace, parentheses and the operators |
| cond_terminal:CIRCUMFLEX | supported | SEM-02, SEM-03, SEM-04 | - | generic | introduces the cross-instance suffix of a job reference (SEM-07) |
| cond_terminal:CMP_OP=!= | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes CMP_OP=!= and the transformer gives it its SEM meaning |
| cond_terminal:CMP_OP=< | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes CMP_OP=< and the transformer gives it its SEM meaning |
| cond_terminal:CMP_OP=<= | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes CMP_OP=<= and the transformer gives it its SEM meaning |
| cond_terminal:CMP_OP== | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes CMP_OP== and the transformer gives it its SEM meaning |
| cond_terminal:CMP_OP=> | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes CMP_OP=> and the transformer gives it its SEM meaning |
| cond_terminal:CMP_OP=>= | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes CMP_OP=>= and the transformer gives it its SEM meaning |
| cond_terminal:COMMA | supported | SEM-02, SEM-03, SEM-04 | - | generic | separates a job reference from its lookback qualifier (SEM-04) |
| cond_terminal:EXITCODE_KW=e | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes EXITCODE_KW=e and the transformer gives it its SEM meaning |
| cond_terminal:EXITCODE_KW=exitcode | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes EXITCODE_KW=exitcode and the transformer gives it its SEM meaning |
| cond_terminal:GLOBAL_NAME | supported | SEM-02, SEM-03, SEM-04 | - | generic | a global variable name: anything but whitespace, parentheses, comma, the comparison characters and the operators |
| cond_terminal:INSTANCE_NAME | supported | SEM-02, SEM-03, SEM-04 | - | generic | a cross-instance suffix: letters, digits, underscore, hash, at or dollar |
| cond_terminal:INT | supported | SEM-02, SEM-03, SEM-04 | - | generic | the integer an exitcode_atom compares against, ASCII digits only |
| cond_terminal:JOB_NAME | supported | SEM-02, SEM-03, SEM-04 | - | generic | a job name: any run of characters except whitespace, parentheses, comma, caret, the operators and a bare colon; a colon inside a name is escaped |
| cond_terminal:LOOKBACK_TOKEN | supported | SEM-02, SEM-03, SEM-04 | - | generic | three lookback spellings: bare hours, `hhhh.mm` and `hhhh\:mm`, with one to four hour digits and one or two minute digits; the mm RANGE is checked at lowering, not here |
| cond_terminal:LPAR | supported | SEM-02, SEM-03, SEM-04 | - | generic | opens an atom's argument list and a parenthesised group |
| cond_terminal:OR=or | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes OR=or and the transformer gives it its SEM meaning |
| cond_terminal:OR=\| | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes OR=\| and the transformer gives it its SEM meaning |
| cond_terminal:QUOTED | supported | SEM-02, SEM-03, SEM-04 | - | generic | a double-quoted comparand with no interior quote; the quotes are stripped from the semantic value |
| cond_terminal:RPAR | supported | SEM-02, SEM-03, SEM-04 | - | generic | closes an atom's argument list and a parenthesised group |
| cond_terminal:STATUS_KW=d | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes STATUS_KW=d and the transformer gives it its SEM meaning |
| cond_terminal:STATUS_KW=done | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes STATUS_KW=done and the transformer gives it its SEM meaning |
| cond_terminal:STATUS_KW=f | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes STATUS_KW=f and the transformer gives it its SEM meaning |
| cond_terminal:STATUS_KW=failure | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes STATUS_KW=failure and the transformer gives it its SEM meaning |
| cond_terminal:STATUS_KW=n | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes STATUS_KW=n and the transformer gives it its SEM meaning |
| cond_terminal:STATUS_KW=notrunning | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes STATUS_KW=notrunning and the transformer gives it its SEM meaning |
| cond_terminal:STATUS_KW=s | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes STATUS_KW=s and the transformer gives it its SEM meaning |
| cond_terminal:STATUS_KW=success | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes STATUS_KW=success and the transformer gives it its SEM meaning |
| cond_terminal:STATUS_KW=t | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes STATUS_KW=t and the transformer gives it its SEM meaning |
| cond_terminal:STATUS_KW=terminated | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes STATUS_KW=terminated and the transformer gives it its SEM meaning |
| cond_terminal:VALUE_KW=v | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes VALUE_KW=v and the transformer gives it its SEM meaning |
| cond_terminal:VALUE_KW=value | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes VALUE_KW=value and the transformer gives it its SEM meaning |
| cond_terminal:WS | supported | SEM-02, SEM-03, SEM-04 | - | generic | whitespace between tokens, ignored by the lexer and never a token |

### lookback_kind

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| lookback_kind:indefinite | supported | SEM-04 | - | generic | the indefinite lookback shape is modelled by the oracle's atom evaluation |
| lookback_kind:window | supported | SEM-04 | - | generic | the window lookback shape is modelled by the oracle's atom evaluation |
| lookback_kind:zero | supported | SEM-04 | - | generic | the zero lookback shape is modelled by the oracle's atom evaluation |

### atom_status

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| atom_status:DONE | supported | SEM-02, SEM-05 | - | generic | a DONE atom reads the referenced job's current recorded status |
| atom_status:FAILURE | supported | SEM-02, SEM-05 | - | generic | a FAILURE atom reads the referenced job's current recorded status |
| atom_status:NOTRUNNING | supported | SEM-02, SEM-05 | - | generic | a NOTRUNNING atom reads the referenced job's current recorded status |
| atom_status:SUCCESS | supported | SEM-02, SEM-05 | - | generic | a SUCCESS atom reads the referenced job's current recorded status |
| atom_status:TERMINATED | supported | SEM-02, SEM-05 | - | generic | a TERMINATED atom reads the referenced job's current recorded status |

### cal_keyword

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| cal_keyword:apr | supported | SEM-37 | - | generic | the apr keyword generates its documented day set |
| cal_keyword:aug | supported | SEM-37 | - | generic | the aug keyword generates its documented day set |
| cal_keyword:cycle | supported | SEM-37 | - | generic | the cycle keyword generates its documented day set |
| cal_keyword:daily | supported | SEM-37 | - | generic | the daily keyword generates its documented day set |
| cal_keyword:dec | supported | SEM-37 | - | generic | the dec keyword generates its documented day set |
| cal_keyword:eom | supported | SEM-37 | - | generic | the eom keyword generates its documented day set |
| cal_keyword:eomweek | supported | SEM-37 | - | generic | the eomweek keyword generates its documented day set |
| cal_keyword:eomwork | supported | SEM-37 | - | generic | the eomwork keyword generates its documented day set |
| cal_keyword:feb | supported | SEM-37 | - | generic | the feb keyword generates its documented day set |
| cal_keyword:fom | supported | SEM-37 | - | generic | the fom keyword generates its documented day set |
| cal_keyword:fomweek | supported | SEM-37 | - | generic | the fomweek keyword generates its documented day set |
| cal_keyword:fomwork | supported | SEM-37 | - | generic | the fomwork keyword generates its documented day set |
| cal_keyword:fri | supported | SEM-37 | - | generic | the fri keyword generates its documented day set |
| cal_keyword:jan | supported | SEM-37 | - | generic | the jan keyword generates its documented day set |
| cal_keyword:jul | supported | SEM-37 | - | generic | the jul keyword generates its documented day set |
| cal_keyword:jun | supported | SEM-37 | - | generic | the jun keyword generates its documented day set |
| cal_keyword:mar | supported | SEM-37 | - | generic | the mar keyword generates its documented day set |
| cal_keyword:may | supported | SEM-37 | - | generic | the may keyword generates its documented day set |
| cal_keyword:mon | supported | SEM-37 | - | generic | the mon keyword generates its documented day set |
| cal_keyword:nov | supported | SEM-37 | - | generic | the nov keyword generates its documented day set |
| cal_keyword:oct | supported | SEM-37 | - | generic | the oct keyword generates its documented day set |
| cal_keyword:sat | supported | SEM-37 | - | generic | the sat keyword generates its documented day set |
| cal_keyword:sep | supported | SEM-37 | - | generic | the sep keyword generates its documented day set |
| cal_keyword:sun | supported | SEM-37 | - | generic | the sun keyword generates its documented day set |
| cal_keyword:thu | supported | SEM-37 | - | generic | the thu keyword generates its documented day set |
| cal_keyword:tue | supported | SEM-37 | - | generic | the tue keyword generates its documented day set |
| cal_keyword:wed | supported | SEM-37 | - | generic | the wed keyword generates its documented day set |
| cal_keyword:weekdays | supported | SEM-37 | - | generic | the weekdays keyword generates its documented day set |
| cal_keyword:workdays | supported | SEM-37 | - | generic | the workdays keyword generates its documented day set |

### cal_family

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| cal_family:cddd | supported | SEM-37 | - | generic | the nth named weekday of a cycle period, `#`/`M`, 1..53 or `L` |
| cal_family:cweek | supported | SEM-37 | - | generic | the nth seven-day chunk of a cycle period, `#`/`M`/`X`, 1..53; `L` belongs to the parity form |
| cal_family:cweek_parity | supported | SEM-37 | - | generic | every even (`E`) or odd (`O`) chunk of a period, or its last (`L`) |
| cal_family:cwek | refused | autocal._parse_token, SEM-37 | - | generic | a cycle-week ordinal, `#`/`M`/`X` with one digit or `L`, whose definitions are garbled in the vendor's own render; refused rather than guessed, because no sane default exists |
| cal_family:cwrk | supported | SEM-37 | - | generic | the nth workday of a cycle period, `#`/`M`/`X`, 1..365 or `L` |
| cal_family:cycl | supported | SEM-37 | - | generic | the nth day of a cycle period, `#`/`M`/`X`, 1..365 or `L` |
| cal_family:cycp | supported | SEM-37 | - | generic | the nth cycle period itself, 1..30; no from-end or excluded form |
| cal_family:day_ordinal | supported | SEM-37 | - | generic | the nth named weekday of the month, `#`/`M`, a SINGLE digit 1..5 or `L` |
| cal_family:mnthd | supported | SEM-37 | - | generic | the nth day of the month, `#`/`M`/`X`, 1..31 or `L` |
| cal_family:month_ordinal | supported | SEM-37 | - | generic | the nth day of a named month, `#`/`M`, 1..31 or `L` |
| cal_family:week | supported | SEM-37 | - | generic | the nth week of the year, `#`/`M`/`X`, 1..53 or `L` |
| cal_family:week_parity | supported | SEM-37 | - | generic | every even (`E`) or odd (`O`) week of the year |
| cal_family:weekd | supported | SEM-37 | - | generic | the nth day of the week, from the start (`#`), the end (`M`) or excluded (`X`), 1..7 or `L` |
| cal_family:wekr | supported | SEM-37 | - | generic | the nth day of a week anchored on a named weekday, `#`/`M`/`X`, 1..7 or `L` |
| cal_family:workd | supported | SEM-37 | - | generic | the nth workday of the month, counted from the start (`#`) or the end (`M`), 1..31 or `L` |
| cal_family:workdx | refused | autocal._parse_token, SEM-37 | - | generic | an excluded workday ordinal whose text contradicts its month-scoped siblings; refused rather than guessed, because no sane default exists |

### cal_operator

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| cal_operator:& | supported | SEM-37, DL-60 | - | generic | intersects two operands |
| cal_operator:&#flat-precedence | provisional | SEM-37, DL-59 | Q8d | none | & and \| evaluate flat left-to-right, with no precedence between them |
| cal_operator:( | supported | SEM-37, DL-60 | - | generic | groups a subexpression |
| cal_operator:) | supported | SEM-37, DL-60 | - | generic | closes a grouped subexpression |
| cal_operator:, | supported | SEM-37, DL-60 | - | generic | separates the rules of one calendar |
| cal_operator:,#list-union | provisional | SEM-37, DL-59 | Q8d | none | the rules of one calendar union; an exclusion-only rule subtracts from that union |
| cal_operator:and | supported | SEM-37, DL-60 | - | generic | the word synonym of `&` |
| cal_operator:and#word-synonym | supported | SEM-37, DL-58 | - | none | AND is an exact synonym of &; the word form was verified, unlike OR |
| cal_operator:not | supported | SEM-37, DL-60 | - | generic | complements its operand |
| cal_operator:or | supported | SEM-37, DL-60 | - | generic | the word synonym of `\|` |
| cal_operator:or#word-synonym | provisional | SEM-37, DL-59 | Q8d | none | OR is pinned as an exact synonym of \| |
| cal_operator:x | supported | SEM-37, DL-60 | - | generic | the X- prefix reads a token as its complement |
| cal_operator:{ | supported | SEM-37, DL-60 | - | generic | the observed brace spelling of `(` |
| cal_operator:\| | supported | SEM-37, DL-60 | - | generic | unions two operands |
| cal_operator:\|#flat-precedence | provisional | SEM-37, DL-59 | Q8d | none | & and \| evaluate flat left-to-right, with no precedence between them |
| cal_operator:} | supported | SEM-37, DL-60 | - | generic | the observed brace spelling of `)` |

### cal_action

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| cal_action:holiday:n | supported | SEM-38 | - | generic | replace the holiday with the NEXT CALENDAR DAY, even if that day is itself a holiday or a non-workday |
| cal_action:holiday:o | supported | SEM-38 | - | generic | restrict to holidays: a generated day that is not a holiday is dropped |
| cal_action:holiday:p | supported | SEM-38 | - | generic | walk BACKWARD to the previous non-holiday workday and use that date |
| cal_action:holiday:s | supported | SEM-38 | - | generic | keep the holiday unchanged, and shield it from the non_workday action |
| cal_action:holiday:w | supported | SEM-38 | - | generic | walk FORWARD to the next non-holiday workday and use that date |
| cal_action:non_workday:n | supported | SEM-38 | - | generic | replace the date with the next workday that is also not a holiday |
| cal_action:non_workday:n#target-recheck | provisional | SEM-38, DL-59 | Q8c | none | every replacement target is final, for N and for W/P and in both categories: the date-conditions are not re-checked and a replaced date never re-enters the other category |
| cal_action:non_workday:o | supported | SEM-38 | - | generic | restrict to non-workdays: a generated day that IS a workday is dropped |
| cal_action:non_workday:p | supported | SEM-38 | - | generic | walk BACKWARD to the previous workday and use that date |
| cal_action:non_workday:s | supported | SEM-38 | - | generic | keep the date unchanged; the day is generated as it falls |
| cal_action:non_workday:w | supported | SEM-38 | - | generic | walk FORWARD to the next workday and use that date |

### cal_workday_form

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| cal_workday_form:all | supported | SEM-36, DL-60 | - | generic | the observed `all` serialization makes every day of the week a workday |
| cal_workday_form:codes | supported | SEM-36 | - | generic | a comma list of two- or three-letter day codes; it is also the fallthrough form, so an unrecognized day is refused here |
| cal_workday_form:mask | supported | SEM-36 | - | generic | the positional seven-character `{X\|.}` mask reads Monday first |

### cal_row_form

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| cal_row_form:date | supported | SEM-36, DL-58 | - | generic | a bare date row fires at 00:00, the vendor's firing time for a job with no start_times of its own |
| cal_row_form:hh:mm | supported | SEM-36 | - | generic | a minute-grained time tail becomes the row's tick |
| cal_row_form:hh:mm:ss | supported | SEM-36, DL-60 | - | generic | the observed export's seconds tail is accepted and truncated to the minute, because ticks are minute-grained |

### event

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| event:DISARM | supported | ir-design ss7 | - | generic | drops a latched tick; no status moves, nothing wakes |
| event:FORCE_STARTJOB | supported | ir-design ss7 | - | generic | starts a job past its condition gate |
| event:KILLJOB | supported | ir-design ss7 | - | generic | terminates a running job, or dequeues and terminates a queued one |
| event:KILLJOB#queued | provisional | DL-50, oracle.Oracle._dispatch | Qr5 | none | killing a queued job dequeues it, consumes its arm and TERMINATEs it |
| event:MUST_COMPLETE_ALARM | supported | SEM-34 | - | generic | emitted when a must_complete deadline passes with the run still live |
| event:MUST_START_ALARM | supported | SEM-34 | - | generic | emitted when a must_start deadline passes with no new run; no status moves |
| event:OFF_HOLD | supported | ir-design ss7 | - | generic | releases a hold and re-attempts the start immediately |
| event:OFF_ICE | supported | ir-design ss7 | - | generic | un-ices a job; conditions are deliberately NOT re-evaluated |
| event:OFF_NOEXEC | supported | ir-design ss7 | - | generic | clears the noexec flag |
| event:ON_HOLD | supported | ir-design ss7 | - | generic | holds a job: it stays startable but does not start |
| event:ON_ICE | supported | ir-design ss7 | - | generic | ices a job: downstream conditions read it as satisfied and it never runs |
| event:ON_ICE#armed | provisional | SEM-20, oracle.Oracle._handle_oob | Q3d | none | a pre-existing arm survives the ice round trip untouched |
| event:ON_ICE#queued | provisional | DL-50, oracle.Oracle._handle_oob | Qr5 | none | icing a queued job dequeues it and settles it INACTIVE now, rather than leaving it in QUE_WAIT |
| event:ON_NOEXEC | supported | ir-design ss7 | - | generic | marks a job as not executing; it completes without running |
| event:SET_GLOBAL | supported | ir-design ss7 | - | generic | sets a global and wakes every job whose condition reads it |
| event:STARTJOB | supported | ir-design ss7 | - | generic | a schedule tick or operator start; it arms must_start whether or not it starts |
| event:STATUS | supported | ir-design ss7 | - | generic | sets a job's status and wakes every job whose condition names it |
| event:TIMER | supported | ir-design ss7 | - | generic | a due deadline or deferred start firing off the timer heap |

### status

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| status:FAILURE | supported | ir-design ss7 | - | generic | a terminal verdict; SEM-09 decides it from the exit code |
| status:INACTIVE | supported | ir-design ss7 | - | generic | the resting status; a live job driven back to it releases everything it held |
| status:QUE_WAIT | supported | ir-design ss7 | - | generic | the start cleared its condition gate but not its capacity gate |
| status:RUNNING | supported | ir-design ss7 | - | generic | the command is live and holds whatever capacity it acquired |
| status:STARTING | supported | ir-design ss7 | - | generic | the gate has cleared and the adapter has been handed the run |
| status:SUCCESS | supported | ir-design ss7 | - | generic | a terminal verdict; SEM-09 decides it from the exit code |
| status:TERMINATED | supported | ir-design ss7 | - | generic | a kill that actually happened, never inferred from a missing record |

### timer

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| timer:deferred_cause | supported | PR-09, oracle.Oracle._schedule_timer | - | generic | the fourth timer shape: a run_window-deferred start replaying its own provenance |
| timer:must_complete | supported | PR-09, oracle.Oracle._schedule_timer | - | generic | armed by the start; it raises MUST_COMPLETE_ALARM if the run is still live |
| timer:must_start | supported | PR-09, oracle.Oracle._schedule_timer | - | generic | armed by the schedule tick; it raises MUST_START_ALARM if no new run began |
| timer:term_run_time | supported | PR-09, oracle.Oracle._schedule_timer | - | generic | armed by the start; it TERMINATEs a run still live at the deadline |

### profile_field

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| profile_field:as_machine | supported | period-model ss2.1, DL-52 | - | generic | the machine names this runner answers to, sorted and de-duplicated |
| profile_field:cmd_grace_us | supported | period-model ss2.1 | - | generic | the grace between SIGTERM and SIGKILL on a cancelled command |
| profile_field:deadman_us | supported | period-model ss2.1, DL-126 | - | generic | the supervisor's observed deadman interval; null means there is no deadman |
| profile_field:default_tz | supported | period-model ss2.1, SEM-35 | - | generic | the zone a job with no timezone of its own is read in |
| profile_field:execution_mode | supported | period-model ss2.1 | - | generic | whether the engine owns the child processes or a supervisor does |
| profile_field:fw_default_interval_us | supported | period-model ss2.1 | - | generic | the poll interval an FW job with no watch_interval uses |
| profile_field:fw_default_interval_us#rounding | provisional | runner_startup.wire_from_profile, period-model ss2.1 | - | none | startup converts the microsecond profile field to WHOLE SECONDS for the watcher and clamps it to at least one, so a sub-second interval is not what the profile asked for. No label was opened for the conversion |
| profile_field:machine_policy | supported | period-model ss2.1, DL-49 | - | generic | how the one ambiguous machine verdict resolves |
| profile_field:reconcile_settle_us | supported | period-model ss2.1 | - | generic | how long reconcile waits for late evidence before it decides |
| profile_field:retry_horizon_us | supported | period-model ss2.1 | - | generic | how far ahead a deferred dispatch retry may be scheduled |
| profile_field:spawn_window_us | supported | period-model ss2.1 | - | generic | the window a spawn has to produce its receipt |
| profile_field:tz_aliases | supported | period-model ss2.1, DL-62 | - | generic | the site-local zone-name table; a name only it resolves fails without it |

### profile_alt

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| profile_alt:execution_mode=detached | supported | period-model ss2.1 | - | generic | a supervisor owns the child processes across engine restarts |
| profile_alt:execution_mode=tethered | supported | period-model ss2.1 | - | generic | the engine owns the child processes; there is no supervisor |
| profile_alt:machine_policy=local-eligible | supported | period-model ss2.1 | - | generic | only a MIXED pool runs here, with a warning that pool placement was ignored; a foreign or unreadable machine still refuses |
| profile_alt:machine_policy=strict | supported | period-model ss2.1 | - | generic | a job whose machine does not resolve local is refused |

### adapter_outcome

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| adapter_outcome:Failed | supported | dossier ss6 | - | generic | a completion with no raw exit code; the engine injects STATUS FAILURE with the cause |
| adapter_outcome:Failed=dispatch lost to engine crash (run directory missing) | supported | runner_adapters.resolve_spool, DL-118 | - | generic | a dispatch whose run directory is gone provably never reached the host, so it fails rather than being retried blind |
| adapter_outcome:Failed=exit_status_unobservable | provisional | runner_adapters.resolve_spool, runner-design ss15 | E7 | generic | a resumed run with no status record fails rather than guessing an exit code |
| adapter_outcome:Failed=exit_status_unobservable (wrapper exited rc={} without a status record) | provisional | runner_adapters.LocalCommandAdapter.run, runner_adapters.SupervisedCommandAdapter._await_outcome, runner-design ss15 | E7 | generic | a wrapper that exited without writing a status record fails the run and names the wrapper's own exit code; both the tethered and the supervised adapter build it |
| adapter_outcome:Failed=malformed status record: outcome 'exited' with exit_code={} | refused | runner_adapters.outcome_from_status | - | generic | an 'exited' record with no integer exit code is refused as a truthful FAILURE, never mapped to something a downstream success could consume |
| adapter_outcome:Failed=spawn failed: {} | supported | runner_adapters.outcome_from_status | - | generic | the wrapper recorded that the spawn itself failed; the run never started |
| adapter_outcome:Failed=unrecognized status record outcome {} | refused | runner_adapters.outcome_from_status | - | generic | a status record whose outcome the protocol does not define is refused, never guessed |
| adapter_outcome:Failed=wrapper spawn failed: {} | supported | runner_adapters.LocalCommandAdapter.run, runner_adapters.SupervisedCommandAdapter.run | - | generic | the engine could not spawn the wrapper at all; the run never started |
| adapter_outcome:Terminated | supported | dossier ss6, DL-41a | - | generic | an OBSERVED kill; the engine injects STATUS TERMINATED for it |
| adapter_outcome:Terminated#external-signal | provisional | runner_adapters | E8 | none | a kill by an external signal is reported as TERMINATED, the same verdict an oracle-ordered kill gets |
| adapter_outcome:Terminated=<dynamic:outcome_from_status> | supported | runner_adapters.outcome_from_status, DL-41a | - | generic | a signalled or terminated status record carries its own cause text into the TERMINATED verdict |
| adapter_outcome:Terminated=wrapper lost; killed at resume | supported | runner_adapters.resolve_spool | - | generic | a resume that finds the wrapper gone kills the surviving command group and reports the kill that happened |
| adapter_outcome:int | supported | dossier ss6, SEM-09 | - | generic | a raw exit code; the SUCCESS/FAILURE verdict over it stays oracle-side |

### wrapper_outcome

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| wrapper_outcome:exited | supported | runner-design ss6, supervisor-protocol ss3, SEM-09 | - | generic | the command ended on its own; the raw exit code goes to the oracle and SEM-09 decides the verdict |
| wrapper_outcome:exited#no-exit-code | refused | runner_adapters.outcome_from_status | - | none | an 'exited' record whose exit_code is not an integer is refused as a truthful FAILURE, never mapped to anything a success-dependent downstream could consume |
| wrapper_outcome:signaled | supported | runner-design ss6, DL-41a | - | generic | the command was killed by a signal; the engine injects STATUS TERMINATED because a kill actually happened |
| wrapper_outcome:spawn_failed | supported | runner-design ss6 | - | generic | /bin/sh could never be spawned; the engine injects STATUS FAILURE and the run never started |
| wrapper_outcome:terminated | supported | runner-design ss6, DL-41a | - | generic | the wrapper killed the command when it lost its parent; the engine injects STATUS TERMINATED with the recorded cause |

### event_source

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| event_source:adapter | supported | ir-design ss7, DL-68, runner.Engine._enqueue | - | generic | the event is a live adapter completion -- the stamp that subjects it to the ss4 stale gate; it is the DEFAULT provenance of an engine-raised input |
| event_source:control | supported | ir-design ss7, DL-68, runner.Engine.inject | - | generic | the event crossed the ss10 control socket, or a rehearsal script stood in for one; it is not something the engine raised itself |
| event_source:reconcile | supported | ir-design ss7, DL-68, runner_startup._inject_completion | - | generic | the completion came from resolving an incomplete run at resume, not from a live adapter; it still goes through the ss4 stale gate |
| event_source:scheduler | supported | ir-design ss7, DL-68, runner.Engine.run_until_quiescent | - | generic | the start came from a calendar tick, so a journal reader can tell it from an operator's sendevent; `Engine._cutoff` stamps it on the boundary path |

### trace_marker

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| trace_marker:DISARM | supported | ir-design ss7, oracle.Oracle._record | - | generic | an explicit journaled disarm: the latched tick is dropped and nothing else moves |
| trace_marker:MUST_COMPLETE_ALARM | supported | ir-design ss7, oracle.Oracle._record | - | generic | the must_complete deadline passed with the run still live; no status moved |
| trace_marker:MUST_START_ALARM | supported | ir-design ss7, oracle.Oracle._record | - | generic | the must_start deadline passed with no new run; no status moved |
| trace_marker:OFF_HOLD | supported | ir-design ss7, oracle.Oracle._record | - | generic | the hold is released and the start is re-attempted immediately |
| trace_marker:OFF_ICE | supported | ir-design ss7, oracle.Oracle._record | - | generic | the ice is cleared; conditions are deliberately NOT re-evaluated |
| trace_marker:OFF_NOEXEC | supported | ir-design ss7, oracle.Oracle._record | - | generic | the noexec flag is cleared |
| trace_marker:ON_HOLD | supported | ir-design ss7, oracle.Oracle._record | - | generic | the job is held: it stays startable but no start goes through |
| trace_marker:ON_ICE | supported | ir-design ss7, oracle.Oracle._record | - | generic | the job is iced: downstream conditions read it as satisfied and it never runs |
| trace_marker:ON_NOEXEC | supported | ir-design ss7, oracle.Oracle._record | - | generic | the job is marked not-executing; it completes without running |
| trace_marker:RUN_WINDOW_DEFER | supported | ir-design ss7, oracle.Oracle._record | - | generic | a start outside the run_window, closer to the next opening, was queued for it |
| trace_marker:RUN_WINDOW_SKIP | supported | ir-design ss7, oracle.Oracle._record | - | generic | a start outside the run_window, closer to the previous close, was dropped |
| trace_marker:SCHED_ARM | supported | ir-design ss7, oracle.Oracle._record | - | generic | a schedule tick that could not start the job latched instead |
| trace_marker:SCHED_DISARM | supported | ir-design ss7, oracle.Oracle._record | - | generic | an unconsumed member arm died with the box run that armed it |
| trace_marker:START_REFUSED | supported | ir-design ss7, oracle.Oracle._record | - | generic | a start request the oracle declined, with the reason it declined it |

### preflight_code

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| preflight_code:calendar | refused | runner_preflight._calendar_preflight, DL-56 | - | generic | a calendar the scheduler cannot read or that can never fire refuses the run |
| preflight_code:job-type | refused | runner_preflight._job_type_preflight | - | unreachable | a job_type with no adapter refuses the run; no JIL reaches this gate, because lowering already refuses every type outside CMD/BOX/FW |
| preflight_code:machine | refused | runner_preflight._machine_preflight, DL-49 | - | generic | a job whose machine does not resolve to this host refuses the run: there is no remote fabric |
| preflight_code:machine-mixed | supported | runner_preflight._machine_preflight, DL-49 | - | generic | a pool with some members here and some elsewhere runs here under local-eligible, with a warning that pool placement was ignored |
| preflight_code:n-retrys | supported | runner_preflight._retry_preflight, DL-53 | - | generic | the run is warned, not refused: n_retrys is carried and never applied, so the job runs exactly once |
| preflight_code:oracle | refused | runner_preflight._oracle_preflight | - | unreachable | an oracle that will not construct over this catalog refuses the run; no JIL reaches this gate, because lowering builds no such catalog |
| preflight_code:owner | refused | runner_preflight._owner_preflight | - | generic | an owner other than the invoking user refuses the run: there is no setuid |
| preflight_code:resources | refused | runner_preflight._resource_preflight, DL-50 | - | generic | a resource the oracle cannot model faithfully refuses the run |
| preflight_code:skeleton-cycle | supported | runner_preflight._skeleton_cycle_preflight, DL-13 | - | generic | a cycle in the AND-success skeleton is legal AutoSys; it warns and disables `plan` rather than refusing the run |
| preflight_code:timezone | refused | runner_preflight._timezone_preflight, SEM-35 | - | generic | a timezone name the SEM-35 ladder cannot resolve refuses the run |

### demand_mode

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| demand_mode:acquire | supported | DL-50, capacity.requirement_demand | - | generic | the start HOLDS its units until the release policy gives them back; a bucket short of them queues the job in QUE_WAIT |
| demand_mode:gate | supported | DL-50, capacity.requirement_demand | - | generic | a threshold check only (res_type T): the level is read, nothing is held and so nothing is ever released |

### machine_verdict

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| machine_verdict:error | refused | runner_preflight.resolve_machine, DL-49 | - | generic | a machine definition the resolver cannot read -- no type, an empty pool, a nested or undefined member -- is refused, never guessed |
| machine_verdict:foreign | supported | DL-49, DL-52 | - | generic | the job's machine resolves elsewhere; the verdict is modelled and `preflight_code:machine` is the ERROR it becomes -- one behaviour, read once as a verdict and once as a refusal |
| machine_verdict:local | supported | DL-49, DL-52 | - | generic | the job's machine resolves to a name this runner answers to, so it runs here |
| machine_verdict:mixed | supported | DL-49 | - | generic | a pool with members on both sides; the machine policy decides whether it runs here |

### literal_alt

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| literal_alt:And.kind=and | supported | SEM-03, ir-design ss3 | - | generic | the discriminator that makes an AND node readable back from JSON |
| literal_alt:CalendarIR.kind=extended | supported | SEM-36, DL-36 | - | generic | a calendar of rules; `compile_calendar` reads it and refuses a standard one |
| literal_alt:CalendarIR.kind=standard | supported | SEM-36, DL-36 | - | generic | a calendar of date rows; `standard_days` reads it and `holcal` requires it |
| literal_alt:CatalogIR.ir_version=0.2 | supported | ir-design ss4 | - | generic | the IR version stamped on every catalog; a reader that meets another refuses |
| literal_alt:ExecSpec.kind=cmd | supported | SEM-10, ir-design ss4 | - | generic | the discriminator that selects the command exec spec |
| literal_alt:ExitCodeAtom.kind=exitcode | supported | SEM-02, ir-design ss3 | - | generic | the discriminator of an exit-code atom |
| literal_alt:FwSpec.kind=fw | supported | SEM-10, ir-design ss4 | - | generic | the discriminator that selects the file-watcher exec spec |
| literal_alt:GlobalAtom.kind=global | supported | SEM-08, ir-design ss3 | - | generic | the discriminator of a global-variable atom |
| literal_alt:Or.kind=or | supported | SEM-03, ir-design ss3 | - | generic | the discriminator that makes an OR node readable back from JSON |
| literal_alt:Paren.kind=paren | supported | SEM-03, ir-design ss3 | - | generic | the discriminator that keeps explicit grouping in the model |
| literal_alt:PreflightItem.severity=ERROR | supported | runner-design ss8 | - | generic | the finding refuses the run |
| literal_alt:PreflightItem.severity=WARN | supported | runner-design ss8 | - | generic | the finding is printed and journaled, and the run goes ahead |
| literal_alt:ResolvedTz.how=city | supported | SEM-35 | - | generic | the unique-city default, which applies ONLY when the estate supplied no alias table at all |
| literal_alt:ResolvedTz.how=map | supported | SEM-35, DL-62 | - | generic | the name resolved through the estate's ujo_timezones alias table, chained at most five hops with an OS lookup per hop |
| literal_alt:ResolvedTz.how=os | supported | SEM-35 | - | generic | the zone name resolved straight out of the OS database |
| literal_alt:ResolvedTz.how=posix | supported | SEM-35 | - | generic | a POSIX fixed-offset spelling, resolved without the zone database |
| literal_alt:SlaSpec.kind=absolute | supported | SEM-34, oracle.Oracle._arm_sla_and_term | - | generic | an absolute must_*_times is lowered and carried, and arms nothing: the oracle owns no calendar, so no absolute deadline exists v1 |
| literal_alt:SlaSpec.kind=relative | supported | SEM-34, oracle.Oracle._arm_sla_and_term | - | generic | a relative `+n` must_*_times is what arms the alarm timer |
| literal_alt:StatusAtom.kind=status | supported | SEM-02, ir-design ss3 | - | generic | the discriminator of a job-status atom |

### runtime

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| runtime:calendar-nesting-cap | refused | autocal._parse_rule | - | none | a rule nested deeper than 100 levels is refused; the bound is the parser's own recursion budget, not a documented vendor limit |
| runtime:calendar-preflight-candidates | supported | DL-57, runner_preflight._next_eligible_day | - | none | the eligible-day probe advances at most 732 candidates, not 732 days: a sparse calendar's 732 candidates can span decades, so the bound is on what was examined and not on the time it covered |
| runtime:calendar-row-seconds-truncation | supported | autocal.standard_rows, DL-60 | - | none | a date row's seconds are dropped: ticks are minute-grained |
| runtime:empty-workday-mask | refused | autocal.compile_calendar | - | none | a W/P action with an all-non-workday mask has nowhere to walk and refuses the calendar before any day is generated |
| runtime:exclusion-only-compound | provisional | autocal.compile_calendar, DL-59 | Q8d | none | a compound rule with no inclusive leaf is evaluated literally as an include, which makes it near-universal |
| runtime:member-arm-scope | provisional | oracle.Oracle._after_transition | Q3c | none | a box member's latched tick is scoped to the box run it was latched in |
| runtime:missed-tick-skip | provisional | runner_startup, runner_scheduler.Scheduler.pop_due | E9 | none | a tick whose instant passed while the engine was down is journaled and dropped, never fired late |
| runtime:preflight-date-basis-utc | provisional | SEM-35, runner_preflight._preflight_local_day | - | none | preflight reads the run anchor as the JOB's local day and falls back to UTC for an unresolvable zone, never consulting the run-level base timezone the scheduler uses; the two can name different days. No label was opened for it |
| runtime:preflight-no-start-skips-probe | provisional | DL-56, runner_preflight.preflight | - | none | preflight with no run anchor skips the calendar-exhaustion probe entirely, so a run_calendar that can never fire again passes unremarked. No label was opened for it |
| runtime:scan-horizon | supported | autocal._SCAN_YEARS, runner_scheduler._EXTENDED_SCAN_DAYS | - | none | a calendar that generates nothing within 60 years reads as exhausted; dormancy is proven within that bound only |
| runtime:sla-offset-broadcast | provisional | SEM-34, ir._Lowerer._sla_attr, oracle.Oracle._sla_offset | - | none | one relative offset broadcasts to every start slot, which SEM-34 marks open -- the strict count rule and the vendor's own example disagree; no label was opened for it |
| runtime:unsized-capacity | refused | runner_preflight._resource_preflight, DL-50 | - | none | preflight refuses a run over an unsized resource; a direct oracle caller bypasses that guard and runs unthrottled, and there the malformed values go quiet -- a malformed job_load reads as zero demand, a malformed priority as unset, and a malformed amount omits the bucket altogether |
| runtime:walk-cap | refused | autocal.CompiledCalendar._walk | - | none | a W/P replacement that finds no valid day within 366 days is degenerate and refuses the calendar |

### adapter_policy

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| adapter_policy:no-retry | passthrough | DL-53 | - | none | the adapter never retries; n_retrys is carried and not applied |
| adapter_policy:no-timeout | supported | runner_adapters.LocalCommandAdapter | - | none | the adapter imposes no timeout of its own; term_run_time is the oracle's timer |
| adapter_policy:unscripted-completion | supported | runner_adapters.FakeAdapter | - | none | a rehearsed run with no script entry completes instantly with exit code 0 |

<!-- register:end -->
