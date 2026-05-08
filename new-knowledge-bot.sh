#!/usr/bin/env bash
# new-knowledge-bot.sh — Provision a declawed bot specialized for the
# client-knowledge graph. Mirrors ~/work/declawed/bin/new-bot.sh but:
#   - Adds a read-only mount of ~/work/agent_memory into /work/agent_memory
#   - Installs a knowledge-bot SOUL.md / CLAUDE.md
#   - Skips PSA stub
#   - Confirms channel is registered in channel_routing.json
#
# Usage:
#   new-knowledge-bot.sh <name> <email> <channel_id> <xoxb> <xapp>
#
# Example:
#   new-knowledge-bot.sh knowledge knowledge.bot@cortadogroup.com C0AV329TC8M xoxb-... xapp-...
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ $# -ne 5 ]; then
  echo "Usage: $0 <name> <email> <channel_id> <xoxb_token> <xapp_token>" >&2
  exit 1
fi
NAME="$1"; EMAIL="$2"; CHANNEL="$3"; XOXB="$4"; XAPP="$5"
NAME_TC="$(echo "${NAME:0:1}" | tr '[:lower:]' '[:upper:]')${NAME:1}"

WATCHDOG="$HOME/work/declawed/bin/listener-watchdog.sh"
ROUTING="$HOME/claude-code-slack-channel/routing.json"
ACCESS="$HOME/.claude/channels/slack/access.json"
CONFIGS_DIR="$HOME/work/clawdbot-containers/configs"
WORKSPACE="$HOME/clawd-${NAME}"
KNOWLEDGE_DIR="$HOME/work/agent_memory"
BOT_TEMPLATE="${KNOWLEDGE_DIR}/bot_template"
ROUTING_FILE="${KNOWLEDGE_DIR}/channel_routing.json"

# Sanity: knowledge bot has its own template (NOT jessica)
[[ -d "$BOT_TEMPLATE" ]] || { echo "knowledge-bot template missing at $BOT_TEMPLATE" >&2; exit 1; }
[[ -f "$BOT_TEMPLATE/SOUL.md" ]] || { echo "$BOT_TEMPLATE/SOUL.md missing" >&2; exit 1; }
[[ -f "$BOT_TEMPLATE/CLAUDE.md" ]] || { echo "$BOT_TEMPLATE/CLAUDE.md missing" >&2; exit 1; }
[[ -f "$BOT_TEMPLATE/agent_listener.py" ]] || { echo "$BOT_TEMPLATE/agent_listener.py missing" >&2; exit 1; }

step() { printf "\n\033[36m▶ %s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1"; exit 1; }

# ─── 0. Sanity checks ─────────────────────────────────────────────────────────
[[ -d "$KNOWLEDGE_DIR" ]] || fail "agent_memory dir missing at $KNOWLEDGE_DIR"
[[ -f "$ROUTING_FILE" ]] || fail "channel_routing.json missing — create it first"
python3 - "$ROUTING_FILE" "$CHANNEL" <<'PYEOF'
import json, sys
p, ch = sys.argv[1], sys.argv[2]
d = json.load(open(p))
if ch not in d:
    print(f"ERROR: channel {ch} not in channel_routing.json — add the mapping first", file=sys.stderr)
    sys.exit(1)
PYEOF
ok "channel ${CHANNEL} found in channel_routing.json"

# ─── 1. Pick next port ─────────────────────────────────────────────────────────
step "Picking next port"
AGENTS_CONF="$HOME/work/declawed/agents.conf"
[[ -f "$AGENTS_CONF" ]] || fail "agents.conf not found at $AGENTS_CONF"
LAST_PORT=$(grep -E "^[a-z_]+\|[0-9]+" "$AGENTS_CONF" 2>/dev/null | awk -F'|' '{print $2}' | sort -n | tail -1)
[[ -n "$LAST_PORT" ]] || LAST_PORT=9100
PORT=$((LAST_PORT + 1))
ok "port=$PORT (last in agents.conf was $LAST_PORT)"

