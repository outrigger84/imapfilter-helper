#!/usr/bin/env bash
# Locked entry point for unattended (systemd timer) invocations.
#
# Serializes scheduled runs against a shared flock so the inbox-sort and
# retention-sweep jobs never execute concurrently against the same
# cache.db / IMAP account: a run in progress makes the next one wait
# (blocking flock) instead of racing it. `timeout` caps how long a run may
# hold the lock so a wedged job can't block the schedule indefinitely.
#
# Usage: run_scheduled.sh <timeout-seconds> <cli-args...>
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="$REPO_DIR/data/.scheduler.lock"
TIMEOUT_SECONDS="$1"
shift

cd "$REPO_DIR"
exec flock "$LOCK_FILE" timeout "$TIMEOUT_SECONDS" ./imapfilter_helper.py "$@"
