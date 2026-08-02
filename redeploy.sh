#!/usr/bin/env bash
# Bring the deployment back after a teardown, without redoing any dashboard work.
#
# Reads every value from .deploy.vars (gitignored), pushes them as Worker secrets,
# and deploys. Safe to re-run — `wrangler secret put` overwrites.
#
# This only recreates the CHEAP half: Worker, container, images, secrets.
# The expensive half — the Cloudflare Access application, its policies, Managed
# OAuth, the redirect-URI allowlist, and any service tokens — lives in Zero
# Trust and is untouched by teardown. Leave it alone and it reattaches by
# hostname, provided the Worker keeps the same name.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .deploy.vars ] || { echo "no .deploy.vars — copy .deploy.vars.example and fill it in"; exit 1; }
set -a
# .deploy.vars is gitignored, so it is absent wherever this gets linted.
# shellcheck source=/dev/null
. ./.deploy.vars
set +a

: "${REQUEST_STATE_KEY:?missing from .deploy.vars}"

echo "==> pushing secrets"
printf '%s' "$REQUEST_STATE_KEY" | npx wrangler secret put REQUEST_STATE_KEY

if [ -n "${ACCESS_TEAM_DOMAIN:-}" ] && [ -n "${ACCESS_AUD:-}" ]; then
  printf '%s' "$ACCESS_TEAM_DOMAIN" | npx wrangler secret put ACCESS_TEAM_DOMAIN
  printf '%s' "$ACCESS_AUD"         | npx wrangler secret put ACCESS_AUD
  [ -n "${ADMIN_EMAILS:-}" ] && printf '%s' "$ADMIN_EMAILS" | npx wrangler secret put ADMIN_EMAILS
elif [ "${ALLOW_OPEN_MODE:-}" = "1" ]; then
  # The container refuses to boot unauthenticated unless this reaches it, so a
  # deliberate open deployment has to push it as a secret like anything else.
  printf '%s' "1" | npx wrangler secret put ALLOW_OPEN_MODE
  echo "    (ALLOW_OPEN_MODE=1 — deploying WITHOUT authentication, and every"
  echo "     caller is an admin. /host and delete_notes will be public.)"
else
  cat >&2 <<'EOF'
Refusing to deploy: no ACCESS_TEAM_DOMAIN / ACCESS_AUD in .deploy.vars, and
ALLOW_OPEN_MODE is not 1.

Deployed without auth, anyone with the URL gets every tool and a browser
console at /host with a destructive button on it. The container will refuse to
boot in that state anyway, so this stops you earlier.

  set ACCESS_TEAM_DOMAIN and ACCESS_AUD   (see AUTH.md), or
  set ALLOW_OPEN_MODE=1                   if you really mean it
EOF
  exit 1
fi

echo "==> deploying"
npm run deploy

cat <<'EOF'

Done. Two endpoints are live on the Worker:

  * /mcp    the MCP endpoint
  * /host   a browser host that drives the widget over 2026-07-28. It is a
            plain route, so Access covers it only if Access covers the Worker.
            Left open, it is a working console with a destructive button.

Two things to expect:

  * first provisioning takes a few minutes before the container answers
  * running containers serve the previous image for several minutes after a
    deploy — see NOTES.md, "A deploy is not a cutover". The host page is
    bundled into the Worker, so it updates immediately.

If Access was left in place, the application reattaches by hostname and no
dashboard work is needed.
EOF
