#!/usr/bin/env bash
# Step through the demo walkthrough of specs/07-demo.md in one terminal, against the api and
# stock-worker that scripts/start.sh runs in another terminal.
#
# Usage: scripts/demo.sh
#
# Before each step it prints a heading and the exact commands, waits for Enter, then runs them.
# A take starts from a clean database: stop scripts/start.sh and run scripts/start.sh --fresh.
set -euo pipefail

cd "$(dirname "$0")/.."

RUN_DIR=.run
API=http://127.0.0.1:8000
export ORDERS_STOCK_API_URL=$API
WORKER_LOG=$RUN_DIR/stock-worker.log
CURSOR=$RUN_DIR/feed-cursor

if [ -t 1 ]; then
    BOLD=$(printf '\033[1m') DIM=$(printf '\033[2m') RED=$(printf '\033[31m')
    GREEN=$(printf '\033[32m') YELLOW=$(printf '\033[33m') RESET=$(printf '\033[0m')
else
    BOLD='' DIM='' RED='' GREEN='' YELLOW='' RESET=''
fi

die() {
    printf '%sdemo.sh: %s%s\n' "$RED" "$*" "$RESET" >&2
    exit 1
}

# --- Presentation -----------------------------------------------------------------------------

# A banner for each part of the demo; the colour tells a failure scenario from normal operation.
part() {
    local colour=$1
    shift
    printf '\n%s%s' "$colour" "$BOLD"
    printf '############################################################################\n'
    printf '#  %s\n' "$@"
    printf '############################################################################%s\n' "$RESET"
}

# Throw away keys pressed while the previous step ran, so each pause waits for a fresh Enter.
# Non-canonical mode with no minimum and no timeout makes cat return at once with whatever is
# pending; bash 3.2's read -t takes whole seconds, which would delay every prompt. Piped input
# is left alone.
discard_typeahead() {
    local saved
    [ -t 0 ] || return 0
    saved=$(stty -g)
    stty -icanon min 0 time 0
    cat >/dev/null
    stty "$saved"
}

# step "D-nn - title" command...: show the commands, wait for Enter, then run them in turn.
step() {
    local title=$1 cmd
    shift
    printf '\n%s--- %s ---%s\n' "$BOLD" "$title" "$RESET"
    for cmd in "$@"; do
        printf '  $ %s\n' "$cmd"
    done
    discard_typeahead
    printf '%s[Enter] to run%s ' "$DIM" "$RESET"
    read -r _ || { printf '\n'; exit 0; }
    for cmd in "$@"; do
        printf '%s$ %s%s\n' "$YELLOW" "$cmd" "$RESET"
        run "$cmd" || printf '%s(exit status %s)%s\n' "$RED" "$?" "$RESET"
    done
}

# Run one displayed command. stdin is /dev/null so that no command swallows the Enter presses
# meant for this script. Long-running commands get their own process group: a background one
# (ending in &) then survives a Ctrl-C here, and the feed consumer, which runs until stopped, is
# stopped after FEED_SECONDS with SIGINT, as Ctrl-C would, reaching both uv and Python.
FEED_SECONDS=4
feed_pid=''
# A Ctrl-C here during that window must not leave the consumer running on its own.
trap '[ -z "$feed_pid" ] || kill -INT -- "-$feed_pid" 2>/dev/null' EXIT
run() {
    case $1 in
        *' &')
            set -m
            eval "$1" </dev/null
            set +m
            ;;
        *consume-feed*)
            set -m
            eval "$1 &" </dev/null
            feed_pid=$!
            set +m
            sleep "$FEED_SECONDS"
            kill -INT -- "-$feed_pid" 2>/dev/null
            wait "$feed_pid" 2>/dev/null
            feed_pid=''
            printf '%s(stopped with SIGINT after %s seconds)%s\n' "$DIM" "$FEED_SECONDS" "$RESET"
            ;;
        *) eval "$1" </dev/null ;;
    esac
}

