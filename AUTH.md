# Auth on a FastMCP 4 / MCP 2026-07-28 server

**This template ships with authentication off.** As cloned, anyone who finds
the URL can call every tool, and the server warns about it on boot. Read this
before exposing it — [Option 0](#option-0--cloudflare-access-what-this-template-wires-up)
is the shortest path to closing it.

Everything marked ✅ was verified directly against `fastmcp==4.0.0b1` /
`mcp==2.0.0`. Everything else is from the spec or FastMCP's docs, linked at the
bottom.

---

## Option 0 — Cloudflare Access (what this template wires up)

Because this runs on Cloudflare, there is a path the rest of this document
doesn't cover: let **Access** be the authorization server. Enable *Managed
OAuth* on an Access application and the whole OAuth flow happens at the edge —
your server never runs one.

```
client → Cloudflare Access → Worker → container
            (runs OAuth)   (moves the JWT)  (verifies it)
```

1. Client hits the URL. Access replies `401` with a `WWW-Authenticate` header
   pointing at its OAuth metadata — not a `302`, so non-browser clients cope.
2. Client does the authorization-code flow; Access issues an opaque token
   (`oauth:CvNoo…`).
3. On each later request Access resolves that token and *"forwards a signed
   assertion to your origin"* as `Cf-Access-Jwt-Assertion`.
4. `src/index.ts` moves that assertion into `Authorization: Bearer` so the
   Python side needs no Cloudflare-specific code.
5. `server.py` verifies it against Access's JWKS — signature, issuer, audience.

### Setup

✅ Verified working on a plain `workers.dev` hostname — **no custom domain
required.** Dashboard work first, because it can't be scripted with a wrangler
token:

1. **Create the application.** For a `workers.dev` URL the shortcut is
   *Workers & Pages* → your Worker → *Settings* → *Domains & Routes* →
   **Enable Cloudflare Access**. (The long way — *Zero Trust* → *Access
   controls* → *Applications* → *Self-hosted* — asks for a hostname from a
   dropdown of **zones you own**, and `workers.dev` is not one of them.)
2. **Add a policy** — e.g. *Emails ending in* `@yourdomain.com`. Without one,
   nobody can get in no matter what else is right.
3. **Enable Managed OAuth** — the application's **Advanced settings** tab.
   See "Two things that will block you" below; this is not optional.
4. **Set Allowed redirect URIs** — same tab. Add your client's callback, e.g.
   `https://claude.ai/*`.
5. **Note two values**: your **team domain**
   (`yourteam.cloudflareaccess.com`) and the application's **AUD tag**.

Then give them to the server:

```bash
# local dev
cp .dev.vars.example .dev.vars     # then fill both in

# deployed
npx wrangler secret put ACCESS_TEAM_DOMAIN
npx wrangler secret put ACCESS_AUD
npm run deploy
```

Neither value is confidential — the team domain is public and the AUD is an
identifier — but they point at *one* Zero Trust org, so they stay out of the
repo rather than sending every clone at someone else's account.

**`.env` will not work for a deployment.** Cloudflare's docs are explicit that
`.env` and `.dev.vars` are local-development only; they never reach a deployed
Worker. Neither a plain `.env` nor `wrangler deploy --env-file .env` produces a
binding. Deploy-time values are either committed `vars` in `wrangler.jsonc` or
secrets — nothing else.

**Order matters when switching from `vars` to secrets.** A binding name cannot
be both, so `wrangler secret put` fails against a Worker that still has the
value as a `var`:

```
Binding name 'ACCESS_TEAM_DOMAIN' already in use. [code: 10053]
```

Remove the `vars` block, **deploy**, then set the secrets.

### Two things that will block you

Both produce confusing symptoms. Neither is obvious from the dashboard.

**1. Managed OAuth off → clients get `302`, not `401`.**

Access will protect the endpoint for browsers while being useless to an MCP
client: it answers with a redirect to an HTML login page, which the client
cannot act on. Diagnose it without touching the dashboard — ask the resource
metadata what it advertises:

```bash
curl -s https://<your-worker>/.well-known/cloudflare-access-protected-resource/mcp
```

Managed OAuth **off** lists only `cloudflared` (a human CLI tool). **On**, an
`oauth` method appears, and `/.well-known/oauth-protected-resource/mcp` goes
`302` → `200`.

**2. Empty "Allowed redirect URIs" → registration is refused.**

The client tries to register itself via Dynamic Client Registration and gets:

```json
{"error":"invalid_client_metadata",
 "error_description":"redirect_uri is not allowed by the account configuration"}
```

Claude surfaces this as *"Couldn't register with … sign-in service."* The
allowlist starts empty, so **every** client is refused until you add one. Test
it directly:

```bash
curl -sX POST https://<team>.cloudflareaccess.com/cdn-cgi/access/oauth/registration \
  -H 'Content-Type: application/json' \
  -d '{"client_name":"probe","redirect_uris":["https://claude.ai/api/mcp/auth_callback"],
       "grant_types":["authorization_code"],"response_types":["code"],
       "token_endpoint_auth_method":"none"}'
```

A `client_id` back means it's working.

**Bonus symptom:** `Invalid nonce. Please try logging in again.` usually just
means stale state from earlier failed attempts. Log out at
`https://<team>.cloudflareaccess.com/cdn-cgi/access/logout`, remove the
connector, and do one clean pass.

### What correct looks like

```
$ curl -i -X POST https://<your-worker>/mcp -d '…'
HTTP/2 401
www-authenticate: Bearer realm="OAuth", error="invalid_token",
  resource_metadata="https://<your-worker>/.well-known/cloudflare-access-protected-resource/mcp"
```

and once a client has authenticated, every request reaching the Worker carries
`Cf-Access-Jwt-Assertion`. Watch it live with `npx wrangler tail --format json`.

```python
# what server.py then builds — no OAuth flow in this process, just verification
JWTVerifier(
    jwks_uri=f"https://{team_domain}/cdn-cgi/access/certs",
    issuer=f"https://{team_domain}",
    audience=aud,
)
```

Unset both and the server boots with **no authentication** and warns on stdout.

### Service tokens: authenticating without a browser

Access's OAuth flow needs a human at a browser, which is useless for CI, a
monitor, or any script. **Service tokens** are the machine equivalent — two
headers, no interaction:

```bash
curl -X POST https://<your-worker>/mcp \
  -H "CF-Access-Client-Id: <id>.access" \
  -H "CF-Access-Client-Secret: <secret>" \
  ...
```

Create one under *Zero Trust* → **Access controls → Service credentials →
Service Tokens**. The secret is shown **once**.

Then let it into the application — this is the step that is easy to miss.
Add a **second policy** on the app:

- **Action: `Service Auth`** — not `Allow`. An `Allow` policy evaluates a human
  identity, and a service token has none, so it never matches.
- **Include: Service Token** → the one you created.

Your existing email policy stays alongside it. Until that policy exists, Access
rejects the token *exactly* as it rejects no credentials at all — same `401`,
same body — so there is no signal telling you the token is the problem.

**A service token carries no `email`.** Call `whoami` with one and the server
reports what it actually sees:

```json
"caller": {
  "authenticated": true,
  "email": null,
  "common_name": "ci-probe.access",
  "is_admin": false,
  "all_claim_keys": ["aud","common_name","exp","iat","iss","sub","type"]
}
```

So an email-based check **authenticates** a service token and **authorizes it
for nothing**. That is the right default — a CI credential should not inherit a
person's permissions — but it means automated probing sees only ungated tools.
To grant a machine something specific, authorize on `common_name`:

```python
ADMIN_SERVICE_TOKENS = {"ci-probe.access"}

def is_machine_admin(ctx) -> bool:
    cn = (ctx.token.claims.get("common_name") or "") if ctx.token else ""
    return cn in ADMIN_SERVICE_TOKENS
```

Two things to remember: a service token is a **second door** into the
application, bypassing your identity policy entirely — delete it in the
dashboard to close it. And *"if your Access application only has Service Auth
policies, you must send the service token on every subsequent request."*

### What about groups?

Tempting — manage membership in Cloudflare, keep names out of your code — but
check what your identity provider actually sends first. **Cloudflare Access
Groups are not JWT group claims.** Access Groups are reusable rule-lists for
*policies*; they do not appear in the token your server receives. Group claims
come from the IdP, and per Cloudflare, *"identity provider groups are only
included in the token when you explicitly configure groups as a custom SAML
attribute or OIDC claim. Access does not add them automatically."*

With **One-time PIN** there is no IdP behind it, so there are no groups at all.
Also note Access trims custom claims past ~1 KB, so *"a user who belongs to many
groups can receive a token without their groups claim"* — group checks can fail
for exactly the people who have the most groups.

An email set in an env var is unglamorous and works everywhere. Reach for groups
when you have a real IdP and enough people that a list stops being readable.

### Caveats

- **Don't enable Managed OAuth on a server that runs its own OAuth.**
  Cloudflare's docs warn against exactly that. Pick one — this template's
  `build_auth()` only ever *verifies* a token, so there is no clash.
- **Policy lives in Cloudflare's dashboard**, not in your code — good for
  non-engineers changing access, worse for reviewability and portability.
- **The origin is only as protected as its front door.** Access enforces at
  the edge; the JWT check in `server.py` is the second layer. Keep both.
- Access is Cloudflare-specific. Everything below this section is not, and is
  what you'd use anywhere else.

Cloudflare documents that a `workers.dev`-only Worker cannot have *"certain
Cloudflare Zero Trust endpoints"* protected. Managed OAuth is **not** one of
them — verified end to end on `*.workers.dev` with a real client.

---

## Two lists, two jobs

Both contain email addresses; they answer different questions.

| where | question | example |
| --- | --- | --- |
| **Access policy** (Cloudflare dashboard) | may this person reach the server at all? | `ll@example.com` → through the door |
| **`ADMIN_EMAILS`** (your server) | which tools may they call? | `ll@example.com` → may call `delete_notes` |

Cloudflare cannot answer the second one — an identity provider knows *who you
are*, not what "admin" means in your application. Remove yourself from
`ADMIN_EMAILS` and you still connect fine, you just stop seeing the gated tool.
Remove yourself from the Access policy and you never arrive.

---

## The two halves

|                    | question                | who answers it            |
| ------------------ | ----------------------- | ------------------------- |
| **Authentication** | *who are you?*          | your identity provider    |
| **Authorization**  | *what may you do?*      | **your server**           |

Conflating them is the usual mistake. Google can tell you a caller is
`alice@example.com`. It cannot tell you they may delete things — it has never
heard of your app.

## How a request carries identity

```
1. user logs in with OAuth       → IdP issues a token
2. client sends it on EVERY request:  Authorization: Bearer <token>
3. server reads the token per request → decides what this caller may see
4. tools/list returns only permitted tools
5. tools/call re-checks before running
```

There is no "logged-in state" on the server. Under MCP 2026-07-28 there is no
session to hold it in — the token *is* the state, arriving fresh each time.

This is why per-user tool lists still work despite the spec removing
per-connection variation. **Per-connection is dead; per-caller is fine.**

Dead:

```
tools/list         → 3 tools
authenticate(...)  → server remembers "now admin"
tools/list         → 20 tools        ← nothing to remember in
```

Fine:

```
tools/list + Bearer <alice@example.com>  → 20 tools
tools/list + Bearer <guest@other.com>   →  3 tools
```

Consequence: **privilege escalation happens at login, not mid-conversation.**

---

## Authentication: wiring a provider

```python
from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider

mcp = FastMCP(
    name="my-server",
    auth=GoogleProvider(
        client_id=...,
        client_secret=...,
        base_url="https://my-server.example.com",   # ← audience. see Hard rules.
        required_scopes=["openid", "email"],
        jwt_signing_key=...,   # dedicated secret, NOT derived from client_secret
    ),
)
```

✅ This constructor is **byte-identical between fastmcp 3.4.2 and 4.0.0b1** —
all 21 parameters, all defaults. Auth is not the risky part of a 3.x → 4.x
migration.

Two settings worth understanding:

- **`jwt_signing_key`** — after Google vouches for the user, *your* server
  issues its own token to the client, signed with this. Pin it to a dedicated
  secret so rotating Google credentials doesn't sign everyone out.
- **`required_scopes=["openid", "email"]`** — what you ask Google for. Enough to
  answer "who is this", nothing more. Don't ask for Drive to check identity.

✅ `enable_cimd=True` is the default in **both** versions. Client ID Metadata
Documents are not a FastMCP 4 feature — 3.x already has them on.

### What's in a Google token

```
sub    114...                  email          alice@example.com
hd     example.com             email_verified true
```

No `notes:write`. No `admin`. `require_scopes("notes:write")` can **never**
work with plain Google — Google will not mint a scope it has never heard of.

Prefer **`hd`** (Workspace hosted domain) over matching the email suffix. A
suffix check is spoofable by an address like `x@example.com.evil.com`.

---

## Authorization: two paths

### Path 1 — Google + your own mapping

Keep Google for identity; write policy in code. ✅ An auth check is any
`Callable[[AuthContext], bool]`, sync or async.

```python
ADMINS = {"alice@example.com"}

def same_org(ctx) -> bool:
    return (ctx.token.claims.get("hd") or "") == "example.com" if ctx.token else False

def is_admin(ctx) -> bool:
    return ctx.token.claims.get("email") in ADMINS if ctx.token else False

@mcp.tool                              # everyone
def search(q: str) -> str: ...

@mcp.tool(auth=same_org)               # anyone in the Workspace
def internal_search(q: str) -> str: ...

@mcp.tool(auth=[same_org, is_admin])   # both must pass (AND)
def delete_all() -> str: ...
```

✅ `AuthContext` exposes `.token` and `.component`. ✅ `AccessToken` carries
`claims`, `scopes`, `subject`, `client_id`, `resource`, `expires_at`.
✅ Multiple checks combine with AND, stopping at the first failure.

### Path 2 — a real IdP in front

Auth0, WorkOS, Keycloak, Descope, Entra. Users may still *log in with Google* —
the IdP federates it — but the IdP mints the token, so it can carry scopes and
roles you manage outside your code.

```python
from fastmcp.server.auth import require_scopes, require_roles

@mcp.tool(auth=require_scopes("notes:write"))
def create_note(text: str) -> str: ...

def keycloak_roles(claims: dict) -> list[str]:
    return claims["realm_access"]["roles"]

@mcp.tool(auth=require_roles("admin", extract=keycloak_roles))
def admin_operation() -> str: ...
```

✅ `require_scopes`, `require_roles`, and `restrict_tag` all exist in 4.0.0b1.
`require_roles` and `InsufficientScopeError` are documented as v4.0.0+.

### Choosing

Path 1 while permissions fit in a readable dict and engineers own them. Path 2
when non-engineers must change access, when roles outgrow a dict, or when you
need audit trails and revocation you didn't build.

Either way you still need audience validation — see Hard rules.

---

## Hard rules

These are settled OAuth practice, not MCP fashion. Get them wrong and the rest
doesn't matter.

### 1. Validate the token audience

A server **MUST NOT** accept a token that wasn't issued for it, and **MUST**
reject any token whose audience isn't its own canonical URI. The client names
the target via the `resource` parameter (RFC 8707); the IdP mints the token with
that audience; you verify it.

This defeats the **confused deputy** — the signature MCP attack, where a token
minted for service A is replayed against service B.

In FastMCP this rides on the provider's `base_url`. You don't implement it; you
*can* misconfigure it. Make sure `base_url` is the server's real public URL.

### 2. Never pass the incoming token downstream

If a tool calls another API on the user's behalf, obtain a **new** token for
that audience. Forwarding the one the client handed you is token passthrough and
the spec forbids it. This is the most common real-world mistake.

### 3. Least privilege, per tool

Read open, write scoped, destructive scoped harder. Fine-grained scopes beat one
`admin` scope: they grant narrowly, and they let a client request a targeted
step-up instead of a full re-login.

### 4. Defense in depth — filter the list *and* check the call

✅ FastMCP runs the check twice: once building `tools/list` (`server.py:954`),
so unauthorized tools are **invisible**, and again on `tools/call`.

Hiding is UX. The call-time check is the security boundary. A caller can always
name a tool you never advertised — never rely on hiding alone.

### 5. Don't build your own identity

Delegate to an IdP. Your server verifies tokens and enforces policy; it should
never store passwords or mint identities.

---

## The caching trap

MCP 2026-07-28 added `ttlMs` / `cacheScope` to list results so clients and
intermediaries can cache them. A per-user tool list cached as `"public"` would
let a shared proxy serve one user's tools to another.

✅ FastMCP defaults safely (`fastmcp/server/caching.py:57`):

```python
hint = CacheHint(ttl_ms=cache_ttl * 1000, scope=cache_scope or "private")
```

No `cache_ttl` → no hint → no caching. Set a TTL and scope defaults to
`"private"`. **Never set `cache_scope="public"` on a server that filters tools
per user.**

---

## Storage

FastMCP's OAuth state — client registrations and encrypted tokens — lives under
`FASTMCP_HOME`. On ephemeral disk it is lost on every deploy, forcing everyone
to log in again. Put it on a volume, or configure `client_storage`.

CIMD shrinks the *registration* half of this over time (clients host their own
metadata at a URL instead of registering), but tokens still need a home.

---

## Quick reference

```python
from fastmcp.server.dependencies import get_access_token

@mcp.tool(auth=same_org)
def whoami() -> str:
    token = get_access_token()          # inside a tool, for row-level logic
    return token.claims.get("email", "unknown")
```

| need                       | use                                        |
| -------------------------- | ------------------------------------------ |
| who is calling             | `get_access_token()` inside the tool        |
| gate one tool              | `@mcp.tool(auth=check)`                     |
| gate by OAuth scope        | `require_scopes("a", "b")`                  |
| gate by token claim/role   | `require_roles(..., extract=...)`           |
| gate every tool with a tag | `restrict_tag(tag, scopes=[...])`           |
| gate globally              | `AuthMiddleware`                            |
| custom rule                | any `Callable[[AuthContext], bool]`         |
| combine rules              | pass a list — AND, fails fast               |

---

## Caveats

`fastmcp==4.0.0b1` is a beta and pulls `pydantic 2.14.0a1`, an alpha. Per-tool
`auth=` is genuinely new — treat it as the direction the ecosystem just
standardized on, not a decade-old convention. The Hard rules above are the
opposite: old, settled, and non-negotiable.

## Upstream

- [FastMCP: Authorization](https://gofastmcp.com/servers/authorization) — the
  fuller treatment of per-tool checks, `AuthMiddleware`, and visibility filtering
- [FastMCP: auth providers](https://gofastmcp.com/servers/auth/authentication) —
  ~30 providers beyond Google
- [MCP: security best practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)
- [MCP 2026-07-28 authorization spec](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
