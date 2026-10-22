#!/bin/sh
# Stop the recorder started by scripts/run_recorder.sh. The recorder flushes its buffers on SIGTERM.
cd "$(dirname "$0")/.." || exit 1
if [ -f logs/recorder.pid ]; then
  kill -TERM "$(cat logs/recorder.pid)" && echo "stop signal sent to supervisor $(cat logs/recorder.pid)"
else
  echo "no logs/recorder.pid: recorder is not running from run_recorder.sh"
fi
