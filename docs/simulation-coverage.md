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
so a new attribute, token, event kind, status, profile field, or value
alternative without a row fails the suite.

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
| job_attr:box_failure | supported | SEM-12 | - | generic | overrides a box's failure verdict; evaluated only when the box is done |
| job_attr:box_name | supported | SEM-11 | - | generic | names the box this job is a member of; the box's start starts the member |
| job_attr:box_success | supported | SEM-12 | - | generic | overrides a box's success verdict; evaluated only when the box is done |
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
| job_attr:exclude_calendar#two-year-probe | supported | DL-56, runner_scheduler | - | none | an exclusion that leaves no eligible day inside 731 days reports the schedule as exhausted; absence is proven within that bound only |
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
| job_attr:must_complete_times | supported | SEM-34 | - | generic | an alarm only: a missed completion raises MUST_COMPLETE_ALARM and changes no status |
| job_attr:must_complete_times#unmatched-slot | provisional | SEM-34, ir._Lowerer._sla_attr, oracle.Oracle._sla_offset | - | none | an instant matching no start time uses the first offset; no label was opened for the corner |
| job_attr:must_start_times | supported | SEM-34 | - | generic | an alarm only: a missed start raises MUST_START_ALARM and changes no status |
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
| job_attr:watch_file | supported | dossier ss6 | - | generic | the path an FW job polls; the job completes when the file arrives |
| job_attr:watch_file_min_size | supported | dossier ss6 | - | generic | the size the watched file must reach before the FW job completes |
| job_attr:watch_file_min_size#steady-size | provisional | runner_adapters.FileWatcherAdapter | E6 | none | the size is read once per poll; a file still growing is not waited out |
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
| calendar_attr:adjust | supported | SEM-36, SEM-38 | - | generic | a uniform blind day shift applied to every surviving day, -9..+9 |
| calendar_attr:adjust#with-replacement | provisional | SEM-38, DL-59 | Q8b | none | disposition replaces first, then the blind adjust shifts every survivor |
| calendar_attr:condition | supported | SEM-37, DL-57 | - | generic | one date-condition rule; the rules of a calendar union into its day set |
| calendar_attr:cyccal | supported | SEM-36, SEM-39 | - | generic | names the cycle whose periods the cycle-scoped tokens count in |
| calendar_attr:description | supported | SEM-36 | - | generic | carried on the calendar record; no rule reads it |
| calendar_attr:end_date | supported | SEM-39 | - | generic | closes the cycle period its preceding start_date opened |
| calendar_attr:holcal | supported | SEM-36 | - | generic | names the standard calendar whose days are this calendar's holidays |
| calendar_attr:holiday | supported | SEM-36, SEM-38 | - | generic | what happens to a generated day that is a holiday; it governs holcal dates outright |
| calendar_attr:non_workday | supported | SEM-36, SEM-38 | - | generic | what happens to a generated day that is not a workday: filter or replacement |
| calendar_attr:start_date | supported | SEM-39 | - | generic | opens one cycle period; it pairs positionally with the end_date after it |
| calendar_attr:workday | supported | SEM-36 | - | generic | the weekday mask every workday-scoped token and W/P walk counts in |
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
| cond_terminal:BARE_VALUE | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes BARE_VALUE and the transformer gives it its SEM meaning |
| cond_terminal:CMP_OP=!= | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes CMP_OP=!= and the transformer gives it its SEM meaning |
| cond_terminal:CMP_OP=< | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes CMP_OP=< and the transformer gives it its SEM meaning |
| cond_terminal:CMP_OP=<= | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes CMP_OP=<= and the transformer gives it its SEM meaning |
| cond_terminal:CMP_OP== | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes CMP_OP== and the transformer gives it its SEM meaning |
| cond_terminal:CMP_OP=> | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes CMP_OP=> and the transformer gives it its SEM meaning |
| cond_terminal:CMP_OP=>= | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes CMP_OP=>= and the transformer gives it its SEM meaning |
| cond_terminal:EXITCODE_KW=e | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes EXITCODE_KW=e and the transformer gives it its SEM meaning |
| cond_terminal:EXITCODE_KW=exitcode | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes EXITCODE_KW=exitcode and the transformer gives it its SEM meaning |
| cond_terminal:GLOBAL_NAME | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes GLOBAL_NAME and the transformer gives it its SEM meaning |
| cond_terminal:INSTANCE_NAME | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes INSTANCE_NAME and the transformer gives it its SEM meaning |
| cond_terminal:JOB_NAME | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes JOB_NAME and the transformer gives it its SEM meaning |
| cond_terminal:LOOKBACK_TOKEN | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes LOOKBACK_TOKEN and the transformer gives it its SEM meaning |
| cond_terminal:OR=or | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes OR=or and the transformer gives it its SEM meaning |
| cond_terminal:OR=\| | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes OR=\| and the transformer gives it its SEM meaning |
| cond_terminal:QUOTED | supported | SEM-02, SEM-03, SEM-04 | - | generic | the grammar lexes QUOTED and the transformer gives it its SEM meaning |
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
| cal_family:cddd | supported | SEM-37 | - | generic | the cddd ordinal family generates its documented day set |
| cal_family:cweek | supported | SEM-37 | - | generic | the cweek ordinal family generates its documented day set |
| cal_family:cweek_parity | supported | SEM-37 | - | generic | the cweek_parity ordinal family generates its documented day set |
| cal_family:cwek | refused | autocal._parse_token, SEM-37 | - | generic | the cwek family is doc-defective: the vendor's own text contradicts itself, so the token is refused rather than guessed |
| cal_family:cwrk | supported | SEM-37 | - | generic | the cwrk ordinal family generates its documented day set |
| cal_family:cycl | supported | SEM-37 | - | generic | the cycl ordinal family generates its documented day set |
| cal_family:cycp | supported | SEM-37 | - | generic | the cycp ordinal family generates its documented day set |
| cal_family:day_ordinal | supported | SEM-37 | - | generic | the day_ordinal ordinal family generates its documented day set |
| cal_family:mnthd | supported | SEM-37 | - | generic | the mnthd ordinal family generates its documented day set |
| cal_family:month_ordinal | supported | SEM-37 | - | generic | the month_ordinal ordinal family generates its documented day set |
| cal_family:week | supported | SEM-37 | - | generic | the week ordinal family generates its documented day set |
| cal_family:week_parity | supported | SEM-37 | - | generic | the week_parity ordinal family generates its documented day set |
| cal_family:weekd | supported | SEM-37 | - | generic | the weekd ordinal family generates its documented day set |
| cal_family:wekr | supported | SEM-37 | - | generic | the wekr ordinal family generates its documented day set |
| cal_family:workd | supported | SEM-37 | - | generic | the workd ordinal family generates its documented day set |
| cal_family:workdx | refused | autocal._parse_token, SEM-37 | - | generic | the workdx family is doc-defective: the vendor's own text contradicts itself, so the token is refused rather than guessed |

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
| cal_operator:and#word-synonym | provisional | SEM-37, DL-59 | Q8d | none | AND is pinned as an exact synonym of & |
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
| cal_action:n | supported | SEM-38 | - | generic | action n replaces an excluded date with a walked target day |
| cal_action:n#target-recheck | provisional | SEM-38, DL-59 | Q8c | none | the replacement target is final: the date-conditions are not re-checked and a replacement never re-enters the other category |
| cal_action:o | supported | SEM-38 | - | generic | action o filters the category without moving any date |
| cal_action:p | supported | SEM-38 | - | generic | action p replaces an excluded date with a walked target day |
| cal_action:p#target-recheck | provisional | SEM-38, DL-59 | Q8c | none | the replacement target is final: the date-conditions are not re-checked and a replacement never re-enters the other category |
| cal_action:s | supported | SEM-38 | - | generic | action s filters the category without moving any date |
| cal_action:w | supported | SEM-38 | - | generic | action w replaces an excluded date with a walked target day |
| cal_action:w#target-recheck | provisional | SEM-38, DL-59 | Q8c | none | the replacement target is final: the date-conditions are not re-checked and a replacement never re-enters the other category |

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
| profile_alt:machine_policy=local-eligible | supported | period-model ss2.1 | - | generic | an unresolvable machine is treated as eligible here |
| profile_alt:machine_policy=strict | supported | period-model ss2.1 | - | generic | a job whose machine does not resolve local is refused |

