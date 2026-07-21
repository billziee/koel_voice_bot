#!/usr/bin/env bash
# Trigger a Render deploy via the REST API and (optionally) wait for it to
# finish. Requires RENDER_API_KEY in the environment — never hardcode it here
# or anywhere else in this repo; export it in your shell before running:
#
#   export RENDER_API_KEY="rnd_xxx"        # from Render dashboard > Account Settings > API Keys
#   ./scripts/deploy_render.sh
#
# Usage:
#   ./scripts/deploy_render.sh [service_id] [--no-wait]
#
#   service_id   Defaults to $RENDER_SERVICE_ID or the koel-backend service id
#                below. Pass explicitly to target a different service (e.g.
#                koel-dashboard) without editing this file.
#   --no-wait    Trigger the deploy and exit immediately instead of polling
#                for completion.

set -euo pipefail

# koel-backend — the live combined backend (render.yaml). Override via
# RENDER_SERVICE_ID or a positional arg without editing this default.
DEFAULT_SERVICE_ID="srv-d9354urtqb8s73aa9rr0"
API_BASE="https://api.render.com/v1"
POLL_INTERVAL_S=10
POLL_TIMEOUT_S=600 # 10 minutes — generous for a free/starter-tier cold build

SERVICE_ID="${RENDER_SERVICE_ID:-$DEFAULT_SERVICE_ID}"
WAIT=1

for arg in "$@"; do
  case "$arg" in
    --no-wait) WAIT=0 ;;
    srv-*) SERVICE_ID="$arg" ;;
    *)
      echo "Unrecognised argument: $arg" >&2
      echo "Usage: $0 [service_id] [--no-wait]" >&2
      exit 2
      ;;
  esac
done

if [[ -z "${RENDER_API_KEY:-}" ]]; then
  echo "RENDER_API_KEY is not set. Export it first:" >&2
  echo "  export RENDER_API_KEY=\"rnd_xxx\"" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (used to parse the JSON response)." >&2
  exit 1
fi

_api() {
  # $1=method $2=path $3=body(optional). Never logs the Authorization header —
  # curl's default (non-verbose) output only ever includes the response body.
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" "${API_BASE}${path}" \
      -H "Authorization: Bearer ${RENDER_API_KEY}" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json" \
      -d "$body"
  else
    curl -sS -X "$method" "${API_BASE}${path}" \
      -H "Authorization: Bearer ${RENDER_API_KEY}" \
      -H "Accept: application/json"
  fi
}

echo "Triggering deploy for service ${SERVICE_ID}..."
deploy_json=$(_api POST "/services/${SERVICE_ID}/deploys" "{}")
deploy_id=$(echo "$deploy_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))")

if [[ -z "$deploy_id" ]]; then
  echo "Failed to start deploy — API response:" >&2
  echo "$deploy_json" >&2
  exit 1
fi

echo "Deploy started: ${deploy_id}"
echo "https://dashboard.render.com/web/${SERVICE_ID}/deploys/${deploy_id}"

if [[ "$WAIT" -eq 0 ]]; then
  echo "--no-wait set; not polling for completion."
  exit 0
fi

echo "Waiting for it to finish (up to ${POLL_TIMEOUT_S}s)..."
elapsed=0
while [[ "$elapsed" -lt "$POLL_TIMEOUT_S" ]]; do
  status=$(_api GET "/services/${SERVICE_ID}/deploys/${deploy_id}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))")
  case "$status" in
    live)
      echo "Deploy ${deploy_id} is live."
      exit 0
      ;;
    build_failed|update_failed|canceled|deactivated)
      echo "Deploy ${deploy_id} ended with status: ${status}" >&2
      exit 1
      ;;
    *)
      echo "  status=${status} (${elapsed}s elapsed)"
      sleep "$POLL_INTERVAL_S"
      elapsed=$((elapsed + POLL_INTERVAL_S))
      ;;
  esac
done

echo "Timed out after ${POLL_TIMEOUT_S}s waiting for deploy ${deploy_id}." >&2
exit 1
