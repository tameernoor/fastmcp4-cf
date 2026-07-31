# fastmcp4-cf

A **Python** MCP server on **Cloudflare** — [FastMCP 4](https://gofastmcp.com)
speaking the [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
stateless protocol, on Cloudflare Containers behind a Worker.

Clone it, add tools, deploy.

## What it exercises

| feature | here |
| --- | --- |
| No sessions, no `initialize` | any instance answers any request |
| `server/discover` | replaces the handshake, cacheable |
| `resultType` on every result | `complete` / `input_required` |
| MRTR with sealed `requestState` | `delete_notes` |
| `ttlMs` + `cacheScope` hints | every list result |
| Deterministic `tools/list` order | prompt-cache friendly |
| Extension negotiation | `capabilities.extensions` |
| MCP Apps (`io.modelcontextprotocol/ui`) | `monitor` and its `ui://` resource |
| Declared sandbox CSP | enforced by `host/` on the widget's iframe |
| A host that speaks it | `host/`, served at `/host` |
| Load balancing, scale-to-zero | Worker `getRandom`, `sleepAfter` |
| Edge OAuth + per-caller authz | Cloudflare Access, admin-gated tool |

## Why this exists

MCP 2026-07-28 is the largest revision since the protocol launched. It **removes
protocol-level sessions**: no `initialize` handshake, no `Mcp-Session-Id`. Every
request carries its own protocol version and client identity in a `_meta`
envelope, so any instance can answer any request — which is what makes
round-robin load balancing and scale-to-zero possible at all.

The core also got *smaller*. Server-initiated requests are gone, replaced by
MRTR. Roots, sampling, logging and dynamic client registration are **deprecated**
rather than removed — still functional for a twelve-month window, but not for new
code. Tasks moved out into a separately-versioned **extension** you negotiate,
which is now the pattern for anything beyond the core.

This is a working place to watch that happen:

- **`whoami`** reports which container answered, and who the server thinks you
  are. Call it repeatedly — the instance changes mid-conversation, nothing pinned.
- **`monitor`** is an **MCP App**: a tool bound to a `ui://` resource that the
  host renders as an interactive widget. Its refresh button makes the host call
  the tool again, so each refresh may be answered by a different container —
  visible in the chart, since the colour is whichever one replied.
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
| `src/index.ts` | the Worker: routing, secrets, serves `/host` |
| `host/index.html` | a host that drives the widget over 2026-07-28 |
| `wrangler.jsonc` | container, Durable Object binding, migration |
| `probe.sh` | smoke tool — raw 2026-07-28 over curl |
| [`AUTH.md`](AUTH.md) | **read before exposing this** — authn/authz |
| [`NOTES.md`](NOTES.md) | gotchas that cost real time to find |

## Prerequisites

**To run locally:** Python 3.12+. FastMCP 4 has no wheel for the 3.9 that ships
with macOS, where the install fails with a misleading *"no matching distribution
found"*. Node 20+ and Docker running only if you want the Worker and several
containers.

**To deploy:** the above, plus a **Workers Paid plan** — Containers are not on
the free tier.

## Run locally

No Cloudflare account needed.

### 1. The tools — no Docker

```bash
git clone https://github.com/tameernoor/fastmcp4-cf && cd fastmcp4-cf
uv venv --python 3.12 .venv                    # or: python3.12 -m venv .venv
uv pip install --prerelease=allow -r requirements.txt

REQUEST_STATE_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
  PORT=9000 .venv/bin/python server.py
```

In another shell:

```bash
./probe.sh http://localhost:9000 server/discover
./probe.sh http://localhost:9000 tools/call monitor
```

`server/discover` replaced the `initialize` handshake — it returns
`supportedVersions`, `resultType`, `instructions`, and cache hints.

### 2. Plus the widget in a browser

```bash
uv pip install --prerelease=allow 'fastmcp[apps]'   # dev-only; NOT in the image
.venv/bin/fastmcp dev apps server.py
```

A tool picker, a real host bridge, and a JSON-RPC log panel. One process, so the
instance never changes — for that you want the Worker.

### 3. The Worker and three containers — needs Docker

```bash
npm install
cp .dev.vars.example .dev.vars
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste into .dev.vars
npm run dev                                                 # :8787 by default
```

That serves two things on the same origin:

| | |
| --- | --- |
| `/mcp` | the MCP endpoint |
| **`/host`** | a host page that drives the widget over **2026-07-28** |

Open **http://localhost:8787/host**. Refresh the widget repeatedly and a
different container answers each time. `probe.sh` works against the Worker too:

```bash
./probe.sh http://localhost:8787 tools/call whoami
```

To drive the widget from FastMCP's preview instead, point it at the Worker
rather than letting it boot its own server — `fastmcp run` accepts a URL:

```bash
.venv/bin/fastmcp dev apps http://localhost:8787/mcp
```

Note that preview connects on **2025-11-25**, not 2026-07-28. See
[`host/`](#the-host) below for why.

`.dev.vars` is local-dev config and deliberately holds no auth settings — you
cannot mint a Cloudflare Access token on localhost, so adding them makes every
local request `401`. Deploy-time values live in `.deploy.vars` instead.

### The server asking a question back

`delete_notes` returns a question instead of blocking on one:

```bash
# Round 1 — the server asks. Note resultType: "input_required".
./probe.sh http://localhost:9000 tools/call delete_notes '{"folder":"notes"}'

# Round 2 — retry the SAME call with the answer and the state.
STATE=$(./probe.sh http://localhost:9000 tools/call delete_notes '{"folder":"notes"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['requestState'])")

./probe.sh http://localhost:9000 tools/call delete_notes '{"folder":"notes"}' \
  '{"confirm":{"action":"accept","content":{"value":true}}}' "$STATE"
```

The tool body runs from the top on **both** rounds — locals don't survive.
`ctx.input_responses` is `None` the first time, populated the second; anything
else that must carry over goes in `request_state`.

### An interactive widget

`monitor` renders as HTML in a sandboxed iframe instead of printing as text —
the [`io.modelcontextprotocol/ui`](https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp)
extension. It is negotiated, not core, so `monitor` returns a plain dict and
hosts without it just show the numbers.

A tool points at a resource, and the resource is the HTML:

```python
@mcp.tool(app=AppConfig(resource_uri=MONITOR_URI, visibility=["model", "app"]))
def monitor() -> dict[str, object]:
    ...

@mcp.resource(MONITOR_URI, meta={"ui": app_config_to_meta_dict(
    AppConfig(csp=ResourceCSP(resource_domains=["https://unpkg.com"])))})
def monitor_ui() -> str:
    return MONITOR_HTML
```

`ui://` infers `text/html;profile=mcp-app`. The host renders that HTML in a
sandboxed iframe: scripts boxed off from the rest of the app, and a CSP built
from the origins you declared. **Declare every external one** — anything missing
is blocked, so a forgotten entry means a blank panel and no error.

The refresh button never talks to this server. It asks the *host* to call the
tool, which is why the model sees that you clicked.
Run it with [step 2 or 3](#2-plus-the-widget-in-a-browser) above.

`fastmcp[apps]` is only needed for FastMCP's preview — `AppConfig` is core
FastMCP, so the image installs plain `requirements.txt`.

## The host

Nothing shipping today drives an MCP App over 2026-07-28. `ext-apps@1.7.5`
peer-depends on `@modelcontextprotocol/sdk ^1.29.0`, and that line's
`LATEST_PROTOCOL_VERSION` is still `2025-11-25` even at 1.30.0 — so FastMCP's
preview, and every other MCP Apps host, connects on the old protocol. A v2 SDK
exists (`@modelcontextprotocol/client@2.0.0`); ext-apps has no 2.x line.

So [`host/index.html`](host/index.html) is a minimal host that does. The Worker
serves it at **`/host`**, same origin as `/mcp`, which is the only reason it
needs no CORS. Around 200 lines, no dependencies, no build step:

- **The MCP leg is hand-written `fetch`**, mirroring `probe.sh`. Using an SDK
  would hide the thing being demonstrated, and no JS SDK speaks this yet anyway.
  Four calls, none of them `initialize`, no session.
- **The view leg is the `ui/*` dialect over postMessage**, which is plain
  JSON-RPC with no envelope. The widget is an unmodified ext-apps `App` and
  cannot tell the other side is speaking a newer protocol.
- **It enforces the resource's CSP.** Building the iframe policy from
  `_meta.ui.csp` is the host's job; ignore it and the widget runs unrestricted.
- **It gates on `_meta.ui.visibility`.** Only tools marked `"app"` may be
  invoked from the widget, so an app cannot reach your whole tool surface.
- **It answers the server's questions.** `delete_notes` returns
  `input_required`, the page renders it, and retries the same call with
  `inputResponses` and the sealed `requestState`. FastMCP's preview has no
  handling for this at all.

The iframe runs with `sandbox="allow-scripts"` and no `allow-same-origin`:
`srcdoc` inherits the parent origin, so keeping that flag would leave the widget
same-origin with the host, able to read its DOM and call `/mcp` with your
session.

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

The host page deploys with the Worker, at
`https://fastmcp4-cf.<your-subdomain>.workers.dev/host`. It is a plain route,
not a static asset binding, so there is nothing extra to provision — but it is
also **public unless you put Access in front of it**, and it carries a
one-click destructive control. See [Authentication](#authentication).

`REQUEST_STATE_KEY` seals the multi-round-trip `request_state`. **Every instance
needs the same value** — see [NOTES.md](NOTES.md) for why the default breaks
under a load balancer. The server refuses to boot without it.

Python changes need a container restart, not just a deploy — running containers
keep serving the previous image for several minutes. [NOTES.md](NOTES.md) covers
that. The host page is different: it is bundled into the Worker, so it updates
the moment `wrangler deploy` finishes.

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
  (`max_instances`)

`server.py` runs with `stateless_http=True`, which is what lets **both**
protocol eras be load-balanced with no affinity. Turn it off and clients on the
old protocol break intermittently — see [NOTES.md](NOTES.md).

Before pushing:

```bash
npm run typecheck    # tsc --noEmit; `npm run check` bundles but does NOT check
npm run check        # wrangler deploy --dry-run
```

## Authentication

**Off by default** — as cloned, anyone with the URL can call every tool, and the
server says so on boot. That includes `/host`, which is a working console with a
destructive button on it, so deploying this open publishes rather more than an
API. Access covers the whole Worker, `/mcp` and `/host` alike.

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
