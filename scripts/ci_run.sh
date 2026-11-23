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
mkdir -p data/week1
if git ls-remote --exit-code --heads origin data >/dev/null 2>&1; then
  git fetch -q origin data
  git worktree add -f -B data "$ST" origin/data >/dev/null
  cp -a "$ST/data/." data/ 2>/dev/null || true
  echo "restored state: $(ls data/week1 | tr '\n' ' ')"
else
  git worktree add --orphan -b data "$ST" >/dev/null 2>&1 || { mkdir -p "$ST"; git -C "$ST" init -q; }
  echo "no data branch yet; starting fresh"
fi
rm -f data/week1/heartbeat.json data/week1/*.pid STOP
# append tweak notes committed on main (tweaks/*.md) to the run's TWEAKS_LOG once each
for t in tweaks/*.md; do
  [ -f "$t" ] || continue
  marker=$(head -n 1 "$t")
  grep -qF "$marker" data/TWEAKS_LOG.md 2>/dev/null || { printf '\n' >> data/TWEAKS_LOG.md; cat "$t" >> data/TWEAKS_LOG.md; echo "tweak note added: $t"; }
done
if [ -f data/week1/FINISHED ]; then echo "run already FINISHED"; exit 0; fi

save() {
  mkdir -p "$ST/data/week1"
  cp -a data/week1/trades_*.csv data/week1/auto_pick_switches.csv data/week1/bankroll_history.csv data/week1/state.json data/week1/coverage.json data/week1/heartbeat.json \
        "$ST/data/week1/" 2>/dev/null || true
  [ -f data/week1/FINISHED ] && cp data/week1/FINISHED "$ST/data/week1/"
  [ -f data/TWEAKS_LOG.md ] && cp data/TWEAKS_LOG.md "$ST/data/"
  tail -n 400 data/week1/bot.log > "$ST/data/week1/bot_tail.log" 2>/dev/null || true
  tail -n 200 data/week1/supervisor.log > "$ST/data/week1/supervisor.log" 2>/dev/null || true
  { head -n 1 data/week1/decisions.csv; tail -n 1500 data/week1/decisions.csv | tail -n +2; } > "$ST/data/week1/decisions_tail.csv" 2>/dev/null || true
  [ -s data/week1/stack_dumps.log ] && tail -n 300 data/week1/stack_dumps.log > "$ST/data/week1/stack_dumps.log"
  ( cd "$ST" && git add -A data && git commit -qm "state $(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M PT') run ${GITHUB_RUN_ID:-local}" \
      && for i in 1 2 3; do git push -q origin HEAD:data && break; sleep 5; done ) || true
  echo "$(TZ=America/Los_Angeles date '+%H:%M:%S PT') state saved"
}

python3 supervisor.py >> data/week1/supervisor.log 2>&1 &
SUP=$!
start=$(date +%s); last_save=$start
while kill -0 $SUP 2>/dev/null; do
  sleep 30
  now=$(date +%s)
  if [ $((now - last_save)) -ge "$SAVE_EVERY" ]; then
    grep HEARTBEAT data/week1/bot.log | tail -n 1; save; last_save=$now
  fi
  [ $((now - start)) -ge "$RUN_SECONDS" ] && break
done
echo "slice over; stopping bot"
touch STOP
for i in $(seq 1 40); do kill -0 $SUP 2>/dev/null || break; sleep 2; done
kill $SUP 2>/dev/null || true
rm -f STOP
save
tail -n 5 data/week1/supervisor.log
