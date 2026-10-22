#!/bin/sh
# Go-live in one command (RUNBOOK.md §2): code-freeze check -> preflight -> smoke test (typed YES) ->
# typed GO LIVE -> tag + push -> start the live engine (detached; sizes x0.5 for the first 2 h).
# Needs CHALLENGE_END (or [challenge] end) and the prop account's WALLEX_API_KEY in .env.
set -eu
cd "$(dirname "$0")/.."
PY=.venv/bin/python
TAG=${TAG:-live-v1}

echo "== 1/6 code freeze: engine/strategy/risk/agent/config/execution must be committed"
if [ -n "$(git status --porcelain -- execution strategy risk agent config models)" ]; then
  git status --short -- execution strategy risk agent config models
  echo "uncommitted trading code: commit first"; exit 1
fi
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && [ "$(git rev-parse "$TAG^{commit}")" != "$(git rev-parse HEAD)" ]; then
  echo "tag $TAG exists on another commit: run with TAG=live-v2 (and add a CHANGELOG entry)"; exit 1
fi

echo "== 2/6 preflight (live)"
$PY manage.py preflight --mode live

echo "== 3/6 smoke test (one tiny LIMIT BUY 5% under the bid, then cancel)"
$PY manage.py smoke_test

echo "== 4/6 confirmation"
printf "Start LIVE trading on the prop account with commit %s? Type GO LIVE: " "$(git rev-parse --short HEAD)"
read -r answer
[ "$answer" = "GO LIVE" ] || { echo "not confirmed: live not started"; exit 1; }

echo "== 5/6 tag $TAG and push"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || git tag -a "$TAG" -m "Code that trades live (go-live $(date -u +%FT%TZ))"
git push origin "$TAG"

echo "== 6/6 start the live engine (detached, own session; shadow keeps running)"
[ -f logs/engine_live.pid ] && kill -0 "$(cat logs/engine_live.pid)" 2>/dev/null && { echo "live engine already running"; exit 1; }
LIVE_TRADING_CONFIRMED=true $PY -c "import subprocess; subprocess.Popen(['scripts/run_engine.sh', 'live'], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
sleep 20
echo "heartbeat: $(cat logs/heartbeat_live.txt 2>/dev/null || echo 'not yet')"
tail -n 3 "journal/live/decisions-$(TZ=Asia/Tehran date +%Y%m%d).jsonl" 2>/dev/null || true
echo "live started. Dashboard: make api + make dashboard -> http://localhost:3000/?mode=live . Stop: kill -TERM \$(cat logs/engine_live.pid)"
