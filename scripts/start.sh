#!/usr/bin/env bash
# Start the service locally in one terminal: check PostgreSQL, install dependencies, apply the
# migrations, seed the catalogue, then run `api` and `stock-worker` until Ctrl-C.
#
# Usage: scripts/start.sh [--docker] [--fresh]
#
# Both processes log to this terminal and to .run/<process>.log; scripts/demo.sh reads the stock
# worker's.
# The stock worker is never restarted automatically: the demo stops it on purpose and restarts it
# deliberately. Ctrl-C stops every api and stock-worker recorded in .run/, including one the demo
# restarted.
set -euo pipefail

cd "$(dirname "$0")/.."

RUN_DIR=.run
API=http://127.0.0.1:8000
export ORDERS_STOCK_API_URL=$API
export ORDERS_STOCK_DATABASE_URL=${ORDERS_STOCK_DATABASE_URL:-postgresql://orders_stock:orders_stock@localhost:5432/orders_stock}

usage() {
    cat <<'EOF'
Usage: scripts/start.sh [--docker] [--fresh]

Starts the api and stock-worker processes against PostgreSQL, as README.md describes.

  --docker  first start PostgreSQL with compose.yaml (docker compose up -d --wait)
  --fresh   drop and recreate the local database first, for a clean demo take
            (the "Starting clean" procedure in specs/03-architecture.md)

ORDERS_STOCK_DATABASE_URL selects the database; it defaults to
postgresql://orders_stock:orders_stock@localhost:5432/orders_stock
EOF
}

if [ -t 1 ]; then
    BOLD=$(printf '\033[1m') RED=$(printf '\033[31m') CYAN=$(printf '\033[36m')
    MAGENTA=$(printf '\033[35m') RESET=$(printf '\033[0m')
else
    BOLD='' RED='' CYAN='' MAGENTA='' RESET=''
fi

say() { printf '%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
die() {
    printf '%sstart.sh: %s%s\n' "$RED" "$*" "$RESET" >&2
    exit 1
}

use_docker=false
fresh=false
for arg in "$@"; do
    case $arg in
        --docker) use_docker=true ;;
        --fresh) fresh=true ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

# --- Prerequisites ----------------------------------------------------------------------------

need() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is not installed or not on PATH. $2"
}
need uv "Install it as README.md 'Prerequisites' describes."
need curl "Install it with the system package manager."
if $use_docker; then
    need docker "Install Docker, or leave out --docker to use your own PostgreSQL."
    docker compose version >/dev/null 2>&1 || die "docker compose is not available; --docker needs Docker Compose v2."
elif $fresh; then
    need dropdb "--fresh uses the PostgreSQL client tools; see README.md 'PostgreSQL' (on macOS the Homebrew keg is not on PATH by default)."
    need createdb "--fresh uses the PostgreSQL client tools; see README.md 'PostgreSQL'."
fi

api_answers() { curl -s -o /dev/null --max-time 2 "$API/openapi.json"; }

# A process group recorded in .run/<name>.pid that still has a live member.
running() {
    local pid
    pid=$(cat "$RUN_DIR/$1.pid" 2>/dev/null) || return 1
    [ -n "$pid" ] && kill -0 -- "-$pid" 2>/dev/null
}

if running api || running stock-worker; then
    die "scripts/start.sh already runs in another terminal; stop it with Ctrl-C first."
fi
if api_answers; then
    die "something already answers at $API; stop it first (an 'orders-stock api' started by hand?)."
