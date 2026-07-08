# Licensing

Model: AGPL-3.0-only for the public repository + commercial licenses sold separately
(dual licensing). Rationale: AGPL is maximally unattractive for enterprise internal
forks, which channels enterprise users to the commercial license.

Operational requirements:
1. Copyright unity. All contributors sign a CLA assigning (or broadly licensing)
   copyright to the two founders' entity, preserving the right to dual-license.
   No external PR is merged without a signed CLA. (Add CLA text before first
   external contribution; not needed while contributors == founders.)
2. Clean-room discipline. No production JIL, exports, names, or derived artifacts
   from any employer estate may enter this repository, its test corpus, docs, or
   issue tracker. tests/corpus/ is synthetic/doc-derived only.
3. LICENSE file: verbatim AGPL-3.0 text from gnu.org — done 2026-07-08 (sha256
   0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0, the canonical
   agpl-3.0.txt). COMMERCIAL.md holds the commercial-availability notice.
   Per-file SPDX headers are deliberately omitted (2026-07-08): the root LICENSE
   and pyproject `license` field govern the whole work; source files stay free of
   non-functional boilerplate. Do not re-add headers.
4. Dependency audit (2026-07-08): runtime deps (lark, pydantic, typer,
   pydantic-core) are MIT; dev-only tools are MIT except hypothesis (MPL-2.0,
   not distributed with the compiler). All compatible with AGPL-3.0-only.
   Re-audit whenever a runtime dependency is added.
5. Copyright notices currently read "dsl41 authors"; replace with the founders'
   entity name once it exists.
