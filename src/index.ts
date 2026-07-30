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

/**
 * Where 2025-era traffic goes. One fixed instance, deliberately.
 *
 * A legacy client opens with `initialize` and gets an `Mcp-Session-Id` held in
 * ONE container's memory, then sends it on every later request. Fan that out
 * at random and ~(1 - 1/N) of its calls hit an instance that never saw the
 * session: `-32600 Session not found`, and the client reports the server as
 * having no tools.
 *
 * It cannot be fixed by hashing the session id either: `initialize` arrives
 * with no session at all, so the instance that mints the session is chosen
 * before there is anything to hash. Pinning the whole era to one instance is
 * the honest fix — legacy sessions were never load-balanceable without shared
 * session storage, which is exactly what 2026-07-28 set out to remove.
 */
const LEGACY_INSTANCE = "instance-legacy";

/**
 * Legacy clients send their own version here (`2025-11-25`), or omit the header
 * entirely (pre-2025-06-18). Only a modern client announces `2026-07-28`.
 * Header-only, so the request body is never consumed.
 */
function isModern(request: Request): boolean {
  return request.headers.get("mcp-protocol-version") === "2026-07-28";
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // Modern requests carry no session, so any instance can serve any of them —
    // that is the whole point, and getRandom exercises it. Legacy traffic is
    // pinned so its sessions survive. Both eras, one endpoint.
    const instance = isModern(request)
      ? await getRandom(env.MCP_CONTAINER, INSTANCES)
      : env.MCP_CONTAINER.getByName(LEGACY_INSTANCE);

    return instance.fetch(withAccessIdentity(request));
  },
} satisfies ExportedHandler<Env>;
