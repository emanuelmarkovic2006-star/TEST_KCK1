#!/usr/bin/env bash
# The "keep working until it's all done, even if it gets interrupted" runner.
#
# This is deliberately just a shell loop, not a Claude Code agent loop —
# it doesn't need any AI to just keep retrying a script. Save the AI
# for the parts that actually need judgment (patterns, model tuning).
#
# What it does:
#   - Calls ingest_all.py repeatedly.
#   - progress.json means each call only does the work that isn't done yet.
#   - If a run fails outright (network down, rate-limited, whatever),
#     it waits and tries again instead of giving up.
#   - Stops automatically once a full pass reports 0 remaining jobs.
#
# Usage:
#   chmod +x run_forever.sh
#   ./run_forever.sh                  # 5 seasons, all leagues in config/leagues.csv
#   ./run_forever.sh 10               # 10 seasons instead
#
# Leave it running in a terminal (or `nohup ./run_forever.sh &` to
# background it, or run it via GitHub Actions / Claude Code for a truly
# unattended run).

SEASONS="${1:-5}"
DB="football.db"
SLEEP_ON_FAIL=60
MAX_CONSECUTIVE_FAILS=10
fails=0

echo "=== Starting full ingest: ${SEASONS} seasons, all leagues in config/leagues.csv ==="

# make sure the DB exists with the schema applied
if [ ! -f "$DB" ]; then
  python3 -c "import sqlite3; c=sqlite3.connect('$DB'); c.executescript(open('schema/schema.sql').read())"
  echo "Created $DB from schema."
fi

while true; do
  OUTPUT=$(python3 ingest_all.py --db "$DB" --seasons "$SEASONS" 2>&1)
  echo "$OUTPUT"

  REMAINING=$(python3 -c "
import json, csv
try:
    p = json.load(open('progress.json'))
except FileNotFoundError:
    p = {'completed': []}
with open('config/leagues.csv') as f:
    leagues = list(csv.DictReader(f))
n_leagues = len(leagues)
completed = len(p['completed'])
print(max(n_leagues*${SEASONS} - completed, 0))
")

  if [ "$REMAINING" -le 0 ]; then
    echo ""
    echo "=== All jobs complete. Nothing left to do. ==="
    python3 -c "
import sqlite3
c = sqlite3.connect('$DB')
n = c.execute('SELECT COUNT(*) FROM matches').fetchone()[0]
leagues = c.execute('SELECT COUNT(DISTINCT league_id) FROM matches').fetchone()[0]
print(f'Final: {n} matches across {leagues} leagues in {\"$DB\"}')
"
    break
  fi

  fails=$((fails+1))
  if [ "$fails" -ge "$MAX_CONSECUTIVE_FAILS" ]; then
    echo "Hit $MAX_CONSECUTIVE_FAILS consecutive incomplete passes without progress. Stopping — check progress.json for what's failing repeatedly (likely a genuinely dead league code, not a transient issue)."
    break
  fi

  echo ""
  echo "$REMAINING jobs still remaining. Waiting ${SLEEP_ON_FAIL}s before continuing..."
  sleep "$SLEEP_ON_FAIL"
done
