# Inspect an estate's jobs

This page is for the operator inspecting job dependencies at a terminal,
including an agent calling the CLI.
The [operator track](../README.md#documents) covers running and recovering an engine.

## Find a job's neighbors

```sh
dsl41 nav fanins estate.jil report
dsl41 nav fanouts estate.py extract --json
dsl41 nav children estate.toml nightly --deep
dsl41 nav path estate.jil extract report
```

Use `dsl41 nav VERB ESTATE JOB`.
For `path`, add the destination after JOB.
ESTATE is a JIL file, a Python DSL module or package directory, or a TOML estate manifest.
Python input executes its source through the same loader as `dsl41 build`.
Use `--param NAME=VALUE` for a Python module's Params bindings.
Use `-p FILE` for JIL properties and `--environment NAME` for a manifest environment.
The [author guide](authoring.md) covers those inputs and their bindings.

| Verb | Result |
| --- | --- |
| `fanins`, `upstream` | Jobs named by JOB's condition |
| `fanouts`, `downstream` | Jobs whose conditions name JOB |
| `box` | JOB's immediate box, or no names if unboxed |
| `children` | A box's immediate members; `--deep` includes nested members |
| `siblings` | Other members of JOB's immediate box; no names if unboxed |
| `ancestors` | Transitive fanins |
| `descendants` | Transitive fanouts |
| `path` | One shortest dependency path from JOB to the destination |
| `show` | JOB's definition rendered as a Python DSL call |

Navigation derives IR-G from the loaded estate on each call.
The [graph contract](ir-design.md#5-ir-g-derived-graph) defines that derivation.
Dependency queries follow job references in `condition`.
They exclude global variables and box completion predicates.
Add `--all` to include mutex atoms, undefined local jobs and cross-instance references.
Cross-instance names appear as `name^INSTANCE`.
When that spelling is also a local job, querying it selects the local job.

## Bound and explain a query

```sh
dsl41 nav ancestors estate.jil report --depth 2 --explain
dsl41 nav fanins estate.jil report --all --json
```

`--depth N` bounds ancestors and descendants by edge count.
Zero returns an empty list.
Transitive queries exclude the starting job, even in a cycle.
Paths follow producer to consumer and include both endpoints.
Equal-length paths choose sorted neighbors first.
An existing job has a path to itself containing only that name.

Text output has one name per line, sorted and without duplicates.
Path output keeps path order.
`--explain` appends a tab and the edge's condition atom or `box_name: BOX` membership.
Multiple atoms for one neighbor are separated by `; `.
For transitive results, the atom is the last edge on the chosen shortest route.
A path's first line has an empty atom.
`show` prints a DSL definition with its condition and box membership.

`--json` returns a list of names, including an empty list when nothing matches.
For `show`, it returns an object with `name` and `definition` strings.
JSON keeps that shape when combined with `--explain`.
`--permit-unknown` accepts unknown attributes using the compiler's passthrough lane.

| Exit | Meaning |
| --- | --- |
| 0 | Query succeeded; an empty neighborhood is a success |
| 1 | Job or requested path was not found |
| 2 | Usage error or estate could not be loaded |

Errors go to stderr.
Navigation does not run jobs or contact a running engine.

## Navigate a decompiled module

```sh
dsl41 decompile estate.jil -o estate.py
python estate.py nav fanins report --json
python estate.py nav show report
python estate.py
```

The module's `nav` command uses its in-memory estate and the same query implementation.
Running the module without arguments prints its rebuilt JIL.
For engine state and interventions, use the [incident guide](incident-triage.md).
