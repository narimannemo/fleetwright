#!/usr/bin/env bash
# Eight workers, no coordinator, no broker.
#
# `claim` exits 1 with no output when the queue is dry, so each subshell ends
# on its own and `wait` returns when the corpus is done. A worker killed
# mid-unit has its lease reclaimed by whoever asks next.
#
# Note `--worker "$me"` on the claim AND on the close. It is not decoration:
# each of these is a separate process, so there is no per-process identity
# that survives from one command to the next, and a close that cannot prove
# whose unit it is gets refused. One name per subshell, used by both.
set -uo pipefail

DB=${DB:-work.db}
KIND=${KIND:-extract}
N=${N:-8}

fleetwright add "$KIND" --from-file "${1:?usage: fleet.sh <units-file>}" --db "$DB"

for i in $(seq 1 "$N"); do
  (
    me="worker-$i"
    while unit=$(fleetwright claim "$KIND" --json --lease 1800 --db "$DB" \
                   --worker "$me"); do
      id=$(printf '%s' "$unit" | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["unit_id"])')
      name=$(printf '%s' "$unit" | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["name"])')

      if ./do-the-work "$name"; then
        fleetwright done "$id" --db "$DB" --worker "$me"
      else
        fleetwright fail "$id" --note "exited $?" --db "$DB" --worker "$me"
      fi
    done
  ) &
done

wait
fleetwright status --who --db "$DB"