# ─── 2. Workspace ──────────────────────────────────────────────────────────────
step "Creating workspace at $WORKSPACE"
mkdir -p "$WORKSPACE"/{credentials,skills,tmp,output,.claude}

# Copy the knowledge-bot template (NOT jessica's)
cp "$BOT_TEMPLATE/SOUL.md"          "$WORKSPACE/SOUL.md"
cp "$BOT_TEMPLATE/CLAUDE.md"        "$WORKSPACE/CLAUDE.md"
cp "$BOT_TEMPLATE/agent_listener.py" "$WORKSPACE/agent_listener.py"

# Inherit the same .claude/settings.json as the rest of the fleet (deny-hooks etc.)
[[ -f "$HOME/clawd-jessica/.claude/settings.json" ]] && \
  cp "$HOME/clawd-jessica/.claude/settings.json" "$WORKSPACE/.claude/settings.json" 2>/dev/null || true

cat > "$WORKSPACE/.env" <<EOF
# ${NAME_TC}Bot — client knowledge graph access
KBQ_DB=/work/agent_memory/agent_memory.db
KBQ_CHANNEL_ROUTING=/work/agent_memory/channel_routing.json
KBQ_QUERY_GRAPH=/work/agent_memory/query_graph.py
KBQ_RENDER_DOSSIER=/work/agent_memory/render_dossier.py
EOF

echo "$EMAIL" > "$WORKSPACE/credentials/owner_email.txt"
ok "workspace populated from knowledge-bot template"

