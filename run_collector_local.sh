#!/bin/zsh
# Runs the collector every 10 minutes on this computer (fallback / backup to
# GitHub Actions). Keeps the Mac awake while running (caffeinate).
# Start:  nohup ./run_collector_local.sh > collector.log 2>&1 &
# Stop:   kill $(cat collector.pid)
cd "$(dirname "$0")"
echo $$ > collector.pid
while true; do
  caffeinate -i -t 60 .venv/bin/python collector/collect.py --data-dir data/raw_local --state state/local_state.json.gz
  sleep 600
done
