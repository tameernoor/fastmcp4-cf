# Notes

Things that cost real time to find while building this. Each was measured, not
inferred. Auth-specific gotchas live in [AUTH.md](AUTH.md).

---

## Load balancing breaks legacy clients

**Symptom:** a client reports *"This connector has no tools available."* Some
requests work, some don't. Reads as flakiness.

MCP 2026-07-28 removes sessions, so the obvious move is to load-balance. But
clients still speaking **2025-11-25 need sticky sessions**, and right now that
is most of them — Claude.ai included, one day after the spec published:

```
user-agent:           Claude-User
mcp-protocol-version: 2025-11-25
mcp-session-id:       a2543266ca85…
```

A legacy client opens with `initialize` and gets an `Mcp-Session-Id` held in
*one* container's memory. Spray its later requests across the pool and roughly
`1 - 1/N` land somewhere that never saw it:

```json
{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Session not found"}}
```

Measured here before the fix: `tools/list` failed 2 of 5 times.

**Hashing the session id does not work.** `initialize` arrives with no session
— and no protocol-version header either — so the instance that mints the
session is chosen before there is anything to hash.

**Fix:** route on the protocol version. Modern traffic fans out, legacy traffic
is pinned to one instance (`src/index.ts`):

```ts
const instance = isModern(request)                       // 2026-07-28
  ? await getRandom(env.MCP_CONTAINER, INSTANCES)        //   any instance
  : env.MCP_CONTAINER.getByName(LEGACY_INSTANCE);        //   always the same one
```

Legacy sessions were never load-balanceable without shared session storage —
which is exactly what 2026-07-28 set out to remove. Until legacy clients age
out, a dual-era server behind a load balancer has to do this.

`max_instances` must cover `INSTANCES` **plus** the pinned legacy instance.

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
