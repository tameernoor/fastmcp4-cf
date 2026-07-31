"""A FastMCP 4 server speaking MCP 2026-07-28, for Cloudflare Containers.

Nothing here is Cloudflare-specific — it's an ordinary FastMCP server listening
on 0.0.0.0:8080. The Worker in src/index.ts is what puts it on Cloudflare, and
it fans requests across several container instances at random. Because the
2026-07-28 protocol carries no session, any instance can answer any request:
call `whoami` repeatedly and watch the instance id change under you.

Three tools, each demonstrating one part of the new protocol:
  whoami        stateless dispatch — no session, so no affinity needed
  monitor       an MCP App — a tool that renders as an interactive widget
  delete_notes  a guard tool — the multi-round-trip replacement for elicitation
"""

import json
import os
import resource
import socket
import time
import uuid

import mcp.types as mt
from fastmcp import Context, FastMCP
from fastmcp.apps import AppConfig, ResourceCSP, app_config_to_meta_dict
from fastmcp.tools import InputRequiredToolResult
from mcp.server.request_state import RequestStateSecurity

INSTANCE = f"{os.uname().nodename}-{uuid.uuid4().hex[:6]}"
BOOTED_AT = time.monotonic()
_calls_served = 0

# MCP Apps (io.modelcontextprotocol/ui) is a negotiated extension, not core, so
# `monitor` must stay useful as plain text. The widget is an enhancement.
MONITOR_URI = "ui://fastmcp4-cf/monitor.html"

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


def _rss_mb() -> float:
    """Current resident memory of this process, in MiB.

    `ru_maxrss` is only the high-water mark, so a live gauge built on it never
    falls. It's the off-Linux fallback: kilobytes on Linux, bytes on macOS.
    """
    try:
        with open("/proc/self/statm") as f:
            resident_pages = int(f.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return raw / 1024 if os.uname().sysname == "Linux" else raw / (1024 * 1024)


def _mem_total_mb() -> float | None:
    """Total memory visible to this container, or None off Linux."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def _cgroup(name: str) -> str | None:
    """Read a cgroup v2 file, or None if it isn't there (macOS, cgroup v1)."""
    try:
        with open(f"/sys/fs/cgroup/{name}") as f:
            return f.read().strip()
    except OSError:
        return None


def _ip() -> str | None:
    """This container's address on the network.

    Genuinely per-container, unlike /proc/sys/kernel/random/boot_id, which
    looks like a machine identifier and is identical across every container
    because they share the host kernel.
    """
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return None


def _container_stats() -> dict[str, object]:
    """What this container is actually using and allowed to use.

    cgroup v2 is the only per-container source: /proc lies about cpus and load.
    A limit reads "max" when uncapped, which is what local Docker gives you —
    a deployed instance has real numbers here.
    """
    used = _cgroup("memory.current")
    limit = _cgroup("memory.max")
    pids = _cgroup("pids.current")

    cpu_seconds = None
    for line in (_cgroup("cpu.stat") or "").splitlines():
        if line.startswith("usage_usec"):
            cpu_seconds = round(int(line.split()[1]) / 1_000_000, 2)

    cpu_limit = None
    quota, _, period = (_cgroup("cpu.max") or "").partition(" ")
    if quota.isdigit() and period.isdigit():
        cpu_limit = round(int(quota) / int(period), 2)

    return {
        "ip": _ip(),
        # Falls back to this process's RSS so the no-Docker loop still shows
        # something; cgroup counts the whole container, which is bigger.
        "memory_used_mb": round(int(used) / (1024 * 1024), 1)
        if used and used.isdigit()
        else round(_rss_mb(), 1),
        "memory_limit_mb": round(int(limit) / (1024 * 1024), 1)
        if limit and limit.isdigit()
        else None,
        "cpu_seconds": cpu_seconds,
        "cpu_limit": cpu_limit,
        "processes": int(pids) if pids and pids.isdigit() else None,
    }


@mcp.tool(app=AppConfig(resource_uri=MONITOR_URI, visibility=["model", "app"]))
def monitor() -> dict[str, object]:
    """Live vitals for the container instance that served this call.

    Renders as a dashboard on hosts that support MCP Apps.
    """
    global _calls_served
    _calls_served += 1

    # `host` is grouped apart because it is NOT per-container: /proc/loadavg
    # is not namespaced and os.cpu_count() ignores cgroup limits, so both read
    # identical on every instance. Don't build per-container gauges from them.
    load1, load5, load15 = os.getloadavg()
    return {
        "instance": INSTANCE,
        "uptime_s": round(time.monotonic() - BOOTED_AT, 1),
        "calls_served": _calls_served,
        "container": _container_stats(),
        "host": {
            "cpu_count": os.cpu_count(),
            "load": {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)},
            "memory_total_mb": _mem_total_mb(),
        },
    }


