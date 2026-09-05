#!/usr/bin/env bash
# Post-install checks for the mobile crash-analytics add-ons.
#
#   ./scripts/mobile-smoke-test.sh [https://sentry.example.com]
#
# Verifies the compose wiring, the Symbol Portal, the symbol server, the
# Symbolicator/Sentry config lines and (when INGEST_CANARY_DSN is set) one canary
# round trip. Exit code is the number of failed checks.
set -uo pipefail

PUBLIC_URL="${1:-}"
dc="docker compose"
fail=0
pass() { echo "  ok    $1"; }
warn() { echo "  warn  $1"; }
bad() {
  echo "  FAIL  $1"
  fail=$((fail + 1))
}

echo "== compose wiring"
if $dc config --services 2>/dev/null | grep -qx symbol-portal; then
  pass "symbol-portal is part of the compose project (COMPOSE_FILE in .env)"
else
  bad "symbol-portal missing from 'docker compose config --services'; check COMPOSE_FILE in .env"
fi
if $dc config --services 2>/dev/null | grep -qx symbol-server; then
  pass "symbol-server is part of the compose project"
else
  bad "symbol-server missing from compose project"
fi

echo "== symbol portal"
if $dc exec -T symbol-portal python -c "import urllib.request,sys,json; d=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=5)); sys.exit(0 if d.get('configured') else 2)" 2>/dev/null; then
  pass "portal healthy and token configured"
else
  rc=$?
  if [ "$rc" -eq 2 ]; then
    bad "portal running but SYMBOL_PORTAL_SENTRY_TOKEN is empty"
  else
    bad "portal container not reachable (docker compose ps symbol-portal)"
  fi
fi
if $dc exec -T symbol-portal python -c "import urllib.request,json,sys; r=urllib.request.urlopen('http://127.0.0.1:8080/api/projects', timeout=20); sys.exit(0 if r.status==200 else 1)" 2>/dev/null; then
  pass "portal can list projects through the Sentry API (token valid)"
else
  bad "portal cannot reach Sentry API or token lacks org:read/project:read"
fi
if $dc exec -T nginx wget -qO- http://symbol-portal:8080/api/health >/dev/null 2>&1; then
  pass "nginx can reach the portal"
else
  bad "nginx cannot reach symbol-portal:8080"
fi
if [ -n "$PUBLIC_URL" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' "${PUBLIC_URL%/}/symbols/api/health")
  if [ "$code" = "200" ] || [ "$code" = "401" ]; then
    pass "portal reachable at ${PUBLIC_URL%/}/symbols/ (HTTP $code)"
  else
    bad "portal not reachable at ${PUBLIC_URL%/}/symbols/ (HTTP $code); is nginx.conf updated?"
  fi
fi

echo "== symbol server"
if $dc exec -T symbolicator wget -qO- http://symbol-server/health >/dev/null 2>&1; then
  pass "symbolicator can reach symbol-server"
else
  bad "symbolicator cannot reach symbol-server (is it running?)"
fi
count=$(find symbol-server/data -mindepth 2 -type f 2>/dev/null | wc -l | tr -d ' ')
if [ "${count:-0}" -gt 0 ]; then
  pass "symbol-server/data holds $count sorted symbol files"
else
  warn "symbol-server/data is empty; run symbol-server/import-ios-symbols.sh on a Mac"
fi
if grep -q '^connect_to_reserved_ips: *true' symbolicator/config.yml 2>/dev/null; then
  pass "symbolicator/config.yml allows reserved IPs"
else
  bad "symbolicator/config.yml lacks 'connect_to_reserved_ips: true' (copy from config.example.yml)"
fi
if grep -q 'internal-system-symbols' sentry/sentry.conf.py 2>/dev/null; then
  pass "sentry.conf.py registers the internal-system-symbols source"
else
  bad "sentry/sentry.conf.py lacks the SENTRY_BUILTIN_SOURCES block (copy from sentry.conf.example.py)"
fi

echo "== ingest canary"
dsn=$(grep -E '^INGEST_CANARY_DSN=.+' .env .env.custom 2>/dev/null | head -1 | cut -d= -f2-)
if [ -z "$dsn" ]; then
  warn "INGEST_CANARY_DSN not set; skipping the round trip (see ingest-canary/README.md)"
else
  if COMPOSE_PROFILES="${COMPOSE_PROFILES:-feature-complete},ingest-canary" $dc run --rm -T ingest-canary python canary.py --once; then
    pass "canary event delivered end to end"
  else
    bad "canary event did not arrive; read the JSON result above for the stage it reached"
  fi
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "all checks passed"
else
  echo "$fail check(s) failed"
fi
exit "$fail"
