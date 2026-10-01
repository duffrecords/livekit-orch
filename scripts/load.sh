#!/usr/bin/env bash
# Scaling demo against the running cluster: starts COUNT sessions at once,
# shows them being picked up while the autoscaler adds worker pods, stops them
# after HOLD seconds, and shows the pods going away again. Needs curl, jq and
# kubectl.
#
# Sessions here have no user, so LiveKit closes them after the room's empty
# timeout (120 s); keep READY_TIMEOUT + HOLD below that.
set -euo pipefail

API="${API:-http://localhost:8080}"
COUNT="${COUNT:-12}"
AGENT="${AGENT:-echo}"
HOLD="${HOLD:-30}"
READY_TIMEOUT="${READY_TIMEOUT:-60}"
DOWN_TIMEOUT="${DOWN_TIMEOUT:-240}"
NAMESPACE="${NAMESPACE:-livekit}"

fail() { echo "FAIL: $*" >&2; exit 1; }

pods() {
  kubectl -n "$NAMESPACE" get pods -l "app=agent-$AGENT" --no-headers 2>/dev/null \
    | awk '{n[$3]++} END {for (s in n) printf "%s%d %s", (i++ ? ", " : ""), n[s], s}'
}
running() {
  kubectl -n "$NAMESPACE" get pods -l "app=agent-$AGENT" --no-headers 2>/dev/null | grep -c Running || true
}
# status counts for our sessions, e.g. "7 ready, 5 pending (workers_full)"
statuses() {
  curl -fsS "$API/sessions" | jq -r --argjson ids "$ids_json" '
    [.sessions[] | select(.id as $i | $ids | index($i))
      | .status + (if .pending_reason then " (" + .pending_reason + ")" else "" end)]
    | group_by(.) | map("\(length) \(.[0])") | join(", ")'
}
ready_count() {
  curl -fsS "$API/sessions" | jq --argjson ids "$ids_json" \
    '[.sessions[] | select(.id as $i | $ids | index($i)) | select(.status == "ready")] | length'
}
line() { printf '  +%3ds  sessions: %-44s pods: %s\n' "$((SECONDS - t0))" "$1" "$(pods)"; }

[ "$(curl -s -o /dev/null -w '%{http_code}' "$API/readyz")" = 200 ] || fail "$API/readyz is not 200; is the cluster up?"

tmp=$(mktemp -d)
ids_json='[]'
cleanup() {
  for id in $(echo "$ids_json" | jq -r '.[]'); do
    curl -s -o /dev/null -X DELETE "$API/sessions/$id" || true
  done
  rm -rf "$tmp"
}
trap cleanup EXIT

before=$(running)
echo "worker pods before: $(pods)"
echo "starting $COUNT sessions"
t0=$SECONDS
for i in $(seq "$COUNT"); do
  curl -fsS -X POST "$API/sessions" -H 'Content-Type: application/json' \
    -d "{\"agent\": \"$AGENT\", \"identity\": \"load-$i\"}" > "$tmp/$i.json" &
done
wait
ids_json=$(jq -s '[.[].id]' "$tmp"/*.json)

while :; do
  line "$(statuses)"
  [ "$(ready_count)" = "$COUNT" ] && break
  [ $((SECONDS - t0)) -ge "$READY_TIMEOUT" ] && fail "not every session had its agent within ${READY_TIMEOUT}s"
  sleep 3
done
echo "all $COUNT sessions ready after $((SECONDS - t0))s"

echo "holding for ${HOLD}s"
hold_until=$((SECONDS + HOLD))
while [ "$SECONDS" -lt "$hold_until" ]; do sleep 5; line "$(statuses)"; done
peak=$(running)
echo "jobs per worker:"
curl -fsS "$API/workers" | jq -r '.workers[] | "  \(.address)  jobs=\(.jobs)\(if .full then "  FULL" else "" end)"'

echo "stopping sessions"
for id in $(echo "$ids_json" | jq -r '.[]'); do
  curl -s -o /dev/null -X DELETE "$API/sessions/$id"
done
ids_json='[]'

echo "waiting for the workers to scale back down"
t0=$SECONDS
while :; do
  n=$(kubectl -n "$NAMESPACE" get pods -l "app=agent-$AGENT" --no-headers | wc -l | tr -d ' ')
  printf '  +%3ds  pods: %s\n' "$((SECONDS - t0))" "$(pods)"
  [ "$n" -le "$before" ] && break
  [ $((SECONDS - t0)) -ge "$DOWN_TIMEOUT" ] && fail "still $n worker pods after ${DOWN_TIMEOUT}s"
  sleep 10
done

echo "PASS: $COUNT sessions ready; workers went $before -> $peak -> $n"
