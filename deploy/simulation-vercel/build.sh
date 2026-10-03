#!/usr/bin/env bash
# Assemble the hosted simulation engine into dist/, ready for `vercel deploy`.
#
#     deploy/simulation-vercel/build.sh
#
# dist/ holds exactly what the function needs: this wrapper, the `simulation`
# package (scenarios, CAD geometry) and the Pi's own `src` package with its
# config, so the hosted engine runs the same code as the machine. The viewer
# page goes to public/, which Vercel serves from its CDN.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
dist="$here/dist"

# Keep the Vercel project link (dist/.vercel) across rebuilds.
link="$here/.vercel-link"
rm -rf "$link"
if [ -d "$dist/.vercel" ]; then
    mv "$dist/.vercel" "$link"
fi

rm -rf "$dist"
mkdir -p "$dist/simulation/cad" "$dist/simulation/viewer" "$dist/public/viewer"

cp "$here/app.py" "$here/bitalino.py" "$here/requirements.txt" "$here/vercel.json" \
   "$here/.python-version" "$dist/"

# The simulation package: modules, scenarios, and the one CAD file it reads.
cp "$root"/simulation/*.py "$dist/simulation/"
cp -R "$root/simulation/scenarios" "$dist/simulation/scenarios"
cp -R "$root/simulation/cohort" "$dist/simulation/cohort"
cp "$root/simulation/cad/machine_geometry.json" "$dist/simulation/cad/"

# The Pi's code and the shipped presets it loads (config/ sits next to src/).
cp -R "$root/raspberry-pi/src" "$dist/src"
cp -R "$root/raspberry-pi/config" "$dist/config"

# Static pages. The battery report is optional: it exists once
# `python -m simulation.quick --all` has run.
cp "$root/simulation/viewer/index.html" "$dist/public/viewer/index.html"
cp "$root/simulation/viewer/index.html" "$dist/simulation/viewer/index.html"
if [ -f "$root/simulation/out/report.html" ]; then
    cp "$root/simulation/out/report.html" "$dist/public/report.html"
fi

if [ -d "$link" ]; then
    mv "$link" "$dist/.vercel"
fi

find "$dist" -name "__pycache__" -type d -prune -exec rm -rf {} +
find "$dist" -name "*.pyc" -delete

echo "assembled: $dist ($(du -sh "$dist" | cut -f1), $(find "$dist" -type f | wc -l | tr -d ' ') files)"
