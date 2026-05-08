#!/usr/bin/env bash
# run-knowledge-bot.sh — Launch the client_knowledge_bot container with the
# agent_memory.db read-only mount + channel_routing.json + the kbq wrapper.
#
# Wraps the existing run-agent.sh by post-mounting; if you want a permanent
# fix add the mounts directly to ~/work/clawdbot-containers/run-agent.sh.

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

AGENT_ID="client_knowledge_bot"
SCRIPT_DIR="$HOME/work/clawdbot-containers"
AGENT_WORKSPACE="$HOME/clawd-${AGENT_ID}"
SKILLS_DIR="$HOME/.clawdbot/skills"
AGENT_CONFIG_DIR="${SCRIPT_DIR}/configs/${AGENT_ID}"
AGENT_CONFIG_FILE="${AGENT_CONFIG_DIR}/clawdbot.json"
CONTAINER_NAME="clawdbot-${AGENT_ID}"
CONTAINER_PORT="${1:-18820}"
IMAGE_NAME="clawdbot-agent"

# Knowledge-specific mounts
KNOWLEDGE_DIR="$HOME/work/agent_memory"
DB_FILE="${KNOWLEDGE_DIR}/agent_memory.db"
ROUTING_FILE="${KNOWLEDGE_DIR}/channel_routing.json"

# Validate
[[ -d "$AGENT_WORKSPACE" ]] || { echo "Workspace missing: $AGENT_WORKSPACE — create it first"; exit 1; }
[[ -d "$AGENT_CONFIG_DIR" ]] || { echo "Config dir missing: $AGENT_CONFIG_DIR — create it first"; exit 1; }
[[ -f "$AGENT_CONFIG_FILE" ]] || { echo "Config file missing: $AGENT_CONFIG_FILE — create it first"; exit 1; }
[[ -f "$DB_FILE" ]] || { echo "agent_memory.db missing: $DB_FILE"; exit 1; }
[[ -f "$ROUTING_FILE" ]] || { echo "channel_routing.json missing: $ROUTING_FILE"; exit 1; }

# Refresh auth profiles into the agent's config
AGENT_AUTH_DIR="${AGENT_CONFIG_DIR}/agents/${AGENT_ID}/agent"
mkdir -p "$AGENT_AUTH_DIR"
cp "$HOME/.clawdbot/agents/main/agent/auth-profiles.json" "${AGENT_AUTH_DIR}/auth-profiles.json"

OAUTH_TOKEN=$(python3 -c "
import json
with open('$HOME/.clawdbot/agents/main/agent/auth-profiles.json') as f:
    d = json.load(f)
for pname in ['anthropic:claude-cli', 'anthropic:manual']:
    p = d.get('profiles', {}).get(pname, {})
    t = p.get('accessToken') or p.get('access') or p.get('token', '')
    if t:
        print(t); break
")
[[ -n "$OAUTH_TOKEN" ]] || { echo "Could not extract OAuth token"; exit 1; }

# Stop existing
docker stop "$CONTAINER_NAME" 2>/dev/null || true
docker rm   "$CONTAINER_NAME" 2>/dev/null || true

# Build image if missing (uses base clawdbot-agent image; it should already exist if other bots run)
if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "==> Building image '${IMAGE_NAME}'..."
    docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"
fi

echo "==> Starting ${CONTAINER_NAME} on port ${CONTAINER_PORT}..."

docker run -d \
    --name "$CONTAINER_NAME" \
    --hostname "$CONTAINER_NAME" \
    --network host \
    -v "${AGENT_WORKSPACE}:/workspace" \
    -v "${SKILLS_DIR}:/skills:ro" \
    -v "${SKILLS_DIR}:/home/agent/.clawdbot/skills:ro" \
    -v "${AGENT_CONFIG_DIR}:/home/agent/.clawdbot" \
    -v "${AGENT_CONFIG_FILE}:/home/agent/.config/clawdbot/clawdbot.json" \
    -v "$HOME/.clawdbot/agents/main/agent/auth-profiles.json:/home/agent/.clawdbot/agents/${AGENT_ID}/agent/auth-profiles.json" \
    -v "$HOME/clawd/memory/box_files.db:/Users/david/clawd/memory/box_files.db:ro" \
    -v "${SCRIPT_DIR}/entrypoint.sh:/entrypoint.sh:ro" \
    -v "${SCRIPT_DIR}/patches/anthropic-sdk-client.js:/usr/local/lib/node_modules/clawdbot/node_modules/@anthropic-ai/sdk/client.js:ro" \
    -v "${SCRIPT_DIR}/patches/pi-ai-anthropic.js:/usr/local/lib/node_modules/clawdbot/node_modules/@mariozechner/pi-ai/dist/providers/anthropic.js:ro" \
    -v "${SCRIPT_DIR}/patches/channel.ts:/usr/local/lib/node_modules/clawdbot/extensions/slack/src/channel.ts:ro" \
    -v "$HOME/.claude/container-settings.json:/home/agent/.claude/settings.json:ro" \
    -v "${KNOWLEDGE_DIR}:/work/agent_memory:ro" \
    -e "ANTHROPIC_OAUTH_TOKEN=${OAUTH_TOKEN}" \
    -e "CLAWDBOT_GATEWAY_TOKEN=${CLAWDBOT_GATEWAY_TOKEN:-localtest}" \
    -e "HOME=/home/agent" \
    -e "CLAWDBOT_SERVICE_KIND=gateway" \
    -e "NODE_OPTIONS=--max-old-space-size=768" \
    --memory 1g \
    --entrypoint /entrypoint.sh \
    "$IMAGE_NAME"

echo "==> ${CONTAINER_NAME} started"
echo "    Logs: docker logs -f ${CONTAINER_NAME}"
echo "    Stop: docker stop ${CONTAINER_NAME}"
