#!/usr/bin/env bash
# Runs the collector every INTERVAL seconds for at most DURATION seconds and
# commits/pushes the new data after every snapshot.
# Used by GitHub Actions: GitHub's own cron is unreliable for short intervals
# (scheduled runs are delayed or dropped), so one job stays alive for ~5.5 h
# and the hourly cron only has to start the next job now and then.
set -u
INTERVAL=${INTERVAL:-600}
DURATION=${DURATION:-19800}   # 5.5 h (GitHub job limit is 6 h)
END=$(( $(date +%s) + DURATION ))

git config user.name  "collector-bot"
git config user.email "collector-bot@users.noreply.github.com"

while [ "$(date +%s)" -lt "$END" ]; do
  START=$(date +%s)
  python collector/collect.py || echo "collector failed (will retry next interval)"

  git add data/raw
  if ! git diff --cached --quiet; then
    git commit -q -m "data: snapshot $(date -u +%Y-%m-%dT%H:%MZ)"
    for i in 1 2 3 4 5; do
      git pull -q --rebase && git push -q && break
      sleep $((i * 5))
    done
  fi

  # sleep until the next interval, but never past the end
  NEXT=$(( START + INTERVAL ))
  NOW=$(date +%s)
  [ "$NEXT" -ge "$END" ] && break
  [ "$NEXT" -gt "$NOW" ] && sleep $(( NEXT - NOW ))
done
echo "loop finished"
