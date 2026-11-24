#!/bin/bash
# Usage: scripts/run-agent-macos.sh [run|status|test-connection|sync-once|validate]
cd "$(dirname "$0")/../agent" && exec ../.venv/bin/python -m adventuresync "${1:-run}"
