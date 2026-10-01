#!/usr/bin/env bash
# End-to-end check of the orchestrator API against the running cluster:
# starts COUNT sessions in parallel, waits for each agent to join, then stops
# them and checks that nothing is left behind. Needs curl and jq.
set -euo pipefail

API="${API:-http://localhost:8080}"
COUNT="${COUNT:-10}"
AGENT="${AGENT:-echo}"
READY_TIMEOUT="${READY_TIMEOUT:-20}"

fail() { echo "FAIL: $*" >&2; exit 1; }

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

[ "$(code "$API/readyz")" = 200 ] || fail "$API/readyz is not 200; is the cluster up?"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "starting $COUNT sessions in parallel"
for i in $(seq "$COUNT"); do
  curl -fsS -X POST "$API/sessions" -H 'Content-Type: application/json' \
    -d "{\"agent\": \"$AGENT\", \"identity\": \"smoke-$i\"}" > "$tmp/$i.json" &
done
wait

ids=$(jq -r .id "$tmp"/*.json)
[ "$(echo "$ids" | sort -u | wc -l | tr -d ' ')" = "$COUNT" ] || fail "expected $COUNT distinct session IDs"
[ "$(jq -r 'select(.token and .url) | .id' "$tmp"/*.json | wc -l | tr -d ' ')" = "$COUNT" ] \
  || fail "a session came back without a token or URL"

echo "waiting for every agent to join"
deadline=$((SECONDS + READY_TIMEOUT))
while :; do
  statuses=$(curl -fsS "$API/sessions" | jq -c '[.sessions[] | {id, status}]')
  ready=0
  for id in $ids; do
    s=$(echo "$statuses" | jq -r --arg id "$id" '.[] | select(.id == $id) | .status')
    [ "$s" = ready ] && ready=$((ready + 1))
  done
  [ "$ready" = "$COUNT" ] && break
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "$statuses" | jq . >&2
    fail "only $ready of $COUNT sessions became ready within ${READY_TIMEOUT}s"
  fi
  sleep 1
done
echo "all $COUNT sessions ready"

echo "stopping sessions"
for id in $ids; do
  [ "$(code -X DELETE "$API/sessions/$id")" = 204 ] || fail "DELETE $id was not 204"
done

first=$(echo "$ids" | head -1)
[ "$(curl -fsS "$API/sessions/$first" | jq -r '.status + "/" + .end_reason')" = "ended/stopped" ] \
  || fail "stopped session does not report ended/stopped"
[ "$(code -X DELETE "$API/sessions/$first")" = 204 ] || fail "second DELETE was not 204"
[ "$(code "$API/sessions/s-0000000000")" = 404 ] || fail "unknown session was not 404"
[ "$(code -X POST "$API/sessions" -H 'Content-Type: application/json' -d '{"agent": "no-such-agent"}')" = 422 ] \
  || fail "unknown agent was not 422"

left=$(curl -fsS "$API/sessions" | jq -r '[.sessions[].id] | join(" ")')
for id in $ids; do
  case " $left " in *" $id "*) fail "session $id is still live after DELETE" ;; esac
done

echo "PASS: $COUNT sessions started, became ready, and were cleaned up"
