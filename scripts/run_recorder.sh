#!/bin/sh
# Keep the orderbook recorder running for days: restart it after a crash, and keep
# the Mac awake while it runs (caffeinate -i: no idle sleep; -s: no system sleep on AC power).
# A closed lid on battery still sleeps the Mac; keep it on power.
#
#   scripts/run_recorder.sh &        # start (writes logs/recorder.pid)
#   scripts/stop_recorder.sh         # stop cleanly (buffers are flushed)
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
echo $$ > logs/recorder.pid
child=""
trap 'echo "$(date -u +%FT%TZ) supervisor stopping" >> logs/recorder.log; [ -n "$child" ] && kill -TERM "$child"; wait "$child"; rm -f logs/recorder.pid; exit 0' TERM INT
while true; do
  echo "$(date -u +%FT%TZ) starting recorder" >> logs/recorder.log
  caffeinate -is .venv/bin/python manage.py record_orderbook >> logs/recorder.log 2>&1 &
  child=$!
  wait "$child"
  status=$?
  echo "$(date -u +%FT%TZ) recorder exited with status $status; restarting in 10s" >> logs/recorder.log
  sleep 10
done
