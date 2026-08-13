#!/usr/bin/env bash
# Wipes build/ in every <tool>/sizif dir, for testing that build_all.sh
# regenerates everything from tracked source alone.
set -euo pipefail

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec > >(tee "$REPO_PATH/clean_all.log") 2>&1

"$REPO_PATH/vivado/sizif/clean.sh"
"$REPO_PATH/vitis/sizif/clean.sh"
"$REPO_PATH/pc_app/sizif/clean.sh"

echo
echo "All build/ dirs wiped clean."
