#!/bin/bash -p
# -p prevents Bash from implicitly sourcing BASH_ENV before this script.
set -euo pipefail

if (( $# == 0 )); then
    echo 'with_app_paths: expected a command and optional arguments' >&2
    exit 2
fi

app_storage_exports=$(/usr/bin/awk '
    $0 == "# app-storage:begin" { begin++; active = 1; next }
    $0 == "# app-storage:end" { end++; if (!active) invalid = 1; active = 0; next }
    active { block = block $0 "\n" }
    END {
        if (begin != 1 || end != 1 || active || invalid) {
            print "with_app_paths: expected one complete app-storage block in ~/.bashrc" > "/dev/stderr"
            exit 1
        }
        printf "%s", block
    }
' "${HOME:?}/.bashrc")
# The user-maintained block is trusted shell code; nothing outside it is evaluated.
eval "$app_storage_exports"
exec "$@"