# What the output above proves, for the presenter to point at.
point() {
    printf '%s' "$GREEN"
    printf '  > %s\n' "$@"
    printf '%s' "$RESET"
}

# Wait until the stock-worker log reports it has applied everything up to event $1, so that the
# next step reads settled stock however quickly Enter is pressed.
wait_for_stock() {
    local waited=0
    until grep -q "offset=$1 lag=0" "$WORKER_LOG"; do
        [ "$waited" -lt 20 ] || die "stock-worker has not applied event $1 within 10 seconds; see $WORKER_LOG."
        sleep 0.5
        waited=$((waited + 1))
    done
}

# The command that reads one resource, pretty-printed with jq when it is installed.
get() {
    if command -v jq >/dev/null 2>&1; then
        printf "curl -s '%s' | jq '%s'" "$API$1" "${2:-.}"
    else
        printf "curl -s -w '\\\\n' '%s'" "$API$1"
    fi
}

# The command that submits one order and shows the status line and headers of the response.
post() {
    printf "curl -si -w '\\\\n' %s/orders -H 'content-type: application/json' \\\\\n      -d '%s'" "$API" "$1"
}

# --- Preconditions ----------------------------------------------------------------------------

command -v curl >/dev/null 2>&1 || die "curl is not installed or not on PATH."
command -v uv >/dev/null 2>&1 || die "uv is not installed or not on PATH."
curl -s -o /dev/null --max-time 2 "$API/openapi.json" && [ -f "$WORKER_LOG" ] ||
    die "the API is not reachable at $API.
Start it first in another terminal with scripts/start.sh (scripts/start.sh --fresh for a clean take)."
worker_pid=$(cat "$RUN_DIR/stock-worker.pid" 2>/dev/null) && kill -0 -- "-$worker_pid" 2>/dev/null ||
    die "stock-worker is not running. Stop scripts/start.sh with Ctrl-C and run scripts/start.sh --fresh."
# The database scripts/start.sh runs against, so that the stock worker restarted in D-08 uses it too.
ORDERS_STOCK_DATABASE_URL=$(cat "$RUN_DIR/database-url" 2>/dev/null) ||
    die "$RUN_DIR/database-url is missing. Stop scripts/start.sh with Ctrl-C and run scripts/start.sh --fresh."
export ORDERS_STOCK_DATABASE_URL
status=$(curl -s -o /dev/null -w '%{http_code}' "$API/orders/web-100045")
[ "$status" = 404 ] ||
    die "this database already holds the demo's orders (GET /orders/web-100045 returned $status).
For a clean take, stop scripts/start.sh with Ctrl-C and run scripts/start.sh --fresh."

printf '%sOrders and stock: a step-by-step demo against %s%s\n' "$BOLD" "$API" "$RESET"
printf 'The api and stock-worker logs appear in the scripts/start.sh terminal.\n'

# --- Unhappy path 1 ---------------------------------------------------------------------------

part "$RED" "UNHAPPY PATH 1 of 2: duplicate submissions of the same order_ref"

step "D-03 - A burst of 11 submissions: 10 racing in parallel, with duplicates, then 1 conflict" \
    "uv run orders-stock burst"
point "6 orders created (201), 4 duplicates answered with the existing order (200)," \
    "1 reuse of web-100047 with different items rejected (409), 0 failed." \
    "Duplicates raced in parallel, and each order_ref still produced exactly one order." \
    "The stock-worker log shows events 1 to 6 applied, ending in offset=6 lag=0."
wait_for_stock 6

step "D-05 - One order, and the stock of BAN-001 after the burst" \
    "$(get /orders/web-100045)" \
    "$(get /stock/BAN-001)"
point "web-100045 was submitted 3 times, holds BAN-001 x2 and APL-003 x1, and is stock_committed." \
    "BAN-001 was in 8 submissions but only 3 accepted orders (2 + 1 + 3 units):" \
    "on_hand is 50 - 6 = 44, as of event 6, so each accepted order counted exactly once."

