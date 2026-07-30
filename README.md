# fastmcp4-cf

A **Python** MCP server on **Cloudflare** — [FastMCP 4](https://gofastmcp.com)
speaking the [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
stateless protocol, on Cloudflare Containers behind a Worker.

Clone it, add tools, deploy.

## Why this exists

MCP 2026-07-28 is the largest revision since the protocol launched. It **removes
protocol-level sessions**: no `initialize` handshake, no `Mcp-Session-Id`. Every
request carries its own protocol version and client identity in a `_meta`
envelope, so any instance can answer any request — which is what makes
round-robin load balancing and scale-to-zero possible at all.

This is a working place to watch that happen:

- **`whoami`** reports which container answered, and who the server thinks you
  are. Call it repeatedly — the instance changes mid-conversation, nothing pinned.
- **`delete_notes`** is a guard tool: the multi-round-trip replacement for
  `ctx.elicit()`, which now raises on modern connections. It also demonstrates
  per-caller authorization.
- **`probe.sh`** speaks the raw protocol over curl, so you can watch a tool call
  succeed with no handshake — impossible under 2025-11-25.
- **CI** boots the real container behind the real Worker and asserts all of it.

> **Tracks a beta.** `fastmcp==4.0.0b1` is the newest FastMCP 4 (no RC yet;
> stable is still 3.4.x) and pulls `pydantic 2.14.0a1`. Pin your versions.

## Why Containers, not Workers

Workers run JS/TS on V8 isolates; Python there goes through Pyodide, which takes
only pure-Python and PyEmscripten wheels — and FastMCP's tree includes
pydantic-core, which is Rust. So Python MCP on Cloudflare means Containers,
which run an ordinary `linux/amd64` image with FastMCP unmodified.

The tradeoff: containers are **regional**, started on demand in the nearest
region — not per-PoP like Workers. Closer to Cloud Run than to edge. If you want
true edge, you want TypeScript and Cloudflare's
[Agents SDK](https://developers.cloudflare.com/changelog/post/2026-07-27-agents-sdk-v0.20.0-mcp-sdk-v2/),
not this.

## Shape

```
request → src/index.ts (Worker)  →  Dockerfile → server.py (FastMCP 4)
             routes + passes secrets in
```

Containers are not standalone: a Worker receives every request and picks which
instance handles it.

| file | role |
| --- | --- |
| `server.py` | the MCP server — put your tools here |
| `Dockerfile` | `linux/amd64` image, required by Containers |
| `src/index.ts` | the Worker: routing, secrets, protocol-era handling |
| `wrangler.jsonc` | container, Durable Object binding, migration |
| `probe.sh` | smoke tool — raw 2026-07-28 over curl |
| [`AUTH.md`](AUTH.md) | **read before exposing this** — authn/authz |
| [`NOTES.md`](NOTES.md) | gotchas that cost real time to find |

## Prerequisites

- **Docker running** — wrangler builds the image locally
- **Workers Paid plan** — Containers are not on the free tier
- Node 20+

## Run locally

No Cloudflare account needed — the container runs in local Docker.

```bash
npm install
cp .dev.vars.example .dev.vars
python -c "import secrets; print(secrets.token_hex(32))"   # paste into .dev.vars
npm run dev                                                # ready on :8787

./probe.sh http://localhost:8787 server/discover
./probe.sh http://localhost:8787 tools/call whoami
```

`server/discover` replaced the `initialize` handshake — it returns
`supportedVersions`, `resultType`, `instructions`, and cache hints. Call
`whoami` a few times and watch `instance` change: several containers answering
one client, no session between them.

### The server asking a question back

`delete_notes` returns a question instead of blocking on one:

```bash
# Round 1 — the server asks. Note resultType: "input_required".
./probe.sh http://localhost:8787 tools/call delete_notes '{"folder":"notes"}'

# Round 2 — retry the SAME call with the answer and the state.
STATE=$(./probe.sh http://localhost:8787 tools/call delete_notes '{"folder":"notes"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['requestState'])")

./probe.sh http://localhost:8787 tools/call delete_notes '{"folder":"notes"}' \
  '{"confirm":{"action":"accept","content":{"value":true}}}' "$STATE"
```

The tool body runs from the top on **both** rounds — locals don't survive.
`ctx.input_responses` is `None` the first time, populated the second; anything
else that must carry over goes in `request_state`.

## Deploy

```bash
npx wrangler login
npx wrangler secret put REQUEST_STATE_KEY    # 32+ bytes of randomness
npm run deploy
```

First provisioning takes a few minutes. Then:

```bash
./probe.sh https://fastmcp4-cf.<your-subdomain>.workers.dev tools/call whoami
```

`REQUEST_STATE_KEY` seals the multi-round-trip `request_state`. **Every instance
needs the same value** — see [NOTES.md](NOTES.md) for why the default breaks
under a load balancer. The server refuses to boot without it.

## Adding your own tools

```python
@mcp.tool
def my_tool(arg: str) -> str:
    """Docstring becomes the tool description the model reads."""
    return f"got {arg}"
```

Two constants are duplicated by necessity — keep them in sync:

- port `8080`: `Dockerfile` (`EXPOSE`), `src/index.ts` (`defaultPort`), `server.py`
- instance count: `src/index.ts` (`INSTANCES`) and `wrangler.jsonc`
  (`max_instances`, which must also cover the pinned legacy instance)

## Authentication

**Off by default** — as cloned, anyone with the URL can call every tool, and the
server says so on boot.

**Cloudflare Access** is wired up and needs no code: Access runs the OAuth flow
at the edge and forwards a signed JWT, which `server.py` verifies.

```bash
npx wrangler secret put ACCESS_TEAM_DOMAIN   # yourteam.cloudflareaccess.com
npx wrangler secret put ACCESS_AUD           # the application's AUD tag
npx wrangler secret put ADMIN_EMAILS         # who may call gated tools
npm run deploy
```

Verified end to end on a plain `workers.dev` hostname — no custom domain needed.
Two dashboard settings will block you if missed; both are in
**[AUTH.md](AUTH.md)**, along with per-caller tool visibility, service tokens
for CI, and the rules worth treating as non-negotiable.

Any other provider — Google, Auth0, WorkOS, Keycloak — swaps into `build_auth()`.

## Teardown

`wrangler delete` removes the Worker but leaves the container application and
the pushed image, which keeps costing storage:

```bash
npx wrangler delete
npx wrangler containers list && npx wrangler containers delete <ID>
npx wrangler containers images list && npx wrangler containers images delete <image>
```

Delete the Access application separately in the Zero Trust dashboard.

## probe.sh

A smoke tool, not a client library. It sends the required headers and `_meta`
envelope and supports multi-round-trip retries. Its `requestState` escaping
handles quotes only. For real client work use the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector).

## License

MIT — see [LICENSE](LICENSE).
