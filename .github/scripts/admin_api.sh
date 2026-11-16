#!/usr/bin/env bash
# Call an admin endpoint of the monitor service and explain any failure in one line,
# instead of handing an error response to jq.
#
# Usage:
#   admin_api.sh <path with query string> <timeout seconds> <output file> [<jq check> <expected fields>]
#
# Returns 0 only for HTTP 2xx with a JSON object body that passes the optional jq check;
# the body is then written to the output file. Otherwise prints one ::error:: annotation
# (also added to the run summary) and returns 1.
#
# Why this exists: without a status check, a stopped service (Railway answers 404
# {"message": "Application not found"}) and a failing check (HTTP 500 {"detail": ...})
# both surfaced only as "jq: Cannot iterate over null".
#
# Messages never include the service URL, the token or recipients: logs of a public
# repository are public.
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "usage: $0 <path> <timeout seconds> <output file> [<jq check> <expected fields>]" >&2
  exit 2
fi

path="$1"
max_time="$2"
out="$3"
check="${4:-}"
expect="${5:-}"

: "${BASE_URL:?BASE_URL is not set}"
: "${ADMIN_TOKEN:?ADMIN_TOKEN is not set}"

url="${BASE_URL%/}${path}"
body="$(mktemp)"
errlog="$(mktemp)"
trap 'rm -f "$body" "$errlog"' EXIT

# Workflow commands need % and newlines escaped; properties (title) also : and ,
escape_msg() { printf '%s' "$1" | tr '\r\n' '  ' | sed 's/%/%25/g'; }
escape_prop() { escape_msg "$1" | sed 's/:/%3A/g; s/,/%2C/g'; }

fail() {
  local title="$1" msg="$2"
  echo "::error title=$(escape_prop "$title")::$(escape_msg "$msg")"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    printf '### %s\n\n%s\n\n' "$title" "$msg" >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 1
}

rc=0
code="$(curl -sS -X POST \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  -H "Accept: application/json" \
  --max-time "$max_time" \
  -o "$body" -w '%{http_code}' \
  "$url" 2>"$errlog")" || rc=$?

if [ "$rc" -eq 28 ]; then
  fail "Request timed out" "${path} did not answer within ${max_time} s. The check may be stuck fetching BIS pages; see the service logs."
elif [ "$rc" -ne 0 ]; then
  case "$rc" in
    6) reason="Could not resolve the host" ;;
    7) reason="Could not connect" ;;
    35|60) reason="TLS handshake or certificate error" ;;
    *) reason="curl exit code ${rc}" ;;
  esac
  fail "Cannot reach the service" "${reason}. Check that the BIS_MONITOR_URL variable points to a running deployment."
fi

# FastAPI puts errors in .detail; Railway's edge puts them in .message
is_object=false
msg=""
edge_msg=""
if jq -e 'type == "object"' "$body" >/dev/null 2>&1; then
  is_object=true
  msg="$(jq -r '(.detail // .message // "") | if type == "string" then . else tojson end | .[0:400]' "$body")"
  edge_msg="$(jq -r 'if has("detail") then "" else (.message // "") end | tostring' "$body")"
fi
snippet="$(tr '\r\n' '  ' < "$body")"
snippet="${snippet:0:300}"

case "$code" in
  2??)
    $is_object || fail "Unexpected response" "${path} returned HTTP ${code}, but the body is not a JSON object: ${snippet}"
    if [ -n "$check" ] && ! jq -e "$check" "$body" >/dev/null 2>&1; then
      fail "Unexpected response" "${path} returned HTTP ${code}, but the body does not match (expected: ${expect}). Fields received: $(jq -c 'keys' "$body"). The service API may have changed."
    fi
    cp "$body" "$out"
    ;;
  401)
    fail "Invalid admin token" "The service rejected X-Admin-Token (HTTP 401). The BIS_ADMIN_TOKEN secret must equal the ADMIN_TOKEN of the service."
    ;;
  404)
    if [ "$edge_msg" = "Application not found" ]; then
      fail "Service not running" "Railway answered 404 Application not found: no deployment serves BIS_MONITOR_URL (the service was stopped or removed, or its domain changed). If it was stopped on purpose, disable this workflow."
    fi
    fail "Request failed" "${path} returned HTTP 404: ${msg:-$snippet}"
    ;;
  409)
    fail "Check already running" "The service is busy (HTTP 409): ${msg}. Run the workflow again in a few minutes."
    ;;
  500)
    fail "Check failed on the service" "${path} returned HTTP 500: ${msg:-$snippet}. The check itself failed (for example, BIS changed its website); the service logs have the traceback."
    ;;
  502|503|504)
    if [ -n "$edge_msg" ]; then
      fail "Service not responding" "Railway answered HTTP ${code} (${edge_msg}): the deployment exists, but the app is not up or has crashed. See the deployment logs."
    fi
    fail "Service unavailable" "${path} returned HTTP ${code}: ${msg:-$snippet}"
    ;;
  *)
    fail "Request failed" "${path} returned HTTP ${code}: ${msg:-$snippet}"
    ;;
esac
