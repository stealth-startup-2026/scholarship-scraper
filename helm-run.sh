#!/usr/bin/env bash
# helm-run.sh - the entrypoint helm's GitHub Actions workflow calls.
# helm stays generic: it dispatches the workflow with a `target`; THIS script
# (owned by the repo) decides what running each target means. Fill in the real
# commands below - you can edit this file straight from helm's scrapers cockpit.
set -euo pipefail
TARGET="${1:-all}"
echo "helm-run: target=$TARGET"
case "$TARGET" in
  usyd) echo "TODO: run usyd scraper, e.g. python usyd/usyd_scraper.py" ;;
  unsw) echo "TODO: run unsw scraper, e.g. python unsw/unsw_scraper.py" ;;
  all)  echo "TODO: run all scrapers" ;;
  *)    echo "unknown target: $TARGET" >&2; exit 1 ;;
esac
