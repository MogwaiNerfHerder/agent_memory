#!/usr/bin/env python3
"""
kbq.py - Knowledge-Bot Query wrapper. The ONLY entry point a bot uses to query
the client knowledge graph from a Slack channel.

Hard isolation contract:
  1. Channel ID comes from $CLAWDBOT_CHANNEL_ID (set by clawdbot when invoking the bot).
  2. channel_routing.json is the SINGLE source of truth for channel → client_slug.
  3. If channel_id is missing, unknown, or routing.json is unreadable: HARD REFUSE.
  4. The LLM-facing tools NEVER take a `client_slug` argument. The slug is always
     the one resolved from channel_id. Bot has no way to override.
  5. Every query writes a row to bot_query_log with the resolved client_slug,
     channel, and slack user for audit.

LLM-facing CLI surface (intentionally minimal):
    kbq.py dossier <entity>
    kbq.py notes <entity> [--type TYPE] [--limit N]
    kbq.py find <query>
    kbq.py timeline [--from DATE] [--to DATE] [--type TYPE]
    kbq.py recent [--since DATE] [--limit N]
    kbq.py drifts
    kbq.py stakeholders <project>
    kbq.py project <project>

Sensitivity tier defaults to whatever channel_routing.json says for this channel.
There is no flag to elevate it from the bot side.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_DB = os.environ.get("KBQ_DB", "/work/agent_memory/agent_memory.db")
DEFAULT_ROUTING = os.environ.get(
    "KBQ_CHANNEL_ROUTING", "/work/agent_memory/channel_routing.json"
)
DEFAULT_QUERY_GRAPH = os.environ.get(
    "KBQ_QUERY_GRAPH", "/work/agent_memory/query_graph.py"
)
DEFAULT_RENDER_DOSSIER = os.environ.get(
    "KBQ_RENDER_DOSSIER", "/work/agent_memory/render_dossier.py"
)


def die(msg: str, exit_code: int = 2):
    print(f"REFUSED: {msg}", file=sys.stderr)
    sys.exit(exit_code)


def load_routing(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        die(f"channel_routing.json not found at {path}")
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        die(f"channel_routing.json malformed: {e}")


def resolve_channel(routing: dict, channel_id: str) -> dict:
    if not channel_id:
        die("CLAWDBOT_CHANNEL_ID env var is empty or unset; cannot resolve client tenant.")
    rec = routing.get(channel_id)
    if not rec:
        die(f"channel_id {channel_id!r} is not in channel_routing.json. "
            "Add an entry there before this bot can serve this channel.")
    if "client_slug" not in rec:
        die(f"channel_routing.json[{channel_id!r}] has no client_slug.")
    return rec


def log_query(db_path, channel_id, client_slug, tool_name, tool_args,
               result_summary, result_count, duration_ms):
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            INSERT INTO bot_query_log
                (channel_id, client_slug, slack_user_id, slack_thread_ts,
                 tool_name, tool_args, result_summary, result_count, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            channel_id, client_slug,
            os.environ.get("CLAWDBOT_SLACK_USER_ID"),
            os.environ.get("CLAWDBOT_SLACK_THREAD_TS"),
            tool_name, json.dumps(tool_args, default=str),
            result_summary, result_count, duration_ms,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        # Don't fail the user-facing query if logging breaks; just complain to stderr.
        print(f"warning: bot_query_log write failed: {e}", file=sys.stderr)


def run_underlying(cmd_args, db_path, channel_id, client_slug, tool_name, tool_args):
    """Invoke an underlying Python tool, log, print stdout to stdout."""
    started = time.time()
    proc = subprocess.run(cmd_args, capture_output=True, text=True)
    duration_ms = int((time.time() - started) * 1000)
    out = proc.stdout
    err = proc.stderr
    rc = proc.returncode

    # Heuristic result_count: lines starting with "  " (typical CLI output indent)
    result_count = sum(1 for ln in out.splitlines() if ln.startswith("  "))
    summary = out.splitlines()[0][:200] if out else (err[:200] if err else "")
    log_query(db_path, channel_id, client_slug, tool_name, tool_args,
              summary, result_count, duration_ms)

    if err and rc != 0:
        print(err, file=sys.stderr)
        sys.exit(rc)
    print(out, end="" if out.endswith("\n") else "\n")
    return rc


def main():
    parser = argparse.ArgumentParser(
        description="Knowledge-Bot Query — channel-scoped read-only access to the client knowledge graph."
    )
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--routing", default=DEFAULT_ROUTING)
    sub = parser.add_subparsers(dest="cmd", required=True)

    # NOTE: NONE of these subcommands accept --client-slug. That is intentional.
    sd = sub.add_parser("dossier"); sd.add_argument("entity"); sd.add_argument("--all", action="store_true")
    sn = sub.add_parser("notes"); sn.add_argument("entity"); sn.add_argument("--type"); sn.add_argument("--limit", type=int, default=20)
    sf = sub.add_parser("find"); sf.add_argument("query")
    st = sub.add_parser("timeline"); st.add_argument("--from", dest="frm"); st.add_argument("--to"); st.add_argument("--type"); st.add_argument("--entity")
    sr = sub.add_parser("recent"); sr.add_argument("--since"); sr.add_argument("--limit", type=int, default=20)
    sub.add_parser("drifts")
    sk = sub.add_parser("stakeholders"); sk.add_argument("project")
    sp = sub.add_parser("project"); sp.add_argument("project")
    sub.add_parser("stats")

    args = parser.parse_args()

    # ---- HARD ISOLATION GATE ------------------------------------------------
    channel_id = os.environ.get("CLAWDBOT_CHANNEL_ID", "").strip()
    routing = load_routing(args.routing)
    rec = resolve_channel(routing, channel_id)
    client_slug = rec["client_slug"]
    max_sensitivity = rec.get("max_sensitivity", "sensitive")
    # ------------------------------------------------------------------------

    # Show users which tenant they're talking to (channel-scoped, not LLM-controllable)
    print(f"[knowledge-bot scope] client={client_slug} sensitivity≤{max_sensitivity}", file=sys.stderr)

    qg = [sys.executable, DEFAULT_QUERY_GRAPH, "--db", args.db]
    rd = [sys.executable, DEFAULT_RENDER_DOSSIER, "--db", args.db,
          "--client-slug", client_slug, "--max-sensitivity", max_sensitivity]

    if args.cmd == "dossier":
        cmd = rd + ["--entity", args.entity]
        if args.all:
            cmd.append("--all")
        run_underlying(cmd, args.db, channel_id, client_slug, "dossier",
                        {"entity": args.entity, "all": args.all})

    elif args.cmd == "notes":
        cmd = qg + ["notes", client_slug, args.entity]
        if args.type:
            cmd += ["--type", args.type]
        cmd += ["--limit", str(args.limit)]
        run_underlying(cmd, args.db, channel_id, client_slug, "notes",
                        {"entity": args.entity, "type": args.type, "limit": args.limit})

    elif args.cmd == "find":
        cmd = qg + ["resolve", client_slug, args.query]
        run_underlying(cmd, args.db, channel_id, client_slug, "find",
                        {"query": args.query})

    elif args.cmd == "timeline":
        cmd = qg + ["timeline", client_slug]
        if args.frm:    cmd += ["--from", args.frm]
        if args.to:     cmd += ["--to", args.to]
        if args.type:   cmd += ["--type", args.type]
        if args.entity: cmd += ["--entity", args.entity]
        run_underlying(cmd, args.db, channel_id, client_slug, "timeline",
                        {"from": args.frm, "to": args.to, "type": args.type, "entity": args.entity})

    elif args.cmd == "recent":
        cmd = qg + ["recent", client_slug, "--limit", str(args.limit)]
        if args.since:
            cmd += ["--since", args.since]
        run_underlying(cmd, args.db, channel_id, client_slug, "recent",
                        {"since": args.since, "limit": args.limit})

    elif args.cmd == "drifts":
        cmd = qg + ["drifts", client_slug]
        run_underlying(cmd, args.db, channel_id, client_slug, "drifts", {})

    elif args.cmd == "stakeholders":
        cmd = qg + ["stakeholders", client_slug, args.project]
        run_underlying(cmd, args.db, channel_id, client_slug, "stakeholders",
                        {"project": args.project})

    elif args.cmd == "project":
        cmd = qg + ["project", client_slug, args.project]
        run_underlying(cmd, args.db, channel_id, client_slug, "project",
                        {"project": args.project})

    elif args.cmd == "stats":
        cmd = qg + ["stats", client_slug]
        run_underlying(cmd, args.db, channel_id, client_slug, "stats", {})


if __name__ == "__main__":
    main()
