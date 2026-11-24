#!/bin/bash
# Install gdbrpc on the local machine:
#   - creates the venv (.venv) and pip-installs the package into it
#   - gdbrpc install: ~/.config/gdbrpc/{.env,bootstrap.py}, plugins/,
#     ~/.gdbinit source line (all idempotent)
#
# Usage:
#   ./install.sh            # install
#   ./install.sh --update   # re-install (pull current code into the venv)
set -e
cd "$(dirname "$0")"

PYTHON=${PYTHON:-python3}

echo "== gdbrpc install =="
$PYTHON --version

if [ "$1" = "--update" ]; then
    echo "-- updating existing venv --"
    .venv/bin/pip install -e . >/dev/null
else
    if [ ! -x .venv/bin/pip ]; then
        echo "-- creating venv (.venv) --"
        $PYTHON -m venv .venv
    fi
    echo "-- pip install -e . --"
    .venv/bin/pip install -e .
fi

echo "-- gdbrpc install (config, shim, .gdbinit hook) --"
.venv/bin/gdbrpc install

echo
echo "== done =="
echo "  CLI:        .venv/bin/gdbrpc   (status | install | new-plugin)"
echo "  Test client: tools/rpc         (raw JSON-RPC)"
echo "  E2E test:    make test"
echo
echo "  Start gdb — the RPC server boots automatically via ~/.gdbinit."
echo "  Console panes:  .venv/bin/gdbrpc attach <view>"
echo "  Dashboards:    mps-tray -> gdbrpc view"