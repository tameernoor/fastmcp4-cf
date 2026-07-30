"""A FastMCP 4 server speaking MCP 2026-07-28, for Cloudflare Containers.

Nothing here is Cloudflare-specific — it's an ordinary FastMCP server listening
on 0.0.0.0:8080. The Worker in src/index.ts is what puts it on Cloudflare, and
it fans requests across several container instances at random. Because the
2026-07-28 protocol carries no session, any instance can answer any request:
call `whoami` repeatedly and watch the instance id change under you.

Two tools, each demonstrating one half of the new protocol:
  whoami        stateless dispatch — no session, so no affinity needed
  delete_notes  a guard tool — the multi-round-trip replacement for elicitation
"""

import json
import os
import uuid

import mcp.types as mt
from fastmcp import Context, FastMCP
from fastmcp.tools import InputRequiredToolResult
from mcp.server.request_state import RequestStateSecurity

INSTANCE = f"{os.uname().nodename}-{uuid.uuid4().hex[:6]}"
_calls_served = 0

# A guard tool's `request_state` is sealed on the way out and verified on the
# way back. FastMCP's default is RequestStateSecurity.ephemeral() — a key
# "generated now and held only by this process" — which breaks the moment a
# retry lands on a different instance than the one that minted the state.
# That is exactly what getRandom() in src/index.ts does, so every instance
# must derive the same key from a shared secret.
#
# There is deliberately NO fallback: a template that silently boots under a
# hardcoded key would hand every deployment the same publicly-readable secret.
REQUEST_STATE_KEY = os.environ.get("REQUEST_STATE_KEY", "")
if len(REQUEST_STATE_KEY) < 32:
    raise SystemExit(
        "REQUEST_STATE_KEY must be set to at least 32 bytes of secret randomness.\n"
        "\n"
        '  generate:  python -c "import secrets; print(secrets.token_hex(32))"\n'
        "  local dev: cp .dev.vars.example .dev.vars   (then paste it in)\n"
        "  deployed:  npx wrangler secret put REQUEST_STATE_KEY\n"
        "\n"
        "It seals the multi-round-trip `request_state`. Every instance behind\n"
        "the load balancer needs the SAME value or guard tools break on retry."
    )

def build_auth():
    """Verify Cloudflare Access identity, when running behind Access.

    Access authenticates at the edge and forwards a signed JWT to the origin;
    the Worker moves it into `Authorization: Bearer` so all this side has to do
    is verify the signature, issuer, and audience against Access's JWKS. There
    is no OAuth flow in this process.

    Both variables unset = no auth at all. That is fine for a demo and wrong
    for anything else, so it says so loudly.
    """
    team_domain = os.environ.get("ACCESS_TEAM_DOMAIN", "").strip()
    aud = os.environ.get("ACCESS_AUD", "").strip()

    if not (team_domain and aud):
        print(
            "WARNING: no authentication — anyone with the URL can call every "
            "tool. Set ACCESS_TEAM_DOMAIN and ACCESS_AUD to require Cloudflare "
            "Access. See AUTH.md.",
            flush=True,
        )
        return None

    from fastmcp.server.auth.providers.jwt import JWTVerifier

    issuer = f"https://{team_domain}"
    return JWTVerifier(
        jwks_uri=f"{issuer}/cdn-cgi/access/certs",
        issuer=issuer,
        audience=aud,
    )


AUTH = build_auth()

# --- authorization -----------------------------------------------------------
# Authentication answers "who are you". This answers "what may you do" — a
# separate question, and the one that matters once tools stop being harmless.
#
# Cloudflare Access hands us the caller's identity in the JWT's claims, so the
# check is an ordinary predicate over `ctx.token`. Any callable works; FastMCP
# runs it twice — once to decide whether the tool appears in `tools/list` at
# all, and again before the call actually runs.
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
}


def is_admin_email(email: str | None) -> bool:
    """Whether this email is on the admin list. Also used by `whoami`."""
    return bool(email) and email.lower() in ADMIN_EMAILS


def is_admin(ctx) -> bool:
    """True for callers listed in ADMIN_EMAILS.

    With no authentication configured there are no identities to tell apart,
    so this passes — otherwise the demo would hide its own guard tool. The
    server has already warned, loudly, that it is wide open in that mode.

    Note a service token can never satisfy this: its JWT carries `common_name`,
    not `email`. That is a feature — machine credentials should not inherit a
    human's permissions — but it means CI probing with a service token sees
    only the ungated tools. Authorize on `common_name` if you want otherwise.
    """
    if AUTH is None:
        return True
    if ctx.token is None:
        return False
    return is_admin_email(ctx.token.claims.get("email"))


