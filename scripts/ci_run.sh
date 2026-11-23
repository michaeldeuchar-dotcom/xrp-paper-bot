#!/usr/bin/env bash
# One GitHub Actions slice of the PAPER run: restore state from the `data` branch, run the
# supervisor+bot for RUN_SECONDS, save state back to `data` every SAVE_EVERY seconds and at the end.
set -uo pipefail
cd "$(dirname "$0")/.."
RUN_SECONDS=${RUN_SECONDS:-20400}     # 5h40m
SAVE_EVERY=${SAVE_EVERY:-1200}        # 20 min
ST="${RUNNER_TEMP:-/tmp}/state"
unset $(env | grep -o '^POLY_[A-Za-z0-9_]*') 2>/dev/null || true   # paper only: never any wallet env

git config user.name "paper-bot"
git config user.email "paper-bot@users.noreply.github.com"

# ---- restore
# Live run writes only under data/week2. The finished week1 trial on the data branch
# is restored with the rest of data/ and must never be deleted, reset, or overwritten.
RUN=week2
mkdir -p data
if git ls-remote --exit-code --heads origin data >/dev/null 2>&1; then
  git fetch -q origin data
  git worktree add -f -B data "$ST" origin/data >/dev/null
  cp -a "$ST/data/." data/ 2>/dev/null || true
  echo "restored data folders: $(ls data | tr '\n' ' ')"
  echo "week1 preserved ($(ls data/week1 2>/dev/null | wc -l) files); $RUN: $(ls data/$RUN 2>/dev/null | tr '\n' ' ')"
else
  git worktree add --orphan -b data "$ST" >/dev/null 2>&1 || { mkdir -p "$ST"; git -C "$ST" init -q; }
  echo "no data branch yet; starting fresh"
fi
mkdir -p "data/$RUN"
# Only clear live pid/heartbeat for the NEW run. Never touch data/week1.
rm -f "data/$RUN/heartbeat.json" "data/$RUN"/*.pid STOP
# append tweak notes committed on main (tweaks/*.md) to the run's TWEAKS_LOG once each
for t in tweaks/*.md; do
  [ -f "$t" ] || continue
  marker=$(head -n 1 "$t")
  grep -qF "$marker" data/TWEAKS_LOG.md 2>/dev/null || { printf '\n' >> data/TWEAKS_LOG.md; cat "$t" >> data/TWEAKS_LOG.md; echo "tweak note added: $t"; }
done
if [ -f "data/$RUN/FINISHED" ]; then echo "$RUN already FINISHED"; exit 0; fi

save() {
  mkdir -p "$ST/data/$RUN"
  cp -a data/$RUN/trades_*.csv data/$RUN/auto_pick_switches.csv data/$RUN/bankroll_history.csv data/$RUN/state.json data/$RUN/coverage.json data/$RUN/heartbeat.json \
        "$ST/data/$RUN/" 2>/dev/null || true
  [ -f data/$RUN/FINISHED ] && cp data/$RUN/FINISHED "$ST/data/$RUN/"
  [ -f data/TWEAKS_LOG.md ] && cp data/TWEAKS_LOG.md "$ST/data/"
  tail -n 400 data/$RUN/bot.log > "$ST/data/$RUN/bot_tail.log" 2>/dev/null || true
  tail -n 200 data/$RUN/supervisor.log > "$ST/data/$RUN/supervisor.log" 2>/dev/null || true
  { head -n 1 data/$RUN/decisions.csv; tail -n 1500 data/$RUN/decisions.csv | tail -n +2; } > "$ST/data/$RUN/decisions_tail.csv" 2>/dev/null || true
  [ -s data/$RUN/stack_dumps.log ] && tail -n 300 data/$RUN/stack_dumps.log > "$ST/data/$RUN/stack_dumps.log"
  ( cd "$ST" && git add -A data && git commit -qm "state $(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M PT') run ${GITHUB_RUN_ID:-local}" \
      && for i in 1 2 3; do git push -q origin HEAD:data && break; sleep 5; done ) || true
  echo "$(TZ=America/Los_Angeles date '+%H:%M:%S PT') state saved"
}

python3 supervisor.py >> data/$RUN/supervisor.log 2>&1 &
SUP=$!
start=$(date +%s); last_save=$start
while kill -0 $SUP 2>/dev/null; do
  sleep 30
  now=$(date +%s)
  if [ $((now - last_save)) -ge "$SAVE_EVERY" ]; then
    grep HEARTBEAT data/$RUN/bot.log | tail -n 1; save; last_save=$now
  fi
  [ $((now - start)) -ge "$RUN_SECONDS" ] && break
done
echo "slice over; stopping bot"
touch STOP
for i in $(seq 1 40); do kill -0 $SUP 2>/dev/null || break; sleep 2; done
kill $SUP 2>/dev/null || true
rm -f STOP
save
tail -n 5 data/$RUN/supervisor.log
