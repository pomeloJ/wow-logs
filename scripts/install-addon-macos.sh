#!/bin/bash
# Copy the addon into the game. Usage: scripts/install-addon-macos.sh [WoW folder]
set -euo pipefail
cd "$(dirname "$0")/.."
WOW="${1:-/Applications/World of Warcraft}"
ADDONS="$WOW/_forever_/Interface/AddOns"
[ -d "$ADDONS" ] || { echo "AddOns folder not found: $ADDONS"; exit 1; }
rm -rf "$ADDONS/AdventureCollector"
cp -R addon/AdventureCollector "$ADDONS/"
[ -f "$ADDONS/AdventureCollector/AdventureCollector.toc" ] && echo "installed to $ADDONS/AdventureCollector"
