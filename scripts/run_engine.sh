#!/bin/sh
# Supervisor for the trading engine: restarts it after a crash, keeps the Mac awake, writes a pid file.
#   scripts/run_engine.sh shadow &      or      scripts/run_engine.sh live &
#   kill -TERM $(cat logs/engine_<mode>.pid)    to stop
MODE=${1:?usage: run_engine.sh shadow|live}
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
echo $$ > "logs/engine_${MODE}.pid"
child=""
trap 'echo "$(date -u +%FT%TZ) supervisor stopping" >> "logs/engine_${MODE}.log"; [ -n "$child" ] && kill -TERM "$child"; wait "$child"; rm -f "logs/engine_${MODE}.pid"; exit 0' TERM INT
while true; do
  echo "$(date -u +%FT%TZ) starting engine ${MODE}" >> "logs/engine_${MODE}.log"
  caffeinate -is .venv/bin/python manage.py run_engine --mode "${MODE}" >> "logs/engine_${MODE}.log" 2>&1 &
  child=$!
  wait "$child"
  status=$?
  echo "$(date -u +%FT%TZ) engine exited with status $status" >> "logs/engine_${MODE}.log"
  [ "$status" -eq 0 ] && break      # normal end (after the challenge end)
  sleep 10
done
rm -f "logs/engine_${MODE}.pid"
