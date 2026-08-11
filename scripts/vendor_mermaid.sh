#!/usr/bin/env bash
# Regenerate src/dsl41/_vendor/ -- the browser assets `dsl41 viz --html`
# (mermaid bundle, DL-70) and `dsl41 viz --explore` (cytoscape bundle,
# DL-71) inline into their emitted pages. Dev-time only: users never need
# node.
#
# Pins (bump deliberately, re-run, re-check the invariants below):
#   mermaid 11.16.1               (MIT; dist/mermaid.min.js copied byte-exact)
#   @mermaid-js/layout-elk 0.2.2  (MIT)
#   elkjs 0.9.3                   (EPL-2.0; dependency of both bundles,
#                                  pinned here so the banners stay true)
#   cytoscape 3.33.1              (MIT)
#   cytoscape-elk 2.3.0           (MIT; drives elk.bundled.js on the main
#                                  thread -- no Worker, no fetch)
#   cytoscape-context-menus 4.1.0 (MIT; its CSS is inlined in
#                                  templates/viz_explore.html, not vendored)
#   esbuild 0.28.2                (build tool only, nothing of it ships)
#
# Both non-mermaid payloads are built, not copied: the upstream packages
# publish ESM only, and the pages need a classic <script>. esbuild output is
# not byte-reproducible across versions -- tests pin a size floor +
# invariants, not a hash. elkjs ships no /*! legal comments of its own, so
# attribution is our banner plus THIRD_PARTY_LICENSES, not preserved input
# comments. elkjs is deliberately duplicated across the two bundles: they
# are independent artifacts, and a shared-chunk build would couple their
# upgrade cadence for ~500 KiB deflated (DL-71).
set -euo pipefail

vendor="$(git rev-parse --show-toplevel)/src/dsl41/_vendor"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"

npm install --no-save --no-audit --no-fund \
    mermaid@11.16.1 @mermaid-js/layout-elk@0.2.2 elkjs@0.9.3 \
    cytoscape@3.33.1 cytoscape-elk@2.3.0 cytoscape-context-menus@4.1.0 \
    esbuild@0.28.2

banner='/*! @mermaid-js/layout-elk 0.2.2 (MIT) bundling elkjs 0.9.3 (EPL-2.0);'
banner+=' built by dsl41 scripts/vendor_mermaid.sh; see THIRD_PARTY_LICENSES'
banner+=' in the dsl41 distribution for full texts and source URLs. */'

cp node_modules/mermaid/dist/mermaid.min.js "$vendor/mermaid.min.js"
npx esbuild @mermaid-js/layout-elk --bundle --format=iife \
    --global-name=elkLayouts --minify --legal-comments=inline \
    --banner:js="$banner" \
    --outfile="$vendor/mermaid-layout-elk.iife.min.js"

# Bundle #2 (DL-71): cytoscape + extensions for the --explore page. The
# entry registers both extensions so the page only needs cyBundle.default.
cat > cy_entry.js <<'EOF'
import cytoscape from "cytoscape";
import elk from "cytoscape-elk";
import contextMenus from "cytoscape-context-menus";
cytoscape.use(elk);
cytoscape.use(contextMenus);
export default cytoscape;
EOF

cybanner='/*! cytoscape 3.33.1 (MIT) + cytoscape-elk 2.3.0 (MIT) +'
cybanner+=' cytoscape-context-menus 4.1.0 (MIT) bundling elkjs 0.9.3 (EPL-2.0);'
cybanner+=' built by dsl41 scripts/vendor_mermaid.sh; see THIRD_PARTY_LICENSES'
cybanner+=' in the dsl41 distribution for full texts and source URLs. */'

npx esbuild cy_entry.js --bundle --format=iife \
    --global-name=cyBundle --minify --legal-comments=inline \
    --banner:js="$cybanner" \
    --outfile="$vendor/cytoscape-explore.iife.min.js"

# The extension CSS is inlined in templates/viz_explore.html between
# begin/end sentinel comments (the page uses a flat menu, so the
# submenu-indicator asset is never loaded); diff the whole block, indent-
# and blank-line-normalized, so a bumped pin failing the check forces a
# deliberate template refresh (per-line grep would pass generic lines
# like '}' anywhere in the template -- review finding).
template="$vendor/../templates/viz_explore.html"
# the trailing echo tolerates a missing final newline (the npm package's
# CSS ends without one); blank lines are dropped anyway
normalize() { { cat "$1"; echo; } | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^$/d'; }
sed -n '/begin cytoscape-context-menus/,/end cytoscape-context-menus/p' "$template" \
    | sed -e '1,/\*\//d' -e '/end cytoscape-context-menus/d' > template-css.txt
diff <(normalize template-css.txt) \
     <(normalize node_modules/cytoscape-context-menus/cytoscape-context-menus.css) \
    || { echo "context-menus CSS drifted from the template copy" >&2; exit 1; }

# Invariants the HTML embedding rests on: payloads are inline-safe (no
# </script anywhere) and the attribution banners are present. Explicit
# gates: under set -e a bare '! grep' never fails the script (POSIX
# errexit exempts '!' pipelines -- review finding).
for payload in mermaid.min.js mermaid-layout-elk.iife.min.js cytoscape-explore.iife.min.js; do
    if grep -q '</script' "$vendor/$payload"; then
        echo "$payload contains </script -- not inline-safe" >&2
        exit 1
    fi
done
grep -q 'EPL-2.0' "$vendor/mermaid-layout-elk.iife.min.js"
grep -q 'EPL-2.0' "$vendor/cytoscape-explore.iife.min.js"
grep -q 'var cyBundle' "$vendor/cytoscape-explore.iife.min.js"

ls -l "$vendor"
shasum -a 256 "$vendor"/*.js
