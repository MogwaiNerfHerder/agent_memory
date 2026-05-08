#!/usr/bin/env python3
"""
KnowledgeBot listener — runs inside the declawed-knowledge container.

Differences from the standard fleet listener:

  - Each @-mention spawns a FRESH Claude session (no per-thread continuity).
    State lives in the Slack thread; the listener pre-fetches the full thread
    on every event and hands it to Claude as initial context.

  - Channel → client_slug isolation is hardened: $CLAWDBOT_CHANNEL_ID is set
    in the subprocess env directly from the Slack event payload (NOT from any
    LLM-controlled source). The kbq wrapper looks it up in
    /work/agent_memory/channel_routing.json and refuses if absent.

  - No personal memory; no calendar; no PSA. The bot is strictly read-only
    over the client knowledge graph.
"""

import asyncio
import json
import os
import re
import sys
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

LISTEN_PORT = int(os.environ.get("AGENT_PORT", "9100"))
CHANNEL_ID  = os.environ.get("AGENT_CHANNEL", "")
MCP_URL     = os.environ.get("MCP_URL", "http://127.0.0.1:8201/mcp")
BOT_TOKEN   = os.environ.get("SLACK_BOT_TOKEN", "")

# Pre-fetched thread length cap (turns)
MAX_THREAD_TURNS = int(os.environ.get("KB_MAX_THREAD_TURNS", "40"))

# Debounce window: when a Slack event arrives, wait this long before firing
# Claude. Subsequent events in the same thread cancel-and-replace the in-flight
# task so the fresh task starts a new debounce. Result: a burst of messages in
# one thread coalesces into ONE Claude session that sees all of them.
DEBOUNCE_SEC = float(os.environ.get("KB_DEBOUNCE_SEC", "3.5"))

LISTENER_VERSION = "knowledge-2026-05-07.v1"

# Bootstrap MCP config inside the container.
# IMPORTANT: NO slack MCP server. The bot only emits text — the LISTENER posts
# to Slack threaded. If we expose the slack MCP, Claude will call its `reply`
# tool without thread_ts and dump replies into the main channel.
def _bootstrap_mcp_config():
    cfg = {"mcpServers": {
        "knowledge": {"type": "sse",  "url": "http://127.0.0.1:8200/sse"},
    }}
    try:
        with open("/tmp/mcp.json", "w") as fh:
            json.dump(cfg, fh)
    except Exception as e:
        print(f"[knowledge] WARN: could not write /tmp/mcp.json: {e}", flush=True)
_bootstrap_mcp_config()

# Make the shared pylib importable in subprocesses
_existing_pp = os.environ.get("PYTHONPATH", "")
_shared = "/skills/.shared-pylib"
if _shared not in _existing_pp.split(":"):
    os.environ["PYTHONPATH"] = (_shared + ":" + _existing_pp).rstrip(":")

# Per-thread coalescence:
#
#   THREAD_LATEST[thread_ts]  = the most recent event payload for the thread.
#   THREAD_TASKS[thread_ts]   = the worker coroutine handle for the thread.
#
# Pattern: every incoming POST overwrites THREAD_LATEST for its thread (no
# queue — Slack already preserves ordering, and `_fetch_thread` re-reads the
# whole thread fresh each session). If no worker is running for the thread,
# spawn one. The worker debounces, drains, and runs ONE Claude session that
# sees the full thread context. If new events landed during processing, the
# worker loops once more — but never spawns multiple parallel sessions.
#
# Outcome the user cares about: 5 messages while we're rate-limited do NOT
# fire 5 Claude sessions when we recover. They fire ONE session, which sees
# every still-existing message in the thread (deletions drop out naturally
# because `_fetch_thread` reads live Slack state).
THREAD_LATEST: dict = {}     # thread_ts -> latest event payload
THREAD_TASKS:  dict = {}     # thread_ts -> asyncio.Future / Task

# Main asyncio loop — set by main() before HTTP thread starts. The HTTP handler
# runs in a different thread and needs an explicit reference to schedule work.
MAIN_LOOP: "asyncio.AbstractEventLoop | None" = None

# Slack helpers ---------------------------------------------------------------