# The HTML the host drops into a sandboxed iframe — exactly what
# `resources/read` returns for MONITOR_URI.
MONITOR_HTML = """<!DOCTYPE html>
<meta name="color-scheme" content="light dark">
<style>
  :root {
    --bg: transparent; --fg: #16181d; --dim: #6b7280;
    --line: #e5e7eb; --card: #ffffff; --accent: #2563eb;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --fg: #e8eaed; --dim: #9aa0a6;
      --line: #2c2f36; --card: #16181d; --accent: #7aa2f7;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
    font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, sans-serif;
  }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 16px; max-width: 560px;
  }
  header { display: flex; align-items: baseline; gap: 8px; margin-bottom: 14px; }
  h1 { font-size: 14px; font-weight: 600; margin: 0; }
  .chip {
    font: 11px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
    padding: 4px 7px; border-radius: 999px;
    background: color-mix(in srgb, var(--accent) 14%, transparent);
    color: var(--accent);
  }
  .grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
    gap: 12px; margin-bottom: 16px;
  }
  .stat .k { font-size: 11px; color: var(--dim); text-transform: uppercase;
             letter-spacing: .04em; }
  .stat .v { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .stat .v small { font-size: 12px; font-weight: 400; color: var(--dim); }
  h2 { font-size: 11px; color: var(--dim); text-transform: uppercase;
       letter-spacing: .04em; margin: 0 0 8px; font-weight: 500; }
  #hist { display: flex; align-items: flex-end; gap: 3px; height: 44px; }
  #hist i { flex: 1; min-width: 4px; border-radius: 2px 2px 0 0; opacity: .85; }
  #legend { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px;
            font: 11px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
            color: var(--dim); }
  #legend b { font-weight: 400; }
  #legend span { display: inline-block; width: 8px; height: 8px;
                 border-radius: 2px; margin-right: 5px; }
  footer { display: flex; align-items: center; justify-content: space-between;
           margin-top: 16px; gap: 12px; }
  button {
    font: inherit; font-weight: 500; padding: 7px 13px; cursor: pointer;
    border: 1px solid var(--line); border-radius: 8px;
    background: var(--card); color: var(--fg);
  }
  button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  button:disabled { opacity: .5; cursor: default; }
  #note { font-size: 12px; color: var(--dim); }
</style>

<div class="card">
  <header>
    <h1>container vitals</h1>
    <span class="chip" id="instance">—</span>
  </header>

  <div class="grid">
    <div class="stat">
      <div class="k">uptime</div>
      <div class="v" id="uptime">—</div>
    </div>
    <div class="stat">
      <div class="k">ip</div>
      <div class="v" id="ip" style="font-size:15px">—</div>
    </div>
    <div class="stat">
      <div class="k">memory</div>
      <div class="v"><span id="mem">—</span><small id="memlimit"></small></div>
    </div>
    <div class="stat">
      <div class="k">cpu used</div>
      <div class="v"><span id="cpu">—</span><small id="cpulimit"></small></div>
    </div>
    <div class="stat">
      <div class="k">processes</div>
      <div class="v" id="procs">—</div>
    </div>
    <div class="stat">
      <div class="k">host load 1m</div>
      <div class="v"><span id="load">—</span><small id="cores"></small></div>
      <div class="k" style="text-transform:none;letter-spacing:0">shared, not per container</div>
    </div>
  </div>

  <h2>your refreshes, in order — colour is the container that answered</h2>
  <div id="hist"></div>
  <div id="legend"></div>

  <footer>
    <span id="note">one container so far</span>
    <button id="refresh">refresh</button>
  </footer>
</div>

<script type="module">
// Pinned deliberately. Loading `@latest` would mean the widget silently
// changes under you, and the origin below must match `csp.resourceDomains`.
import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@1.7.5/app-with-deps";

// Every refresh appends here, so the chart is a record of successive
// host-mediated calls rather than a poll of one machine.
const history = [];
const seen = new Map();
const tally = new Map();   // this page's own count, so it always matches your clicks
const PALETTE = ["#2563eb", "#16a34a", "#ea580c", "#9333ea", "#0891b2"];

const $ = (id) => document.getElementById(id);

function colourFor(instance) {
  if (!seen.has(instance)) seen.set(instance, PALETTE[seen.size % PALETTE.length]);
  return seen.get(instance);
}

function render(stats) {
  if (!stats) return;
  const colour = colourFor(stats.instance);
  history.push(stats);
  if (history.length > 40) history.shift();

  $("instance").textContent = stats.instance;
  $("instance").style.color = colour;
  $("uptime").textContent = stats.uptime_s < 90
    ? `${Math.round(stats.uptime_s)}s`
    : `${Math.floor(stats.uptime_s / 60)}m`;
  // Counted here, not read off the server. The server's own counter includes
  // every other client that ever called it, which makes it look wrong the
  // first time a container appears already above 1.
  tally.set(stats.instance, (tally.get(stats.instance) ?? 0) + 1);

  const c = stats.container;
  $("ip").textContent = c.ip ?? "—";
  $("mem").textContent = `${Math.round(c.memory_used_mb)} MiB`;
  $("memlimit").textContent = c.memory_limit_mb
    ? ` / ${Math.round(c.memory_limit_mb)}`
    : " / uncapped";
  $("cpu").textContent = c.cpu_seconds === null ? "—" : `${c.cpu_seconds.toFixed(1)}s`;
  $("cpulimit").textContent = c.cpu_limit ? ` / ${c.cpu_limit} cpu` : " / uncapped";
  $("procs").textContent = c.processes ?? "—";

  $("load").textContent = stats.host.load["1m"].toFixed(2);
  $("cores").textContent = ` / ${stats.host.cpu_count} cpu`;

  // One bar per refresh, in order, coloured by whoever answered it. Equal
  // height: the interesting thing is the sequence of colours, not a magnitude.
  $("hist").replaceChildren(...history.map((h) => {
    const bar = document.createElement("i");
    bar.style.height = "100%";
    bar.style.background = colourFor(h.instance);
    bar.title = h.instance;
    return bar;
  }));

  // textContent, not innerHTML: `id` comes off the wire, and a template that
  // interpolates server data into markup teaches the wrong reflex.
  $("legend").replaceChildren(...[...seen].map(([id, c]) => {
    const el = document.createElement("b");
    const swatch = document.createElement("span");
    swatch.style.background = c;
    el.append(swatch, `${id} · ${tally.get(id) ?? 0}`);
    return el;
  }));

  $("note").textContent = seen.size === 1
    ? "one container so far — keep refreshing"
    : `${seen.size} containers have answered, no session between them`;
}

const app = new App({ name: "fastmcp4-cf monitor", version: "1.0.0" });

// The host pushes the result of the model's own call to `monitor` in here,
// so the widget is populated before anyone touches the button.
app.ontoolresult = (result) => render(result.structuredContent);
app.onerror = console.error;

$("refresh").addEventListener("click", async () => {
  const button = $("refresh");
  button.disabled = true;
  try {
    // NOT a request to the server. The host performs the call on our behalf,
    // and records it in the conversation so the model knows it happened.
    const result = await app.callServerTool({ name: "monitor", arguments: {} });
    // A tool that fails resolves normally with isError — only transport
    // failures reject — so this needs checking, not just a try/catch.
    if (result.isError) throw new Error("tool returned an error");
    render(result.structuredContent);
  } catch (e) {
    console.error(e);
    $("note").textContent = "call failed — see console";
  } finally {
    button.disabled = false;
  }
});

await app.connect();
</script>
"""


# `ui://` infers `text/html;profile=mcp-app`, so no mime_type needed. The host
# builds the iframe's CSP from `resourceDomains` and blocks anything missing —
# drop unpkg.com and the widget's script silently never loads.
@mcp.resource(
    MONITOR_URI,
    meta={
        "ui": app_config_to_meta_dict(
            AppConfig(csp=ResourceCSP(resource_domains=["https://unpkg.com"]))
        )
    },
)
def monitor_ui() -> str:
    """The dashboard `monitor` renders in."""
    return MONITOR_HTML


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
        # 2026-07-28 traffic is sessionless by definition, but a 2025-era
        # client opens with `initialize` and expects an `Mcp-Session-Id` held
        # in one process's memory — which no load balancer can honour. This
        # serves legacy requests sessionlessly too (a fresh transport per
        # request), so BOTH eras fan out across every instance and the Worker
        # needs no era-aware routing. See NOTES.md.
        stateless_http=True,
    )
