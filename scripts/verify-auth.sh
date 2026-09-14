#!/usr/bin/env bash
# Verify the API's auth posture: EVERY path requires credentials.
#
# Upstream wires Basic auth only into the /api/v1 command router, so the
# Workspace metadata routes and FastAPI's own docs are served to anyone
# unless something covers them. api_app.py's factory appends a blanket
# Basic auth middleware; this script is the check that it still does its job
# against a RUNNING stack. tests/test_api_app_auth.py checks the same
# property in-process, including the CORS preflight case this cannot see.
#
#   OPENBB_USER=openbb OPENBB_PASS=<password> \
#     OPENBB_URL=https://openbb.<your-tailnet>.ts.net scripts/verify-auth.sh
#
# or from inside the container's own namespace:
#
#   docker exec -i openbb-api bash -c \
#     'OPENBB_URL=http://127.0.0.1:6900 OPENBB_USER=<u> OPENBB_PASS=<p> bash -s' \
#     < scripts/verify-auth.sh
set -euo pipefail
: "${OPENBB_URL:?set OPENBB_URL to the base URL to test}"
: "${OPENBB_USER:?set OPENBB_USER}"
: "${OPENBB_PASS:?set OPENBB_PASS}"

# Every root path the stack serves outside /api/v1. The command router is
# checked separately: a correct request there returns 422 (auth passed, empty
# query rejected), not 200.
PATHS=(
  /
  /widgets.json
  /apps.json
  /agents.json
  /openapi.json
  /docs
  /redoc
  /docs/oauth2-redirect
)

fail=0

# check <label> <expected-code> [curl args...]
check() {
  local label="$1" expected="$2"
  shift 2
  local got
  got=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@" || echo 000)
  if [ "$got" = "$expected" ]; then
    printf '   OK   %-24s %s\n' "$label" "$got"
  else
    printf '   FAIL %-24s expected %s, got %s\n' "$label" "$expected" "$got" >&2
    fail=1
  fi
}

echo "1/5 no credentials -> 401 on every root path..."
for p in "${PATHS[@]}"; do
  check "$p" 401 "$OPENBB_URL$p"
done

# NOTE: if your real username is literally "wrong", this pass is meaningless.
echo "2/5 wrong credentials -> 401 on every root path..."
for p in "${PATHS[@]}"; do
  check "$p" 401 -u "wrong:wrong" "$OPENBB_URL$p"
done

echo "3/5 right credentials -> 200 on every root path..."
for p in "${PATHS[@]}"; do
  check "$p" 200 -u "$OPENBB_USER:$OPENBB_PASS" "$OPENBB_URL$p"
done

echo "4/5 the command router (422 = auth passed, empty query rejected)..."
q="$OPENBB_URL/api/v1/equity/price/quote"
check "quote no-creds"    401 "$q"
check "quote wrong-creds" 401 -u "wrong:wrong" "$q"
check "quote right-creds" 422 -u "$OPENBB_USER:$OPENBB_PASS" "$q"

echo "5/5 CORS preflight is answered, not blocked by auth..."
# Browsers never send credentials on a preflight. If the auth middleware
# outranks CORSMiddleware this returns 401 with no CORS headers, and every
# cross-origin client (OpenBB Workspace) is locked out. curl -u never sends
# a preflight, so only an explicit OPTIONS probe catches it.
pre=$(curl -s -o /dev/null -D - --max-time 15 -X OPTIONS \
  -H "Origin: https://pro.openbb.co" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: authorization" \
  "$OPENBB_URL/widgets.json" || true)
pre_code=$(printf '%s' "$pre" | awk 'NR==1{print $2}')
if [ "$pre_code" = "200" ] && printf '%s' "$pre" | grep -qi '^access-control-allow-origin:'; then
  printf '   OK   %-24s %s + ACAO\n' "preflight" "$pre_code"
else
  printf '   FAIL %-24s expected 200 + Access-Control-Allow-Origin, got %s\n' "preflight" "${pre_code:-000}" >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "Auth posture FAILED — something is served unauthenticated." >&2
  exit 1
fi
echo "Auth posture verified: nothing is served without credentials."