def _slack_post(channel: str, thread_ts: str, text: str) -> str:
    """Post a status/reply in the given thread; return its ts."""
    try:
        data = json.dumps({
            "channel": channel,
            "thread_ts": thread_ts,
            "text": text,
        }).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage", data=data,
            headers={"Authorization": f"Bearer {BOT_TOKEN}", "Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req).read())
        return resp.get("ts", "")
    except Exception as e:
        print(f"[knowledge] _slack_post error: {e}", flush=True)
        return ""


def _slack_update(channel: str, ts: str, text: str):
    """Update an existing message (used for live-streaming the status box)."""
    try:
        data = json.dumps({"channel": channel, "ts": ts, "text": text}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.update", data=data,
            headers={"Authorization": f"Bearer {BOT_TOKEN}", "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req).read()
    except Exception as e:
        print(f"[knowledge] _slack_update error: {e}", flush=True)


def _slack_delete(channel: str, ts: str):
    try:
        data = json.dumps({"channel": channel, "ts": ts}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.delete", data=data,
            headers={"Authorization": f"Bearer {BOT_TOKEN}", "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req).read()
    except Exception:
        pass


def _describe_tool(tool_name: str, content_str: str) -> str:
    """Map a Claude tool-use to a short status line (Slack mrkdwn)."""
    if tool_name == "Bash":
        m = re.search(r"'command':\s*'([^']{0,160})", content_str)
        cmd = (m.group(1) if m else "").strip()
        # Special-case our knowledge wrapper so users see the actual sub-query.
        kbq = re.search(r"kbq\.py\s+(\w+)(?:\s+\"([^\"]{0,60})\")?", cmd)
        if kbq:
            sub = kbq.group(1)
            arg = kbq.group(2) or ""
            label = f"querying graph: *{sub}*" + (f" `{arg}`" if arg else "")
            return f":mag: {label}"
        return f":computer: running `{cmd[:80]}`"
    if tool_name == "Read":
        m = re.search(r"'file_path':\s*'([^']+)'", content_str)
        f = m.group(1).split("/")[-1] if m else "file"
        return f":page_facing_up: reading `{f}`"
    if tool_name in ("Grep", "Glob"):
        return ":mag: searching files"
    if tool_name in ("Write", "Edit"):
        return ":pencil2: writing file"
    if tool_name == "TodoWrite":
        return ":memo: planning"
    if tool_name == "ToolSearch":
        return ""  # noisy; suppress
    return f":wrench: {tool_name}"


def _slack_react(channel: str, ts: str, name: str, remove: bool = False):
    try:
        data = json.dumps({"channel": channel, "timestamp": ts, "name": name}).encode()
        endpoint = "reactions.remove" if remove else "reactions.add"
        req = urllib.request.Request(
            f"https://slack.com/api/{endpoint}", data=data,
            headers={"Authorization": f"Bearer {BOT_TOKEN}", "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req).read()
    except Exception:
        pass


def _user_display_name(user_id: str) -> str:
    """Best-effort resolve user_id -> display name (cached)."""
    if not user_id:
        return ""
    try:
        req = urllib.request.Request(
            f"https://slack.com/api/users.info?user={user_id}",
            headers={"Authorization": f"Bearer {BOT_TOKEN}"},
        )
        resp = json.loads(urllib.request.urlopen(req).read())
        u = resp.get("user", {})
        return u.get("real_name") or u.get("name") or user_id
    except Exception:
        return user_id


def _fetch_thread(channel: str, thread_ts: str) -> list:
    """Fetch the FULL thread (capped at MAX_THREAD_TURNS) as a list of dicts:
       [{ts, user, name, text}]"""
    out = []
    cursor = None
    fetched = 0
    while fetched < MAX_THREAD_TURNS:
        url = (f"https://slack.com/api/conversations.replies"
               f"?channel={channel}&ts={thread_ts}&limit={MAX_THREAD_TURNS}")
        if cursor:
            url += f"&cursor={cursor}"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {BOT_TOKEN}"})
            resp = json.loads(urllib.request.urlopen(req).read())
        except Exception as e:
            print(f"[knowledge] thread fetch error: {e}", flush=True)
            break
        msgs = resp.get("messages") or []
        for m in msgs:
            out.append({
                "ts":   m.get("ts", ""),
                "user": m.get("user", "") or m.get("bot_id", ""),
                "name": _user_display_name(m.get("user", "")),
                "text": m.get("text", ""),
            })
        fetched = len(out)
        if not resp.get("has_more"):
            break
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return out[:MAX_THREAD_TURNS]


def _format_thread_context(turns: list, current_ts: str) -> str:
    """Render the thread as a Markdown-style transcript for the LLM."""
    lines = ["## Thread context (most recent message is the @-mention asking you to respond)\n"]
    for t in turns:
        marker = "👉 " if t["ts"] == current_ts else "   "
        text = t["text"]
        # Strip Slack <@U...> mentions to be readable
        text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
        lines.append(f"{marker}**{t['name']}** _{t['ts']}_: {text}")
    return "\n".join(lines)


# Claude Agent SDK invocation -------------------------------------------------

ClaudeSDKClient = None
ClaudeAgentOptions = None


async def _thread_worker(thread_ts: str):
    """Per-thread coalescing worker.

    Sleeps DEBOUNCE_SEC, then takes the latest event for the thread and runs
    ONE Claude session against the live thread state. If new events arrived
    while we were processing, drain again — but never run multiple sessions
    in parallel for the same thread.
    """
    try:
        # Initial debounce: lets a burst settle before we fire.
        try:
            await asyncio.sleep(DEBOUNCE_SEC)
        except asyncio.CancelledError:
            return

        while True:
            evt = THREAD_LATEST.pop(thread_ts, None)
            if not evt:
                break
            try:
                await _process_event(evt)
            except Exception as e:
                print(f"[knowledge] thread_worker {thread_ts} error: {e}", flush=True)
            # If new events arrived while we were processing, the next loop
            # iteration will pick them up and run ONE more session that sees
            # the now-current thread state.
    finally:
        # Race: if a final POST arrived after our pop but before this finally,
        # spawn a follow-up worker so it doesn't get stranded.
        leftover = THREAD_LATEST.get(thread_ts)
        THREAD_TASKS.pop(thread_ts, None)
        if leftover and MAIN_LOOP is not None:
            THREAD_TASKS[thread_ts] = asyncio.run_coroutine_threadsafe(
                _thread_worker(thread_ts), MAIN_LOOP
            )


async def _process_event(event: dict):
    """Handle one Slack event end-to-end: fetch thread, spawn fresh Claude
    session, post reply."""
    # Gateway sends `chat_id`; fall back to legacy `channel` and env.
    channel = event.get("chat_id") or event.get("channel") or CHANNEL_ID
    # message_ts is the ts the gateway reacted on (the user's message itself).
    # thread_ts is the thread root; if absent (root-of-channel post) use ts.
    message_ts = event.get("ts") or event.get("message_id") or ""
    thread_ts = event.get("thread_ts") or message_ts or ""
    user_text = event.get("text") or ""
    user_id   = event.get("user_id") or event.get("user") or ""

    if not channel or not message_ts:
        print(f"[knowledge] event missing channel/message_ts: {event}", flush=True)
        return

    print(f"[knowledge] _process_event start chan={channel} ts={message_ts} thread={thread_ts}", flush=True)

    # Pre-fetch the FULL Slack thread. This is the live state — deleted
    # messages won't appear, so the consolidated session naturally ignores
    # them.
    turns = _fetch_thread(channel, thread_ts)

    # Verify the triggering message still exists. If the user deleted or it's
    # otherwise gone, skip — don't waste a Claude session answering air.
    if not any(t.get("ts") == message_ts for t in turns):
        print(f"[knowledge] triggering message {message_ts} no longer in thread (deleted?) — skip", flush=True)
        _slack_react(channel, message_ts, "eyes", remove=True)
        return

    thread_context = _format_thread_context(turns, message_ts)

    # Build the initial prompt — what Claude sees as its first user turn.
    # The system prompt comes from SOUL.md/CLAUDE.md (loaded by Claude Code).
    initial_prompt = (
        f"{thread_context}\n\n"
        f"---\n\n"
        f"The LAST message above is the request you need to address. It may be "
        f"a quick lookup, a complex multi-part synthesis, or a meta-question "
        f"about the thread. Take whatever depth the request warrants — there's "
        f"no per-mention budget. Use as many `kbq.py` calls as you need. "
        f"Synthesize freely from the thread + the graph; cite sources. Reply in "
        f"Slack mrkdwn (single-asterisk *bold*, NEVER **double**). If the "
        f"answer would exceed ~2000 chars, upload a .docx summary and post a "
        f"tight headline message linking to it."
    )

    # ENV — channel_id is the load-bearing isolation; comes from the Slack
    # event itself, NOT from anything the LLM could control.
    env = dict(os.environ)
    env["CLAWDBOT_CHANNEL_ID"] = channel
    env["CLAWDBOT_SLACK_THREAD_TS"] = thread_ts
    env["CLAWDBOT_SLACK_USER_ID"] = user_id

    # Spawn Claude (fresh session every time — no SESSIONS dict)
    opts = ClaudeAgentOptions(
        cwd="/workspace",
        permission_mode="bypassPermissions",
        env=env,
    )

    response_chunks: list = []
    tool_steps: list = []     # ordered list of human-readable tool steps
    status_msg_ts = ""        # ts of our live-updating status message in-thread
    start_time = time.time()

    def _render_status() -> str:
        recent = [s for s in tool_steps if s][-6:]
        elapsed = int(time.time() - start_time)
        elapsed_str = f"{elapsed // 60}m{elapsed % 60:02d}s" if elapsed >= 60 else f"{elapsed}s"
        body = "\n".join(recent) if recent else ":hourglass_flowing_sand: thinking..."
        return f"{body}\n_({elapsed_str} elapsed)_"

    try:
        async with ClaudeSDKClient(options=opts) as client:
            await client.query(initial_prompt)
            async for msg in client.receive_response():
                # Collect any text blocks for the final reply
                for blk in getattr(msg, "content", []) or []:
                    txt = getattr(blk, "text", None)
                    if txt:
                        response_chunks.append(txt)

                # Detect tool-use events and update the in-thread status box
                content_str = str(getattr(msg, "content", ""))
                if "ToolUseBlock" in content_str:
                    tm = re.search(r"name='([^']+)'", content_str)
                    if tm:
                        step = _describe_tool(tm.group(1), content_str)
                        if step and (not tool_steps or tool_steps[-1] != step):
                            tool_steps.append(step)
                            text = _render_status()
                            if not status_msg_ts:
                                status_msg_ts = _slack_post(channel, thread_ts, text)
                            else:
                                _slack_update(channel, status_msg_ts, text)
    except Exception as e:
        print(f"[knowledge] Claude session error: {e}", flush=True)
        _slack_post(channel, thread_ts, f"Sorry — listener error: {e}")
        _slack_react(channel, message_ts, "eyes", remove=True)
        _slack_react(channel, message_ts, "x")
        if status_msg_ts:
            _slack_delete(channel, status_msg_ts)
        return

    final_body = "".join(response_chunks).strip()
    if not final_body:
        final_body = "(no response generated)"

    # Drop the live status box (it's served its purpose) and post the answer.
    if status_msg_ts:
        _slack_delete(channel, status_msg_ts)

    _slack_post(channel, thread_ts, final_body)
    _slack_react(channel, message_ts, "eyes", remove=True)
    _slack_react(channel, message_ts, "white_check_mark")


# HTTP listener ---------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):  # quiet default access log
        return

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode())
        except Exception as e:
            self.send_response(400); self.end_headers()
            self.wfile.write(f"bad request: {e}".encode())
            return

        # 200 immediately so the gateway doesn't retry
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

        # Schedule on the main asyncio loop (HTTP handler runs in its own thread,
        # so we MUST use the explicit MAIN_LOOP reference set by main()).
        thread_ts = body.get("thread_ts") or body.get("ts") or ""
        if MAIN_LOOP is None:
            print("[knowledge] ERR: MAIN_LOOP not set; dropping event", flush=True)
            return
        # Stash latest event payload for this thread; spawn a worker if one
        # isn't already running. The worker will debounce, drain, and call
        # _process_event ONCE per quiescent burst.
        THREAD_LATEST[thread_ts] = body
        existing = THREAD_TASKS.get(thread_ts)
        if existing and not existing.done():
            return  # worker is already going to pick up the new latest event
        THREAD_TASKS[thread_ts] = asyncio.run_coroutine_threadsafe(
            _thread_worker(thread_ts), MAIN_LOOP
        )


def _http_thread(loop):
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"[knowledge] LISTENER_VERSION={LISTENER_VERSION} on :{LISTEN_PORT} channel={CHANNEL_ID}", flush=True)
    server.serve_forever()


async def main():
    global ClaudeSDKClient, ClaudeAgentOptions, MAIN_LOOP
    from claude_agent_sdk import ClaudeSDKClient as _C, ClaudeAgentOptions as _O
    ClaudeSDKClient = _C
    ClaudeAgentOptions = _O

    MAIN_LOOP = asyncio.get_running_loop()
    Thread(target=_http_thread, args=(MAIN_LOOP,), daemon=True).start()
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
