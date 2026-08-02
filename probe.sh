#!/usr/bin/env bash
# Talk raw MCP 2026-07-28 to a server. No initialize, no session — each POST is
# complete on its own, carrying its protocol version and client identity in the
# _meta envelope, mirrored into the required headers.
#
#   ./probe.sh http://localhost:8787 server/discover
#   ./probe.sh https://fastmcp4-cf.<you>.workers.dev tools/call whoami
#
# For a multi-round-trip (MRTR) retry, pass the 5th and 6th args: the answers
# to a prior InputRequiredResult, and the request_state it handed back.
#   ./probe.sh $BASE tools/call delete_notes '{"folder":"x"}' \
#       '{"confirm":{"action":"accept","content":{"value":true}}}' "$STATE"
set -euo pipefail

BASE="${1:?base url, e.g. http://localhost:8787}"
METHOD="${2:?method, e.g. tools/call}"
NAME="${3:-}"
ARGS="${4:-}"
RESPONSES="${5:-}"
STATE="${6:-}"
[ -z "$ARGS" ] && ARGS='{}'

# The retry carries the client's answers plus the server's opaque state. Both
# ride in params — there is no session to hold them.
MRTR=""
if [ -n "$RESPONSES" ]; then
  MRTR=",\"inputResponses\":$RESPONSES"
  # Backslashes first, then quotes. The other order double-escapes anything
  # already containing \" and produces invalid JSON.
  [ -n "$STATE" ] && MRTR="$MRTR,\"requestState\":$(printf '%s' "$STATE" | sed 's/\\/\\\\/g; s/"/\\"/g; s/^/"/; s/$/"/')"
fi

META='"_meta":{
        "io.modelcontextprotocol/protocolVersion":"2026-07-28",
        "io.modelcontextprotocol/clientInfo":{"name":"probe.sh","version":"0.1.0"},
        "io.modelcontextprotocol/clientCapabilities":{}
      }'

if [ -n "$NAME" ]; then
  PARAMS="{\"name\":\"$NAME\",\"arguments\":$ARGS,$META$MRTR}"
  NAME_HEADER=(-H "Mcp-Name: $NAME")
else
  PARAMS="{$META$MRTR}"
  NAME_HEADER=()
fi

curl -sS -X POST "${BASE%/}/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H "Mcp-Method: ${METHOD}" \
  ${NAME_HEADER[@]+"${NAME_HEADER[@]}"} \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"${METHOD}\",\"params\":${PARAMS}}"
echo
