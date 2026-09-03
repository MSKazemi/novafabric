#!/usr/bin/env bash
# pytest-watcher --runner target. pytest-watcher appends its own pytest args,
# which the scoped selector supplies itself, so they are deliberately ignored.
exec "$(dirname "$0")/run-scoped-tests.sh" direct
