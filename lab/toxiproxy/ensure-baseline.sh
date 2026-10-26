#!/bin/sh
# Reconcile loop: keep a baseline latency on the app -> PostgreSQL link.
# Toxiproxy keeps toxics in memory only, so after a restart the latency would silently
# disappear. Every few seconds this puts it back if it is missing.
# The lab database runs on the same host (~0.1 ms per round trip); real databases sit on
# another machine (1-3 ms). Without this, query-count bugs like N+1 are invisible here.
API="${TOXIPROXY_API:-http://toxiproxy:8474}"
LATENCY_MS="${BASELINE_LATENCY_MS:-2}"
JITTER_MS="${BASELINE_JITTER_MS:-1}"
BODY="{\"name\":\"baseline_latency\",\"type\":\"latency\",\"stream\":\"downstream\",\"attributes\":{\"latency\":${LATENCY_MS},\"jitter\":${JITTER_MS}}}"

while true; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$API/proxies/postgres/toxics/baseline_latency")
  if [ "$code" = "404" ]; then
    curl -s -o /dev/null -X POST -H 'Content-Type: application/json' -d "$BODY" \
      "$API/proxies/postgres/toxics" && echo "baseline latency ${LATENCY_MS}ms set"
  fi
  sleep 15
done