mcp = FastMCP(
    name="fastmcp4-cf",
    version="0.1.0",
    auth=AUTH,
    # `keys` is a rotation ring: keys[0] seals, every key unseals. To rotate
    # without downtime, roll [old, new] -> [new, old] -> [new].
    request_state_security=RequestStateSecurity(
        keys=[REQUEST_STATE_KEY],
        audience="fastmcp4-cf",
    ),
    instructions=(
        "A throwaway server for watching stateless MCP on Cloudflare "
        "Containers. Call whoami several times and compare the instance "
        "field — requests are load-balanced across instances with no session "
        "binding them together."
    ),
)


@mcp.tool
def whoami() -> str:
    """Which container instance served THIS call, and who the server thinks you are.

    Call it repeatedly and watch `instance` change: no session, so no affinity.

    `caller` is what authorization decisions are made from. A human logging in
    through Cloudflare Access has an `email` claim; a service token has
    `common_name` and no email at all — which is why an email-based check can
    authenticate a service token but never authorize one.
    """
    global _calls_served
    _calls_served += 1

    caller: dict[str, object] = {"authenticated": False}
    if AUTH is not None:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
        if token is not None:
            claims = token.claims or {}
            caller = {
                "authenticated": True,
                # The claims worth deciding on. `sub` is stable; `email` is
                # human logins; `common_name` is service tokens.
                "email": claims.get("email"),
                "common_name": claims.get("common_name"),
                "sub": claims.get("sub"),
                "is_admin": is_admin_email(claims.get("email")),
                # Everything else, so you can see what your IdP actually sends.
                "all_claim_keys": sorted(claims),
            }

    return json.dumps(
        {
            "instance": INSTANCE,
            "calls_served_by_this_instance": _calls_served,
            "caller": caller,
        },
        indent=2,
    )


@mcp.tool(auth=is_admin)
def delete_notes(folder: str, ctx: Context) -> str | InputRequiredToolResult:
    """Delete every note in a folder. Asks for confirmation first.

    Admin-only, to show the second layer: authentication got you through the
    door, authorization decides which doors. A caller who is not in
    ADMIN_EMAILS never sees this tool in `tools/list`, and is refused if they
    call it by name anyway — hiding is UX, the call-time check is the boundary.

    A guard tool: the kind of back-and-forth that used to be `await
    ctx.elicit(...)`. That blocked mid-call on a server-to-client request,
    which 2026-07-28 removed — so instead this RETURNS a question and gets
    called a second time with the answer.

    Round 1: ctx.input_responses is None  -> return the question
    Round 2: ctx.input_responses is set   -> read it and act
    """
    answer = ctx.input_responses.get("confirm") if ctx.input_responses else None

    if answer is None:
        # Nothing asked yet. Return the question as this call's result.
        # Anything needed on the next round goes in request_state — the tool
        # body starts from the top each time, so locals do not survive.
        doomed = 47  # pretend we counted them
        return InputRequiredToolResult(
            mt.InputRequiredResult(
                input_requests={
                    "confirm": mt.ElicitRequest(
                        method="elicitation/create",
                        params=mt.ElicitRequestFormParams(
                            mode="form",
                            message=(
                                f"Delete all {doomed} notes in '{folder}'? "
                                "This cannot be undone."
                            ),
                            requested_schema={
                                "type": "object",
                                "properties": {
                                    "value": {
                                        "type": "boolean",
                                        "title": "Yes, delete them",
                                    }
                                },
                                "required": ["value"],
                            },
                        ),
                    )
                },
                request_state=json.dumps({"folder": folder, "count": doomed}),
            )
        )

    # Round 2. The client answered; request_state came back as we minted it
    # (sealed on the wire, verified and unsealed before we see it).
    if answer.action != "accept":
        return f"Cancelled — nothing deleted from '{folder}'."

    carried = json.loads(ctx.request_state) if ctx.request_state else {}
    if not (answer.content or {}).get("value"):
        return f"Declined — nothing deleted from '{folder}'."

    return (
        f"Deleted {carried.get('count', '?')} notes from "
        f"'{carried.get('folder', folder)}'. (Demo — nothing was really deleted.)"
    )


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",  # noqa: S104 — inside a container, fronted by the Worker
        port=int(os.environ.get("PORT", "8080")),
    )
