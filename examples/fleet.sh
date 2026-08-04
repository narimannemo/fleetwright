#!/usr/bin/env bash
# Eight workers, no coordinator, no broker.
#
# `claim` exits 1 with no output when the queue is dry, so each subshell ends
# on its own and `wait` returns when the corpus is done. A worker killed
# mid-unit has its lease reclaimed by whoever asks next.
set -uo pipefail

DB=${DB:-work.db}
KIND=${KIND:-extract}
N=${N:-8}

superagentic add "$KIND" --from-file "${1:?usage: fleet.sh <units-file>}" --db "$DB"

for _ in $(seq 1 "$N"); do
  (
    while unit=$(superagentic claim "$KIND" --json --lease 1800 --db "$DB"); do
      id=$(printf '%s' "$unit" | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["unit_id"])')
      name=$(printf '%s' "$unit" | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["name"])')

      if ./do-the-work "$name"; then
        superagentic done "$id" --db "$DB"
      else
        superagentic fail "$id" --note "exited $?" --db "$DB"
      fi
    done
  ) &
done

wait
superagentic status --who --db "$DB"
