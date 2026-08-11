#!/usr/bin/env bash
# Regenerate src/dsl41/_vendor/ -- the browser assets `dsl41 viz --html`
# inlines into its emitted page. Dev-time only: users never need node.
#
# Pins (bump deliberately, re-run, re-check the invariants below):
#   mermaid 11.16.1               (MIT; dist/mermaid.min.js copied byte-exact)
#   @mermaid-js/layout-elk 0.2.2  (MIT)
#   elkjs 0.9.3                   (EPL-2.0; layout-elk's dependency, pinned
#                                  here so the attribution banner stays true)
#   esbuild 0.28.2                (build tool only, nothing of it ships)
#
# The elk bundle is built, not copied: layout-elk publishes ESM only, and the
# page needs a classic <script>. esbuild output is not byte-reproducible
# across versions -- tests pin a size floor + invariants, not a hash. elkjs
# ships no /*! legal comments of its own, so attribution is our banner plus
# THIRD_PARTY_LICENSES, not preserved input comments.
set -euo pipefail

vendor="$(git rev-parse --show-toplevel)/src/dsl41/_vendor"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"

npm install --no-save --no-audit --no-fund \
    mermaid@11.16.1 @mermaid-js/layout-elk@0.2.2 elkjs@0.9.3 esbuild@0.28.2

banner='/*! @mermaid-js/layout-elk 0.2.2 (MIT) bundling elkjs 0.9.3 (EPL-2.0);'
banner+=' built by dsl41 scripts/vendor_mermaid.sh; see THIRD_PARTY_LICENSES'
banner+=' in the dsl41 distribution for full texts and source URLs. */'

cp node_modules/mermaid/dist/mermaid.min.js "$vendor/mermaid.min.js"
npx esbuild @mermaid-js/layout-elk --bundle --format=iife \
    --global-name=elkLayouts --minify --legal-comments=inline \
    --banner:js="$banner" \
    --outfile="$vendor/mermaid-layout-elk.iife.min.js"

# Invariants the HTML embedding rests on: payloads are inline-safe (no
# </script anywhere) and the attribution banner is present.
! grep -q '</script' "$vendor/mermaid.min.js"
! grep -q '</script' "$vendor/mermaid-layout-elk.iife.min.js"
grep -q 'EPL-2.0' "$vendor/mermaid-layout-elk.iife.min.js"

ls -l "$vendor"
shasum -a 256 "$vendor"/*.js