# ─── 3. Clawdbot config ────────────────────────────────────────────────────────
# We still clone the clawdbot.json shell from jessica — it's just a routing
# config (channel allowlist + binding). The bot's IDENTITY comes from
# our knowledge-bot SOUL.md/CLAUDE.md, not from anything in this file.
step "Cloning clawdbot config shell from jessica"
cp -r "$CONFIGS_DIR/jessica" "$CONFIGS_DIR/$NAME"
cd "$CONFIGS_DIR/$NAME"
rm -f clawdbot.json.bak* 2>/dev/null || true
rm -rf sessions/* logs/* 2>/dev/null || true
TEMPLATE_CHANNEL=$(python3 -c "import json,sys; d=json.load(open('clawdbot.json')); print(next((b['match']['peer']['id'] for b in d.get('bindings',[]) if b.get('agentId')=='jessica'), ''))")
sed -i "" "s/${TEMPLATE_CHANNEL}/${CHANNEL}/g" clawdbot.json
sed -i "" "s/jessica/${NAME}/g; s/Jessica/${NAME_TC}/g; s/JessicaBot/${NAME_TC}Bot/g" clawdbot.json
ok "configs/$NAME ready (routing shell only — identity from bot_template/)"

# ─── 4. Container ──────────────────────────────────────────────────────────────
step "Starting OrbStack container declawed-$NAME"
if docker ps -a --format '{{.Names}}' | grep -qx "declawed-$NAME"; then
  docker rm -f "declawed-$NAME" >/dev/null
fi
docker run -d --name "declawed-$NAME" --network host --restart unless-stopped \
  -v "$CONFIGS_DIR/$NAME:/home/agent/.clawdbot" \
  -v "$HOME/.clawdbot/skills:/home/agent/.clawdbot/skills" \
  -v "$HOME/.clawdbot/skills:/skills" \
  -v "$WORKSPACE:/workspace" \
  -v "$HOME/clawd/memory/box_files.db:/data/box_files.db:ro" \
  -v "${KNOWLEDGE_DIR}:/work/agent_memory:ro" \
  --user agent --entrypoint bash clawdbot-agent -c "sleep infinity" >/dev/null
sleep 2
docker ps --filter "name=declawed-$NAME" --format '{{.Status}}' | grep -q "Up" || fail "container did not start"
ok "container Up"

# ─── 5. Push Claude OAuth creds ────────────────────────────────────────────────
step "Pushing Claude OAuth creds"
docker exec "declawed-$NAME" mkdir -p /home/agent/.claude
docker cp "$HOME/.claude/.credentials.json" "declawed-$NAME:/home/agent/.claude/.credentials.json"
docker exec --user root "declawed-$NAME" chown -R agent:agent /home/agent/.claude
ok "creds pushed"

# ─── 6. Write /tmp/mcp.json ────────────────────────────────────────────────────
step "Writing /tmp/mcp.json"
echo "{\"mcpServers\":{\"slack\":{\"type\":\"http\",\"url\":\"http://127.0.0.1:8201/mcp?channel=${CHANNEL}\"},\"knowledge\":{\"type\":\"sse\",\"url\":\"http://127.0.0.1:8200/sse\"}}}" \
  | docker exec -i "declawed-$NAME" tee /tmp/mcp.json >/dev/null
ok "/tmp/mcp.json written"

# ─── 7. agents.conf ────────────────────────────────────────────────────────────
step "Adding to agents.conf"
AGENTS_CONF="$HOME/work/declawed/agents.conf"
if ! grep -q "^${NAME}|" "$AGENTS_CONF" 2>/dev/null; then
  echo "${NAME}|${PORT}|${CHANNEL}|${XOXB}" >> "$AGENTS_CONF"
  chmod 600 "$AGENTS_CONF"
  ok "appended to agents.conf"
else
  ok "already in agents.conf"
fi

# ─── 8. routing.json (gateway) ─────────────────────────────────────────────────
step "Adding to gateway routing.json"
python3 - <<EOF
import json, shutil
p = "$ROUTING"
d = json.load(open(p))
shutil.copy(p, p + ".bak")
d["$CHANNEL"] = {
    "botToken": "$XOXB",
    "appToken": "$XAPP",
    "name": "$NAME",
    "agentUrl": "http://127.0.0.1:$PORT",
}
json.dump(d, open(p, "w"), indent=2)
print("  ok routing.json now has", len(d), "entries")
EOF

# ─── 9. access.json ────────────────────────────────────────────────────────────
step "Adding to gateway access.json"
python3 - <<EOF
import json, os, shutil
p = "$ACCESS"
d = json.load(open(p))
shutil.copy(p, p + ".bak")
d.setdefault("channels", {})["$CHANNEL"] = {"requireMention": True, "allowFrom": []}
json.dump(d, open(p, "w"), indent=2)
os.chmod(p, 0o600)
print("  ok access.json now has", len(d["channels"]), "channels")
EOF

# ─── 10. Restart gateway ───────────────────────────────────────────────────────
step "Restarting gateway"
launchctl kickstart -k "gui/$(id -u)/com.claude-code-slack-channel"
sleep 3
ok "gateway restarted"

# ─── 11. Start listener ────────────────────────────────────────────────────────
step "Starting listener"
docker exec -d "declawed-$NAME" bash -c \
  "cd /workspace && AGENT_CHANNEL=${CHANNEL} AGENT_PORT=${PORT} MCP_URL=http://127.0.0.1:8201/mcp SLACK_BOT_TOKEN=${XOXB} python3 /workspace/agent_listener.py > /tmp/agent.log 2>&1"
sleep 4
docker exec "declawed-$NAME" cat /tmp/agent.log | tail -3
ok "listener started"

# ─── 12. Validate ──────────────────────────────────────────────────────────────
step "Validating"
"$HOME/work/declawed/bin/validate-bot.sh" "$NAME" || true

echo
echo "🧠 ${NAME_TC}Bot is live on port $PORT in channel $CHANNEL"
echo "   workspace: $WORKSPACE"
echo "   client_slug: $(python3 -c "import json; print(json.load(open('${ROUTING_FILE}'))['${CHANNEL}']['client_slug'])")"
echo "   email:     $EMAIL"