### adapter_outcome

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| adapter_outcome:Failed | supported | dossier ss6 | - | generic | a completion with no raw exit code; the engine injects STATUS FAILURE with the cause |
| adapter_outcome:Failed=dispatch lost to engine crash (run directory missing) | supported | runner_adapters.resolve_spool, DL-118 | - | generic | a dispatch whose run directory is gone provably never reached the host, so it fails rather than being retried blind |
| adapter_outcome:Failed=exit_status_unobservable | provisional | runner_adapters, runner-design ss15 | E7 | generic | a wrapper that exited without a status record fails the run rather than guessing an exit code |
| adapter_outcome:Terminated | supported | dossier ss6, DL-41a | - | generic | an OBSERVED kill; the engine injects STATUS TERMINATED for it |
| adapter_outcome:Terminated#external-signal | provisional | runner_adapters | E8 | none | a kill by an external signal is reported as TERMINATED, the same verdict an oracle-ordered kill gets |
| adapter_outcome:int | supported | dossier ss6, SEM-09 | - | generic | a raw exit code; the SUCCESS/FAILURE verdict over it stays oracle-side |

### runtime

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| runtime:calendar-row-seconds-truncation | supported | autocal.standard_rows, DL-60 | - | none | a date row's seconds are dropped: ticks are minute-grained |
| runtime:empty-workday-mask | refused | autocal.compile_calendar | - | none | a W/P action with an all-non-workday mask has nowhere to walk and refuses the calendar before any day is generated |
| runtime:exclusion-only-compound | provisional | autocal.compile_calendar, DL-59 | Q8d | none | a compound rule with no inclusive leaf is evaluated literally as an include, which makes it near-universal |
| runtime:member-arm-scope | provisional | oracle.Oracle._after_transition | Q3c | none | a box member's latched tick is scoped to the box run it was latched in |
| runtime:missed-tick-skip | provisional | runner_scheduler.Scheduler.pop_due, runner_startup | E9 | none | a tick whose instant passed while the engine was down is journaled and dropped, never fired late |
| runtime:scan-horizon | supported | autocal._SCAN_YEARS, runner_scheduler._EXTENDED_SCAN_DAYS | - | none | a calendar that generates nothing within 60 years reads as exhausted; dormancy is proven within that bound only |
| runtime:sla-offset-broadcast | supported | SEM-34, ir._Lowerer._sla_attr | - | none | one relative offset broadcasts to every start slot |
| runtime:unsized-capacity | refused | runner_preflight._resource_preflight, DL-50 | - | none | preflight refuses a run over an unsized resource; a direct oracle caller bypasses that guard and runs unthrottled |
| runtime:walk-cap | refused | autocal.CompiledCalendar._walk | - | none | a W/P replacement that finds no valid day within 366 days is degenerate and refuses the calendar |

### adapter_policy

| id | class | cite | label | detector | effect |
| --- | --- | --- | --- | --- | --- |
| adapter_policy:no-retry | passthrough | DL-53 | - | none | the adapter never retries; n_retrys is carried and not applied |
| adapter_policy:no-timeout | supported | runner_adapters.LocalCommandAdapter | - | none | the adapter imposes no timeout of its own; term_run_time is the oracle's timer |
| adapter_policy:unscripted-completion | supported | runner_adapters.FakeAdapter | - | none | a rehearsed run with no script entry completes instantly with exit code 0 |

<!-- register:end -->
