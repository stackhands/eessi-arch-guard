#!/bin/bash
# Render each Graphviz drawing here to the SVG committed beside it. The docs
# build never runs Graphviz; run this by hand after changing a .dot file:
#   pixi exec --spec graphviz bash docs/orgmode/explanation/figures/render.sh
set -euo pipefail
cd "$(dirname -- "$0")"
for dot in *.dot; do
    dot -Tsvg "$dot" -o "${dot%.dot}.svg"
done