fi
mkdir -p "$RUN_DIR"
rm -f "$RUN_DIR"/*.pid

# --- Dependencies and PostgreSQL -----------------------------------------------------------

say "Installing dependencies"
uv sync

# ORDERS_STOCK_DATABASE_URL with the password hidden, for messages.
shown_url=$(printf '%s' "$ORDERS_STOCK_DATABASE_URL" | sed -E 's#(//[^:/@]+):[^@]*@#\1:***@#')

# Everything below hands ORDERS_STOCK_DATABASE_URL to libpq, so first check that libpq can read it
# at all; a SQLAlchemy-style postgresql+driver:// address is a common mistake.
uv run --quiet python -c '
import os
from psycopg.conninfo import conninfo_to_dict
conninfo_to_dict(os.environ["ORDERS_STOCK_DATABASE_URL"])' >/dev/null 2>&1 ||
    die "ORDERS_STOCK_DATABASE_URL is set to $shown_url, which PostgreSQL's tools cannot read (a SQLAlchemy-style postgresql+driver:// address is a common cause).
Unset it (unset ORDERS_STOCK_DATABASE_URL) to use the demo database, or set it to a postgresql:// address."

# One libpq parameter (host, port, user, dbname) of ORDERS_STOCK_DATABASE_URL, parsed by libpq itself.
db_param() {
    uv run --quiet python -c '
import sys
from psycopg.conninfo import conninfo_to_dict
from orders_stock.config import Settings
print(conninfo_to_dict(Settings.from_env().database_url).get(sys.argv[1]) or "")' "$1"
}

# --fresh drops a database, so refuse before touching anything unless ORDERS_STOCK_DATABASE_URL
# names the demo database itself: exactly orders_stock, the documented default, on a local host.
# Any other name may belong to another project.
if $fresh; then
    host=$(db_param host) port=$(db_param port) user=$(db_param user) dbname=$(db_param dbname)
    case $host in
        '' | localhost | 127.0.0.1 | ::1 | /*) ;;
        *) die "--fresh drops the database, so it only runs against a local one; ORDERS_STOCK_DATABASE_URL points at host '$host'." ;;
    esac
    [ "$dbname" = orders_stock ] ||
        die "--fresh only drops the demo database orders_stock, but ORDERS_STOCK_DATABASE_URL names '${dbname:-(none)}'; nothing was dropped."
    [ -n "$user" ] || die "--fresh needs ORDERS_STOCK_DATABASE_URL to name the role that owns the database."
    if $use_docker && [ "${port:-5432}" != 5432 ]; then
        die "--docker --fresh expects the compose database on port 5432; ORDERS_STOCK_DATABASE_URL uses port $port."
    fi
fi

if $use_docker; then
    say "Starting PostgreSQL with compose.yaml"
    docker compose up -d --wait
fi

say "Checking PostgreSQL at $shown_url"
if ! error=$(uv run --quiet python -c '
import psycopg
from orders_stock.config import Settings
psycopg.connect(Settings.from_env().database_url, connect_timeout=5).close()' 2>&1); then
    printf '%s\n' "$error" | tail -n 1 >&2
    die "cannot connect to PostgreSQL at $shown_url.
Start PostgreSQL and create the role and databases as README.md 'PostgreSQL' describes,
or run scripts/start.sh --docker to start PostgreSQL in a container with compose.yaml."
fi

if $fresh; then
    # "Starting clean": nothing runs against the database (checked above), so drop and recreate it.
    say "Recreating database $dbname (--fresh)"
    if $use_docker; then
        docker compose exec -T postgres dropdb -U "$user" --if-exists "$dbname"
        docker compose exec -T postgres createdb -U "$user" "$dbname"
    else
        # The same commands as "Starting clean", run as the ORDERS_STOCK_DATABASE_URL role, which owns
        # the database and has CREATEDB; the maintenance connection reuses its host and port. The
        # password goes only into their environment, never onto a command line that ps can show.
        maintenance=$(uv run --quiet python -c '
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from orders_stock.config import Settings
params = conninfo_to_dict(Settings.from_env().database_url)
params.pop("password", None)
print(make_conninfo(**{**params, "dbname": "postgres"}))')
        password=$(db_param password)
        with_password() {
            if [ -n "$password" ]; then PGPASSWORD=$password "$@"; else "$@"; fi
        }
        with_password dropdb --if-exists --maintenance-db="$maintenance" "$dbname"
        with_password createdb --maintenance-db="$maintenance" -O "$user" "$dbname"
    fi
    # Event IDs restart at 1, so the demo's feed consumer must start again too.
    rm -f "$RUN_DIR/feed-cursor"
fi

say "Applying migrations (on macOS the first run can pause for several seconds)"
uv run orders-stock migrate
say "Seeding the demo catalogue"
uv run orders-stock seed

# --- Processes --------------------------------------------------------------------------------

log_pids=''

# Follow .run/<name>.log in this terminal with a prefix per line. It keeps following across a
# restart of the process, because a restarted process appends to the same file.
show_log() {
    set -m # its own process group, so Ctrl-C does not cut off the processes' shutdown lines
    (tail -n +1 -F "$RUN_DIR/$1.log" 2>/dev/null | awk -v p="$2" '{ print p $0; fflush() }') &
    set +m
    log_pids="$log_pids $!"
}

# Run `orders-stock <name>` in its own process group, recorded in .run/<name>.pid, so that it
# survives a Ctrl-C in the demo's terminal and cleanup can stop the uv wrapper and Python together.
start_process() {
    set -m
    uv run orders-stock "$1" >>"$RUN_DIR/$1.log" 2>&1 </dev/null &
    set +m
    echo "$!" >"$RUN_DIR/$1.pid"
    disown # the watch loop below reports its end, not bash's own "Killed: 9" notice
}

cleanup() {
    trap - EXIT
    trap '' INT # a second Ctrl-C must not cut the shutdown short
    set +e
    local name pid waited=0
    printf '\n'
    say "Stopping api and stock-worker"
    for name in api stock-worker; do
        if running "$name"; then
            kill -TERM -- "-$(cat "$RUN_DIR/$name.pid")" 2>/dev/null
        fi
    done
    while { running api || running stock-worker; } && [ "$waited" -lt 20 ]; do
        sleep 0.5
        waited=$((waited + 1))
    done
    for name in api stock-worker; do
        if running "$name"; then
            pid=$(cat "$RUN_DIR/$name.pid")
            say "$name did not stop within 10 seconds; killing it"
            kill -KILL -- "-$pid" 2>/dev/null
        fi
        rm -f "$RUN_DIR/$name.pid"
    done
    sleep 0.3 # let the log followers print the last lines
    for pid in $log_pids; do
        kill -- "-$pid" 2>/dev/null
    done
    say "Stopped"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# The database for scripts/demo.sh, which restarts the stock worker; owner-only, as the URL can
# hold a password.
(umask 077 && printf '%s\n' "$ORDERS_STOCK_DATABASE_URL" >"$RUN_DIR/database-url")
: >"$RUN_DIR/api.log"
: >"$RUN_DIR/stock-worker.log"
show_log api "${CYAN}api          |${RESET} "
show_log stock-worker "${MAGENTA}stock-worker |${RESET} "

start_process api
start_process stock-worker

waited=0
until api_answers; do
    running api || die "api exited during start-up; its log is above and in $RUN_DIR/api.log."
    [ "$waited" -lt 60 ] || die "api did not answer at $API within 30 seconds."
    sleep 0.5
    waited=$((waited + 1))
done
waited=0
until grep -q 'stock-worker started' "$RUN_DIR/stock-worker.log"; do
    running stock-worker || die "stock-worker exited during start-up; its log is above and in $RUN_DIR/stock-worker.log."
    [ "$waited" -lt 60 ] || die "stock-worker did not start within 30 seconds."
    sleep 0.5
    waited=$((waited + 1))
done

say "API listening on $API (interactive docs at $API/docs)"
say "Logs: $RUN_DIR/api.log and $RUN_DIR/stock-worker.log. Run scripts/demo.sh in another terminal."
say "Press Ctrl-C to stop api and stock-worker."

# Watch the processes. The api stopping ends the run; the stock worker stopping does not, and it
# is not restarted here: the demo restarts it deliberately, and this loop reports what happened.
worker_up=true
while :; do
    running api || die "api stopped unexpectedly; its log is above and in $RUN_DIR/api.log."
    if running stock-worker; then
        $worker_up || say "stock-worker is running again (pid $(cat "$RUN_DIR/stock-worker.pid"))"
        worker_up=true
    elif $worker_up; then
        say "${RED}stock-worker is down.${RESET}${BOLD} It is not restarted automatically; orders are still accepted."
        worker_up=false
    fi
    sleep 1
done
