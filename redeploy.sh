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
set -a; . ./.deploy.vars; set +a

: "${REQUEST_STATE_KEY:?missing from .deploy.vars}"

echo "==> pushing secrets"
printf '%s' "$REQUEST_STATE_KEY" | npx wrangler secret put REQUEST_STATE_KEY

# Auth is optional: without these the server boots wide open and says so.
if [ -n "${ACCESS_TEAM_DOMAIN:-}" ] && [ -n "${ACCESS_AUD:-}" ]; then
  printf '%s' "$ACCESS_TEAM_DOMAIN" | npx wrangler secret put ACCESS_TEAM_DOMAIN
  printf '%s' "$ACCESS_AUD"         | npx wrangler secret put ACCESS_AUD
  [ -n "${ADMIN_EMAILS:-}" ] && printf '%s' "$ADMIN_EMAILS" | npx wrangler secret put ADMIN_EMAILS
else
  echo "    (no ACCESS_* set — deploying WITHOUT authentication)"
fi

echo "==> deploying"
npm run deploy

cat <<'EOF'

Done. Two things to expect:

  * first provisioning takes a few minutes before the container answers
  * running containers serve the previous image for several minutes after a
    deploy — see NOTES.md, "A deploy is not a cutover"

If Access was left in place, the application reattaches by hostname and no
dashboard work is needed.
EOF
