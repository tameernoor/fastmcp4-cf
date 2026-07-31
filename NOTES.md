# Notes

Things that cost real time to find while building this. Each was measured, not
inferred. Auth-specific gotchas live in [AUTH.md](AUTH.md).

---

## Load balancing breaks legacy clients — unless you serve them statelessly

**Symptom:** a client reports *"This connector has no tools available."* Some
requests work, some don't. Reads as flakiness rather than a routing bug.

MCP 2026-07-28 removes sessions, so the obvious move is to load-balance. But
clients still speaking **2025-11-25 open with `initialize` and expect a
session**, and right now that is most of them — Claude.ai included, one day
after the spec published:

```
user-agent:           Claude-User
mcp-protocol-version: 2025-11-25
mcp-session-id:       a2543266ca85…
```

That session lives in *one* process's memory. Spray the client's later requests
across the pool and roughly `1 - 1/N` land somewhere that never saw it:

```json
{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Session not found"}}
```

Measured here: `tools/list` failed 2 of 5 times.

**Fix — `stateless_http=True`.** FastMCP will serve 2025-era requests without a
session at all (a fresh transport per request), so both eras fan out freely and
the router needs no era-awareness:

```python
mcp.run(transport="http", ..., stateless_http=True)
```

Verified: legacy `initialize` returns **no** `Mcp-Session-Id`, and legacy
`tools/list` then succeeds repeatedly with no session to lose. The TypeScript
SDK makes the same choice by default (`legacy: 'stateless'`); FastMCP's Python
default is session-based, which is fine on one process and wrong behind a load
balancer.

**If you cannot go stateless** — because you rely on something the session
holds — the fallback is sticky routing: pin the whole legacy era to one
instance, keyed off the protocol-version header.

```ts
const instance = request.headers.get("mcp-protocol-version") === "2026-07-28"
  ? await getRandom(env.MCP_CONTAINER, INSTANCES)
  : env.MCP_CONTAINER.getByName("instance-legacy");
```

Note that **hashing the session id cannot work**: `initialize` arrives with no
session — and no protocol-version header either — so the instance that mints
the session is chosen before there is anything to hash. And pinning costs
legacy clients any load balancing at all, which is why `stateless_http` is the
better answer where it fits.

If you do pin, `max_instances` must cover `INSTANCES` **plus** the pinned
instance.

---

## request_state and the ephemeral sealing key

**Symptom:** guard tools work sometimes. Retries fail with
`Invalid or expired requestState`.

A guard tool's `request_state` is sealed (AES-GCM) on the way out and verified
on the way back, because the spec treats client-echoed state as
attacker-controlled. FastMCP's default is a **per-process ephemeral key** —
correct for a single process, wrong behind any load balancer, because round 2
usually lands on an instance that cannot unseal what another instance sealed.

Measured here: **1 of 6 retries succeeded** with the default; **6 of 6** after
passing a shared key.

**Fix:** give every instance the same key.

```python
RequestStateSecurity(keys=[REQUEST_STATE_KEY], audience="fastmcp4-cf")
```

Keys must be at least 32 bytes. `keys` is a rotation ring — `keys[0]` seals,
all keys unseal, so roll `[old, new]` → `[new, old]` → `[new]`.

`server.py` refuses to boot without one rather than falling back to something
guessable, and `src/index.ts` passes it into the container — secrets do not
reach a container unless handed over explicitly.

FastMCP documents this properly at
[gofastmcp.com/servers/elicitation](https://gofastmcp.com/servers/elicitation);
what is measured above is how badly it degrades in practice.

---

## The app preview runs one process

**Symptom:** an MCP App built to show instances changing shows one instance,
forever. Load balancing looks broken.

`fastmcp dev apps server.py` starts a single Python server and points the
browser at it. There is only one instance to answer.

**Fix:** `fastmcp run` accepts a URL, so aim the dev host at the Worker instead
of letting it boot a server. Each proxied call is then its own request, fanned
out by `getRandom`:

```bash
npm run dev
fastmcp dev apps http://localhost:8787/mcp
```

Measured: 8 refreshes, all 3 containers answered, each with its own
`calls_served`.

---

## A deploy is not a cutover

**Symptom:** you change Python, deploy, test immediately, and conclude your
change did nothing.

`wrangler deploy` updates the Worker at once, but **running containers keep
serving the previous image for several minutes**. Measured here: still answering
from the old build six minutes after a successful deploy, then it rolled.

Anything the Python side reads at process start — code, `REQUEST_STATE_KEY`,
`ADMIN_EMAILS`, the Access settings — needs a container **restart** to take
effect. A secret set with `wrangler secret put` reaches the Worker immediately
and the container only on its next boot (`sleepAfter`, or a rollout).

Give a tool a new field and use its presence as a canary for which build
answered. `whoami` does this. To watch the rollout directly:

```bash
npx wrangler containers info <app-id>   # `version` and image digest bump
```

---

## Local dev generally wants auth off

`.dev.vars` feeds local `wrangler dev`. Put `ACCESS_TEAM_DOMAIN` / `ACCESS_AUD`
in there and every local request needs a real Cloudflare Access JWT — which you
cannot mint on localhost. Everything answers `401` and it looks like the server
is broken.

So the two files are deliberately different:

| file | for | holds |
| --- | --- | --- |
| `.dev.vars` | local `wrangler dev` | `REQUEST_STATE_KEY` only — no auth |
| `.deploy.vars` | `./redeploy.sh` | all four, including the Access settings |

Both are gitignored; `.example` versions of each are committed.

## `.env` does not reach a deployment

Cloudflare's docs are explicit that `.env` and `.dev.vars` are
local-development only. Neither a plain `.env` nor
`wrangler deploy --env-file .env` produces a binding on a deployed Worker.
Deploy-time values are either committed `vars` in `wrangler.jsonc` or secrets —
nothing else.

And a binding name cannot be both, so switching from one to the other has an
order:

```
Binding name 'ACCESS_TEAM_DOMAIN' already in use. [code: 10053]
```

Remove the `vars` block, **deploy**, then set the secrets.
