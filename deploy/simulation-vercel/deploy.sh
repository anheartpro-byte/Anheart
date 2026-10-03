#!/usr/bin/env bash
# Build and deploy the hosted simulation engine to Vercel.
#
#     deploy/simulation-vercel/deploy.sh            # preview deployment
#     deploy/simulation-vercel/deploy.sh --prod     # production deployment
#
# The Vercel login used here lives in <repo>/.vercel-cli (gitignored), so it
# is this project's login and not the machine-wide one. Create it once with:
#
#     npx vercel login --global-config .vercel-cli
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
config="$root/.vercel-cli"
project="${VERCEL_SIM_PROJECT:-anheart-simulation}"

if [ ! -f "$config/auth.json" ]; then
    echo "No Vercel login in $config. Run: npx vercel login --global-config .vercel-cli" >&2
    exit 1
fi

"$here/build.sh"

cd "$here/dist"
if [ ! -d .vercel ]; then
    npx -y vercel@latest link --yes --project "$project" --global-config "$config"
fi
npx -y vercel@latest deploy --yes --global-config "$config" "$@"