step "D-06 - A client retry with the items reordered, then a conflicting reuse (qty 5)" \
    "$(post '{"order_ref":"web-100045","customer_id":"cust-42","items":[{"sku":"APL-003","qty":1},{"sku":"BAN-001","qty":2}]}')" \
    "$(post '{"order_ref":"web-100045","customer_id":"cust-42","items":[{"sku":"APL-003","qty":1},{"sku":"BAN-001","qty":5}]}')" \
    "$(get /stock/BAN-001)"
point "The retry is 200 OK with the same order: item order does not make a different order." \
    "The conflicting reuse is 409 Conflict, application/problem+json, code order_ref_conflict." \
    "BAN-001 still shows on_hand 44: neither request touched stock."

# --- Unhappy path 2 ---------------------------------------------------------------------------

part "$RED" "UNHAPPY PATH 2 of 2: the stock worker is down for a while, then catches up"

step "D-07 - Kill the stock worker with SIGKILL: no chance to shut down cleanly" \
    "pkill -9 -f 'orders-stock stock-worker'"
point "The scripts/start.sh terminal reports that stock-worker is down; nothing restarts it."

step "D-07 - Keep selling: a second burst while the stock worker is down" \
    "uv run orders-stock burst --prefix outage"
point "The same summary as before: intake does not depend on the stock worker."

step "D-07 - The order is accepted, the stock is stale and says so, the feed moves on" \
    "$(get /orders/outage-100045 .status)" \
    "$(get /stock/BAN-001)" \
    "$(get '/order-events?after=6' '[.events[].event_id]')"
point "outage-100045 is \"accepted\": its stock is not applied yet." \
    "BAN-001 still reads on_hand 44 as_of_event_id 6: stale, and as_of_event_id says how stale." \
    "Meanwhile the feed already holds events 7 to 12, one per new order."

worker_lines=$(wc -l <"$WORKER_LOG")
step "D-08 - Restart the stock worker and watch it catch up" \
    "uv run orders-stock stock-worker >>$WORKER_LOG 2>&1 &"
# Recorded where scripts/start.sh finds it, so its Ctrl-C stops this worker too.
echo "$!" >"$RUN_DIR/stock-worker.pid"
wait_for_stock 12
printf '%sstock-worker log since the restart:%s\n' "$DIM" "$RESET"
tail -n "+$((worker_lines + 1))" "$WORKER_LOG"
point "It resumed from its stored offset 6 with 6 events to apply, and applied 7 to 12 in one go."

step "D-08 - Stock and order status after the catch-up" \
    "for sku in APL-003 BAN-001 BRD-004 MLK-002; do curl -s -w '\\n' $API/stock/\$sku; done" \
    "$(get /orders/outage-100045 .status)"
point "on_hand is 34, 38, 14 and 20, all as of event 12: exactly the values without the outage," \
    "the seed minus both bursts' quantities, and outage-100045 is now \"stock_committed\"."

# --- Normal operation -------------------------------------------------------------------------

part "$GREEN" "THE INTEGRATION SURFACE: the order-accepted feed, working normally"

step "D-04 - Another team's system reads the feed from the start" \
    "uv run orders-stock consume-feed --from-start --cursor-file $CURSOR"
point "12 events for the 12 orders, not one per submission (24 so far), in commit order;" \
    "events 7 to 12 were published while the stock worker was down."

step "D-09 - A new order, and the consumer resumes from its cursor" \
    "$(post '{"order_ref":"web-100051","customer_id":"cust-5","items":[{"sku":"MLK-002","qty":2}]}')" \
    "uv run orders-stock consume-feed --cursor-file $CURSOR"
point "The consumer starts after event 12, replays nothing, and prints only event 13." \
    "The stock-worker log shows applied events 13..13."

printf '\n%sThe demo is complete.%s\n' "$BOLD" "$RESET"
printf 'The acceptance suite covers the same scenarios: uv run pytest -v\n'
printf 'For another take, stop scripts/start.sh with Ctrl-C and run scripts/start.sh --fresh.\n'
