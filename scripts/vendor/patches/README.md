# cytoscape-elk patch

[`cytoscape-elk.patch`](cytoscape-elk.patch) is an ordinary unified diff against
`node_modules/cytoscape-elk/dist/cytoscape-elk.js` from the pinned npm package
`cytoscape-elk` 2.3.0. The upstream source is
[cytoscape/cytoscape.js-elk](https://github.com/cytoscape/cytoscape.js-elk).
The package entry is readable, unminified JavaScript. The patch is applied
before esbuild bundles and minifies it (DL-388).

The diff contains two independent hooks. Its added and removed lines show the
complete local changes with their surrounding code:

- DL-383 assigns the existing ELK and position-application promise to
  `layout.promise`. `run()` still returns the layout. The promise covers the
  existing position-application callback as well as ELK completion.
- DL-384 adds the optional `nodeLabels(node)` callback before scratch storage.
  The callback supplies native ELK label rectangles. It changes no body
  dimensions or position application.

Keep the transpiled prebuilt entry. Cytoscape's constructor wrapper calls the
layout constructor as a function. Rebuilding the package's source as a native
class would break that wrapper (DL-383).

`scripts/vendor/package.json` and its lockfile own package resolution (DL-215).
The two SHA256 values in `scripts/vendor_mermaid.sh` pin this patch's exact
input and result. They are not package pins:

| Entry | SHA256 |
| --- | --- |
| Unpatched | `25acbb9dea9b0dc6fef0e79b697547ee56e579f0e1c22ae4353131e72c7e140a` |
| Patched | `e72eab68d6f7273598b64b3e75f0052d2c40633b9296f11712fa1a8b81109185` |

From the repository root, regenerate all four assets with:

```sh
scripts/vendor_mermaid.sh
```

The script installs the locked npm tree in a temporary directory. It checks
the unpatched SHA256, runs `git apply --check`, applies the diff, and checks
the patched SHA256. All four steps precede writes to `src/dsl41/_vendor/`.
A changed input, an already patched input, an invalid diff, or an unexpected
result fails the build. No fuzzy, reverse, or fallback application is used.
Do not bypass a failed check or copy a partial result into the checkout.

For a deliberate package upgrade:

1. Inspect upstream's current entry and source for each hook first. Drop an
   individual hook when upstream provides equivalent behavior. Drop the whole
   patch and its guards when neither hook is needed.
2. Update the existing manifest and lockfile using the command documented in
   `scripts/vendor_mermaid.sh`. Install that locked tree in a temporary
   directory and inspect its unpatched entry.
3. Recreate the remaining diff against that exact entry. Review both sides of
   each hunk. Recompute the unpatched and patched SHA256 values in the script
   and this table. A hash mismatch alone never justifies changing a hash.
4. Run `scripts/vendor_mermaid.sh` from the repository root. Review all four
   generated assets and their attribution. A maintenance-only patch rewrite
   must preserve their bytes; an upgrade needs its own artifact review.
5. Run the local gates in `docs/agent-workflow.md` and the browser suite in
   Chromium, WebKit, and Firefox before accepting changed runtime bytes.

Existing regression coverage in `tests/test_viz_explore_browser.py` includes
`test_elk_rejection_releases_controls_and_preserves_the_view`,
`test_elk_pipeline_failure_restores_the_previous_grab_setting`,
`test_elk_old_failure_cannot_release_a_new_arrangement`,
`test_elk_startup_failure_finishes_loading_and_allows_retry`,
`test_compact_caption_empty_box_keeps_top_landmark_and_native_rectangle`, and
`test_compact_caption_pixel_budget_and_native_leaf_rectangles`.
The non-browser asset checks remain in `tests/test_viz_explore.py` and
`tests/test_viz_html.py`.
