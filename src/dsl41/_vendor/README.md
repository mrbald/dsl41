# Vendored browser assets

Inlined verbatim into pages emitted by `dsl41 viz --html` (mermaid + elk
payloads, DL-70) and `dsl41 viz --explore` (cytoscape payload, DL-71) so
the pages render offline (`file://`, zero network). Dev-time regeneration
only — users of the package never need node.

| file | origin | license |
|---|---|---|
| `mermaid.min.js` | mermaid 11.16.1 `dist/mermaid.min.js`, byte-exact from npm | MIT (bundles DOMPurify, lodash-es, js-yaml, cytoscape, d3, katex, roughjs — see its `/*! */` banners) |
| `mermaid-layout-elk.iife.min.js` | built: `@mermaid-js/layout-elk` 0.2.2 + `elkjs` 0.9.3, esbuild IIFE (`scripts/vendor_mermaid.sh`) | MIT + EPL-2.0 (elkjs) |
| `cytoscape-explore.iife.min.js` | built: `cytoscape` 3.33.1 + `cytoscape-elk` 2.3.0 + `elkjs` 0.9.3 + `cytoscape-context-menus` 4.1.0, esbuild IIFE (`scripts/vendor_mermaid.sh`) | MIT + EPL-2.0 (elkjs) |

`elkjs` is deliberately duplicated across the two built bundles: they are
independent artifacts, and a shared-chunk build would couple their upgrade
cadence for ~500 KiB deflated (DL-71).

Full license texts and source URLs: `THIRD_PARTY_LICENSES` at the repo
root (shipped in the wheel's dist-info).

Regenerate with `scripts/vendor_mermaid.sh` (pins the versions; bump them
there deliberately). Invariants the HTML embedding relies on, checked by
the script and by `tests/test_viz_html.py` / `tests/test_viz_explore.py`:

- no payload contains `</script` (they are inlined into `<script>`
  blocks without further escaping);
- each built bundle starts with an attribution banner naming elkjs/EPL-2.0;
- `mermaid.min.js` matches its pinned sha256 (byte-exact from npm — the
  esbuild-built bundles are not byte-reproducible, so they get size
  floors instead);
- the elk bundle's IIFE global is a namespace object (`elkLayouts.default`
  holds the layout-loader array); the page JS accepts both shapes;
- the cytoscape bundle's IIFE global is `cyBundle` (`cyBundle.default` is
  the constructor with both extensions registered); cytoscape-elk drives
  elkjs's bundled synchronous shim on the main thread — no Worker, no
  fetch — and the context-menus CSS is inlined in
  `templates/viz_explore.html` (drift-checked by the script).
