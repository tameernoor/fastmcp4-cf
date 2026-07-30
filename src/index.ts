/**
 * The Worker that fronts the Python container.
 *
 * Cloudflare Containers are not standalone: a Worker receives every request
 * and decides which container instance handles it. `getRandom` picks one of
 * INSTANCES at random per request — a load balancer with no affinity, which
 * only works because the 2026-07-28 protocol has no session to be pinned to.
 * Under the old protocol this would break immediately: the client's
 * `initialize` would land on one instance and its next call on another.
 */

import { Container, getRandom } from "@cloudflare/containers";

interface Env {
  MCP_CONTAINER: DurableObjectNamespace<FastMCPContainer>;
  /**
   * Seals the multi-round-trip `request_state`. EVERY instance must see the
   * same value or guard tools break when a retry lands on a different one.
   *   local:    .dev.vars
   *   deployed: npx wrangler secret put REQUEST_STATE_KEY
   */
  REQUEST_STATE_KEY: string;
  /**
   * Cloudflare Access identity, when the Worker is behind an Access
   * application with Managed OAuth enabled. Both unset = the server runs open.
   *   ACCESS_TEAM_DOMAIN  e.g. myteam.cloudflareaccess.com
   *   ACCESS_AUD          the Access application's AUD tag
   */
  ACCESS_TEAM_DOMAIN?: string;
  ACCESS_AUD?: string;
  /** Comma-separated emails allowed to call admin-gated tools. */
  ADMIN_EMAILS?: string;
}

/**
 * Access authenticates at the edge, then forwards a signed assertion to the
 * origin as `Cf-Access-Jwt-Assertion`. FastMCP — like every MCP server — reads
 * bearer tokens from `Authorization`. Bridge the two so the Python side needs
 * no Cloudflare-specific code, just a JWT verifier.
 *
 * Only fills `Authorization` when absent, so a request that already carries a
 * bearer token is left alone.
 */
function withAccessIdentity(request: Request): Request {
  const assertion = request.headers.get("cf-access-jwt-assertion");
  if (!assertion || request.headers.get("authorization")) return request;

  const headers = new Headers(request.headers);
  headers.set("authorization", `Bearer ${assertion}`);
  return new Request(request, { headers });
}

export class FastMCPContainer extends Container<Env> {
  /** Must match EXPOSE / the port server.py listens on. */
  defaultPort = 8080;
  /** Scale to zero once idle — the container stops, and cold-starts on demand. */
  sleepAfter = "10m";
  /** Secrets and config reach the container only by being passed in explicitly. */
  envVars = {
    REQUEST_STATE_KEY: this.env.REQUEST_STATE_KEY,
    ACCESS_TEAM_DOMAIN: this.env.ACCESS_TEAM_DOMAIN ?? "",
    ACCESS_AUD: this.env.ACCESS_AUD ?? "",
    ADMIN_EMAILS: this.env.ADMIN_EMAILS ?? "",
  };
}

/** Fan out across this many instances so `whoami` visibly changes. */
const INSTANCES = 3;

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // No affinity, for either protocol era. 2026-07-28 is sessionless by
    // design, and server.py runs with `stateless_http=True` so 2025-era
    // requests are served without a session too — meaning every request here,
    // old or new, can go to any instance.
    //
    // Without that flag this breaks: a legacy client's `initialize` mints an
    // `Mcp-Session-Id` in one container's memory and its next request lands
    // somewhere else. See NOTES.md.
    const instance = await getRandom(env.MCP_CONTAINER, INSTANCES);
    return instance.fetch(withAccessIdentity(request));
  },
} satisfies ExportedHandler<Env>;
